"""Google API wire contract without credentials or network requests."""

import unittest
import urllib.parse
from unittest.mock import Mock

from router import google_cse


class GoogleCSETests(unittest.TestCase):
    def search(self, response, query='"поиск" & Python', domains=None, language='ru'):
        transport = Mock(return_value=response)
        result = google_cse.search(query, 'google-cse-2', domains, language,
                                   'fake-google-key', 'fake-cx', transport)
        return result, transport

    def test_request_and_result_mapping(self):
        result, transport = self.search({'kind': 'customsearch#search', 'items': [
            {'link': 'https://docs.python.org/', 'title': 'Python', 'snippet': 'Описание'}]})
        request = transport.call_args.args[0]
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(url.scheme + '://' + url.netloc + url.path, google_cse.SEARCH_URL)
        self.assertEqual(urllib.parse.parse_qs(url.query), {
            'key': ['fake-google-key'], 'cx': ['fake-cx'],
            'q': ['"поиск" & Python'], 'num': ['10'], 'hl': ['ru']})
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        transport.assert_called_once()
        self.assertEqual(result, {'results': [{'url': 'https://docs.python.org/',
            'title': 'Python', 'content': 'Описание', 'engine': 'google-cse-2'}]})

    def test_domains_are_grouped_and_language_is_optional(self):
        _, transport = self.search({'kind': 'customsearch#search'}, query='Python OR asyncio',
                                    domains=['python.org', 'habr.com'], language=None)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.call_args.args[0].full_url).query)
        self.assertEqual(params['q'], ['(Python OR asyncio) (site:python.org OR site:habr.com)'])
        self.assertNotIn('hl', params)

    def test_empty_search_response(self):
        for response in ({'kind': 'customsearch#search'},
                         {'kind': 'customsearch#search', 'items': []}):
            with self.subTest(response=response):
                self.assertEqual(self.search(response)[0], {'results': []})

    def test_optional_text_fields(self):
        result, _ = self.search({'kind': 'customsearch#search', 'items': [{'link': 'https://example.org'}]})
        self.assertEqual(result['results'][0]['title'], '')
        self.assertEqual(result['results'][0]['content'], '')

    def test_malformed_responses_are_rejected(self):
        responses = [None, [], {}, {'kind': 'other'},
                     {'kind': 'customsearch#search', 'items': None},
                     {'kind': 'customsearch#search', 'items': {}},
                     {'kind': 'customsearch#search', 'items': [None]},
                     {'kind': 'customsearch#search', 'items': [{}]},
                     {'kind': 'customsearch#search', 'items': [{'link': 42}]},
                     {'kind': 'customsearch#search', 'items': [{'link': 'https://example.org', 'title': None}]},
                     {'kind': 'customsearch#search', 'items': [{'link': 'https://example.org', 'snippet': []}]}]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.search(response)

    def test_api_error_does_not_leak_payload(self):
        response = {'error': {'code': 403, 'message': 'fake-google-key invalid at secret-url'}}
        with self.assertRaises(ValueError) as error:
            self.search(response)
        self.assertNotIn('fake-google-key', str(error.exception))
        self.assertNotIn('secret-url', str(error.exception))

    def test_transport_failure_propagates_without_retry(self):
        transport = Mock(side_effect=TimeoutError('deadline exceeded'))
        with self.assertRaises(TimeoutError):
            google_cse.search('query', 'google-cse', [], None, 'fake-key', 'fake-cx', transport)
        transport.assert_called_once()
