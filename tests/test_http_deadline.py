"""Exercise the real child HTTP client against a slow local server."""
import http.server
import json
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from router import http_client


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.seen = threading.Event()
        seen = self.seen

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.set()
                if self.path.startswith('/stall'):
                    time.sleep(3)
                self.send_response(429 if self.path.startswith('/error') else 200)
                self.end_headers()
                try:
                    if self.path.startswith('/trickle'):
                        for byte in b'{"results": []}' + b' ' * 50:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.08)
                    else:
                        self.wfile.write(b'{"results": []}')
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Expected: the test deliberately kills the client.

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = 'http://127.0.0.1:%s' % self.server.server_port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_stalled_headers_and_trickling_body_are_killed_and_reaped(self):
        original = subprocess.Popen
        for path in ('/stall', '/trickle'):
            workers = []
            def spawn(*args, **kwargs):
                process = original(*args, **kwargs)
                workers.append(process)
                return process
            self.seen.clear()
            with self.subTest(path=path), patch.object(http_client.subprocess, 'Popen', side_effect=spawn):
                start = time.monotonic()
                with self.assertRaises(TimeoutError):
                    http_client.request_json(urllib.request.Request(self.url + path), .6)
                elapsed = time.monotonic() - start
                self.assertTrue(self.seen.is_set(), 'server never received the request')
                self.assertLess(elapsed, 1.6)
                self.assertEqual(len(workers), 1)
                self.assertIsNotNone(workers[0].poll(), 'HTTP worker leaked')

    def test_success_and_sanitized_http_error(self):
        self.assertEqual(http_client.request_json(urllib.request.Request(self.url), 2), {'results': []})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            http_client.request_json(urllib.request.Request(self.url + '/error?key=secret'), 2)
        self.assertEqual(caught.exception.code, 429)
        self.assertNotIn('secret', str(caught.exception))

    def test_invalid_deadline_does_not_spawn(self):
        for timeout in (0, -1, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), patch.object(http_client.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    http_client.request_json(urllib.request.Request(self.url), timeout)
                run.assert_not_called()
