import contextlib
import importlib.util
import io
import json
import pathlib
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('usage', ROOT / 'scripts/tavily-usage.py')
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)


class UsageTests(unittest.TestCase):
    def test_router_counts_are_per_engine_and_use_configured_port(self):
        payload = {'budget': [{'engine': 'tavily', 'month_used': 2},
                              {'engine': 'tavily-2', 'month_used': 7}]}
        with patch.object(usage.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(payload).encode())) as request:
            counts = usage.read_local_counts({'ROUTER_PORT': '18090'})
        self.assertEqual(counts['tavily']['month_used'], 2)
        self.assertEqual(counts['tavily-2']['month_used'], 7)
        self.assertEqual(request.call_args.args[0], 'http://127.0.0.1:18090/budget')

    def test_no_keys_does_not_request_provider_usage(self):
        with patch.object(usage, 'read_keys', return_value={}), patch.object(usage, 'fetch_usage') as fetch, contextlib.redirect_stdout(io.StringIO()):
            usage.main()
        fetch.assert_not_called()
