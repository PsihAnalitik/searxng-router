"""Provider wire contracts and credential failover; no real provider requests."""
import base64
import importlib.util
import io
import json
import pathlib
import unittest
import urllib.error
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('provider_app', ROOT / 'router/app.py')
app = importlib.util.module_from_spec(spec)
with patch.dict('os.environ', {}, clear=True):
    spec.loader.exec_module(app)


def xml_response(xml):
    return io.BytesIO(json.dumps({'rawData': base64.b64encode(xml.encode()).decode()}).encode())


class YandexTests(unittest.TestCase):
    def setUp(self):
        for name, value in {'PAID_KEYS': {'yandex-api': 'fake-yandex-key'},
                            'PAID_OPTIONS': {'yandex-api': {'folder_id': 'fake-folder'}}}.items():
            active = patch.object(app, name, value)
            active.start()
            self.addCleanup(active.stop)

    def test_authorization_request_and_xml_result(self):
        xml = '<yandexsearch><response><results><grouping><group><doc><url>https://example.org/</url><title>Про <hlword>поиск</hlword></title><passages><passage>Первый <hlword>текст</hlword>.</passage><passage>Второй.</passage></passages></doc></group></grouping></results></response></yandexsearch>'
        with patch.object(app.urllib.request, 'urlopen', return_value=xml_response(xml)) as call:
            result = app.yandex_search('"поиск"', 'yandex-api', None, 'en')
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, 'https://searchapi.api.cloud.yandex.net/v2/web/search')
        self.assertEqual(request.get_header('Authorization'), 'Api-Key fake-yandex-key')
        body = json.loads(request.data)
        self.assertEqual(body['folderId'], 'fake-folder')
        self.assertEqual(body['query']['queryText'], '"поиск"')
        self.assertEqual(body['query']['searchType'], 'SEARCH_TYPE_COM')
        self.assertEqual(body['responseFormat'], 'FORMAT_XML')
        self.assertEqual(result['results'][0], {'url': 'https://example.org/', 'title': 'Про поиск',
                                              'content': 'Первый текст. Второй.', 'engine': 'yandex-api'})

    def test_domain_pack_and_default_search_type(self):
        with patch.object(app.urllib.request, 'urlopen', return_value=xml_response('<yandexsearch><response/></yandexsearch>')) as call:
            app.yandex_search('философия', 'yandex-api', 'philosophy', None)
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body['query']['searchType'], 'SEARCH_TYPE_RU')
        for domain in app.PACKS['philosophy']:
            self.assertIn('site:' + domain, body['query']['queryText'])

    def test_empty_results_are_not_a_provider_error(self):
        with patch.object(app.urllib.request, 'urlopen', return_value=xml_response('<yandexsearch><response><error code="15">none</error></response></yandexsearch>')):
            self.assertEqual(app.yandex_search('test', 'yandex-api', None, None), {'results': []})

    def test_xml_quota_error_is_not_silently_empty(self):
        for code in ('32', '55', '42'):
            with self.subTest(code=code), patch.object(app.urllib.request, 'urlopen', return_value=xml_response('<yandexsearch><response><error code="'+code+'">quota</error></response></yandexsearch>')):
                with self.assertRaises(ValueError):
                    app.yandex_search('test', 'yandex-api', None, None)

    def test_unsafe_or_malformed_xml_is_rejected(self):
        for xml in ('<!DOCTYPE yandexsearch [<!ENTITY x "text">]><yandexsearch/>', '<broken', '<html/>'):
            with self.subTest(xml=xml), patch.object(app.urllib.request, 'urlopen', return_value=xml_response(xml)):
                with self.assertRaises(ValueError):
                    app.yandex_search('test', 'yandex-api', None, None)

    def test_yandex_configuration_requires_folder_only_for_enabled_key(self):
        self.assertEqual(app.configured_keys({}), {})
        with self.assertRaisesRegex(ValueError, 'YANDEX_FOLDER_ID'):
            app.configured_keys({'YANDEX_API_KEY': 'fake-key'})
        self.assertEqual(app.configured_keys({'YANDEX_API_KEY': 'fake-key', 'PAID_ENGINES': ''}), {})
        env = {'YANDEX_API_KEY': 'fake-one', 'YANDEX_API_KEY_2': 'fake-two',
               'YANDEX_FOLDER_ID': 'shared', 'YANDEX_FOLDER_ID_2': 'independent'}
        self.assertEqual(list(app.configured_keys(env)), ['yandex-api', 'yandex-api-2'])
        self.assertEqual(app.provider_option(env, 'yandex-api-2', 'FOLDER_ID'), 'independent')
        env['YANDEX_FOLDER_ID_2'] = ''
        self.assertEqual(app.provider_option(env, 'yandex-api-2', 'FOLDER_ID'), 'shared')


class QuotaFailoverTests(unittest.TestCase):
    def test_tavily_http_quota_errors_switch_to_second_key(self):
        for status in (429, 432, 433):
            with self.subTest(status=status), patch.object(app, 'PAID_ENGINES', ['tavily', 'tavily-2']), patch.object(app, 'upstream', return_value={'results': []}), patch.object(app, 'budget_state', side_effect=lambda e: {'engine': e, 'month_left': 5, 'day_left': 5}), patch.object(app, 'budget_reserve', return_value=True) as reserve, patch.object(app, 'tavily_search', side_effect=[urllib.error.HTTPError('https://example.test', status, 'quota', {}, None), {'results': [{'url': 'https://example.org'}]}]) as paid:
                result = app.search('test', None, 5, None)
                self.assertEqual(result['engines_used'], ['tavily-2'])
                self.assertEqual([c.args[1] for c in paid.call_args_list], ['tavily', 'tavily-2'])
                self.assertEqual([c.args[0] for c in reserve.call_args_list], ['tavily', 'tavily-2'])
                self.assertIn(str(status), result['warnings'][0])

    def test_yandex_xml_quota_error_switches_to_next_key(self):
        failed = '<yandexsearch><response><error code="32">quota</error></response></yandexsearch>'
        success = '<yandexsearch><response><results><doc><url>https://example.org</url><title>OK</title></doc></results></response></yandexsearch>'
        engines = ['yandex-api', 'yandex-api-2']
        with patch.object(app, 'PAID_ENGINES', engines), patch.object(app, 'PAID_KEYS', dict(zip(engines, ['fake-one', 'fake-two']))), patch.object(app, 'PAID_OPTIONS', {e: {'folder_id': 'folder'} for e in engines}), patch.object(app, 'upstream', return_value={'results': []}), patch.object(app, 'budget_state', side_effect=lambda e: {'engine': e, 'month_left': 5, 'day_left': 5}), patch.object(app, 'budget_reserve', return_value=True), patch.object(app.urllib.request, 'urlopen', side_effect=[xml_response(failed), xml_response(success)]) as paid:
            result = app.search('test', None, 5, None)
        self.assertEqual(result['engines_used'], ['yandex-api-2'])
        self.assertIn('Yandex XML error 32', result['warnings'][0])
        self.assertEqual([c.args[0].get_header('Authorization') for c in paid.call_args_list], ['Api-Key fake-one', 'Api-Key fake-two'])
