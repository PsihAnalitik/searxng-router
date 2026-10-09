import importlib.util
import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('http_app', ROOT / 'router/app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_search_http_preserves_free_contract(self):
        with patch.object(app, 'upstream', return_value={'results': [{'url': 'https://example.org', 'title': 'ok'}]}), patch.object(app, 'tavily_search') as paid:
            with urllib.request.urlopen(self.url + '/search?q=hello&limit=1') as response:
                result = json.load(response)
            self.assertEqual(result['tier'], 'free')
            self.assertFalse(result['escalated'])
            self.assertEqual(len(result['results']), 1)
            paid.assert_not_called()

    def test_invalid_input_does_not_dispatch(self):
        with patch.object(app, 'search') as search:
            for path in ('/search', '/search?q=x&pack=not-a-pack'):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(self.url + path)
                self.assertEqual(caught.exception.code, 400)
                caught.exception.close()
            search.assert_not_called()
