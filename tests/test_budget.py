"""Integration tests against a dedicated Valkey, never the live instance."""
import concurrent.futures
import importlib.util
import os
import pathlib
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('budget_app', ROOT / 'router/app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


@unittest.skipUnless(os.environ.get('TEST_VALKEY_HOST'), 'set TEST_VALKEY_HOST to a dedicated test server')
class BudgetTests(unittest.TestCase):
    def setUp(self):
        client = app.Valkey(os.environ['TEST_VALKEY_HOST'], int(os.environ.get('TEST_VALKEY_PORT', '6379')))
        self.addCleanup(client.close)
        self.patch = patch.object(app, 'VALKEY', client)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.engine = 'test-' + self.id().rsplit('.', 1)[-1]
        # Test-only namespace. Existing application counters remain untouched.
        for key in app._period_keys(self.engine):
            client.cmd('DEL', key)
        self.limits = patch.dict(app.LIMITS, {self.engine: {'month': 1, 'day': 1}})
        self.limits.start()
        self.addCleanup(self.limits.stop)

    def test_atomic_reservation_across_clients(self):
        def reserve(_):
            client = app.Valkey(os.environ['TEST_VALKEY_HOST'], int(os.environ.get('TEST_VALKEY_PORT', '6379')))
            try:
                return client.cmd('EVAL', app.RESERVE_SCRIPT, 2, *app._period_keys(self.engine), 1, 1)
            finally:
                client.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(reserve, range(32)))
        self.assertEqual(sum(results), 1)
        state = app.budget_state(self.engine)
        self.assertEqual((state['month_used'], state['day_used']), (1, 1))

    def test_existing_nonempty_counter_remains_exhausted(self):
        keys = app._period_keys(self.engine)
        for key in keys:
            app.VALKEY.cmd('SET', key, 1)
        self.assertFalse(app.budget_reserve(self.engine))
        self.assertEqual(app.VALKEY.cmd('MGET', *keys), ['1', '1'])

    def test_day_and_month_limits_independently(self):
        month, day = app._period_keys(self.engine)
        for full in (month, day):
            app.VALKEY.cmd('SET', month, 0)
            app.VALKEY.cmd('SET', day, 0)
            app.VALKEY.cmd('SET', full, 1)
            self.assertFalse(app.budget_reserve(self.engine))

    def test_real_counters_rotate_keys_when_day_or_month_runs_out(self):
        for period in ("day", "month"):
            with self.subTest(period=period):
                first, second = self.engine + "-" + period, self.engine + "-" + period + "-2"
                for engine in (first, second):
                    for key in app._period_keys(engine):
                        app.VALKEY.cmd("DEL", key)
                limits = {first: {"day": 1, "month": 2 if period == "day" else 1},
                          second: {"day": 1, "month": 1}}
                with patch.dict(app.LIMITS, limits), patch.object(app, "PAID_ENGINES", [first, second]), patch.object(app, "upstream", return_value={"results": []}), patch.object(app, "paid_search", return_value={"results": [{"url": "https://example.org"}]}) as paid:
                    self.assertEqual(app.search("q", None, 5, None)["engines_used"], [first])
                    self.assertEqual(app.search("q", None, 5, None)["engines_used"], [second])
                    self.assertEqual(app.search("q", None, 5, None)["tier"], "none")
                    self.assertEqual([call.args[1] for call in paid.call_args_list], [first, second])
                    self.assertEqual(app.budget_state(first)["day_used"], 1)
                    self.assertEqual(app.budget_state(second)["day_used"], 1)
