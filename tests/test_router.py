"""Offline regression tests for free-first routing and paid budget boundaries."""

import importlib.util
import io
import http.client
import urllib.error
import json
import pathlib
import unittest
from unittest.mock import Mock, patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tested_router", ROOT / "router/app.py")
router = importlib.util.module_from_spec(SPEC)
with patch.dict("os.environ", {}, clear=True):
    SPEC.loader.exec_module(router)


def result(url="https://example.org/article"):
    return {"results": [{"url": url, "title": "Article", "content": "Text"}]}


class ConfiguredKeysTests(unittest.TestCase):
    def test_auto_discovers_only_nonempty_credentials(self):
        self.assertEqual(router.configured_keys({
            "TAVILY_API_KEY": " key-one ", "TAVILY_API_KEY_2": "key-two",
            "TAVILY_API_KEY_EMPTY": "  ", "OTHER_API_KEY": "ignored",
        }), {"tavily": "key-one", "tavily-2": "key-two"})

    def test_explicit_empty_disables_paid_pool(self):
        self.assertEqual(router.configured_keys({
            "TAVILY_API_KEY": "key-one", "PAID_ENGINES": "",
        }), {})

    def test_missing_credentials_never_enter_pool(self):
        self.assertEqual(router.configured_keys({"PAID_ENGINES": "tavily,tavily-2"}), {})

    def test_selection_preserves_requested_order(self):
        self.assertEqual(list(router.configured_keys({
            "TAVILY_API_KEY": "one", "TAVILY_API_KEY_2": "two",
            "PAID_ENGINES": "tavily-2,tavily",
        })), ["tavily-2", "tavily"])

    def test_duplicate_credentials_rejected(self):
        with self.assertRaises(ValueError):
            router.configured_keys({"TAVILY_API_KEY": "same", "TAVILY_API_KEY_2": "same"})

    def test_unsupported_provider_rejected(self):
        with self.assertRaises(ValueError):
            router.configured_keys({"PAID_ENGINES": "other-provider"})


class SearchTests(unittest.TestCase):
    def setUp(self):
        patches = {
            "upstream": Mock(return_value={"results": []}),
            "tavily_search": Mock(return_value=result()),
            "VALKEY": Mock(),
            "PAID_KEYS": {"tavily": "test-key"},
            "PAID_ENGINES": ["tavily"],
            "LIMITS": {"tavily": {"day": 1, "month": 10}},
        }
        for name, replacement in patches.items():
            active = patch.object(router, name, replacement)
            active.start()
            self.addCleanup(active.stop)
        router.VALKEY.cmd.side_effect = lambda command, *args: ["0", "0"] if command == "MGET" else 1

    def search(self, pack=None):
        return router.search("test query", pack, 5, "ru")

    def test_free_success_never_checks_budget_or_calls_paid(self):
        router.upstream.return_value = result()
        response = self.search()
        self.assertEqual(response["tier"], "free")
        self.assertFalse(response["escalated"])
        router.VALKEY.cmd.assert_not_called()
        router.tavily_search.assert_not_called()

    def test_no_credentials_never_contacts_valkey_or_paid(self):
        with patch.object(router, "PAID_ENGINES", []), patch.object(router, "PAID_KEYS", {}):
            response = self.search()
        self.assertEqual(response["tier"], "none")
        self.assertFalse(response["escalated"])
        router.VALKEY.cmd.assert_not_called()
        router.tavily_search.assert_not_called()

    def test_free_failure_uses_direct_tavily_after_reservation(self):
        router.upstream.side_effect = TimeoutError("free timeout")
        events = []

        def budget(command, *args):
            events.append(command)
            return ["0", "0"] if command == "MGET" else 1

        def paid(*args):
            events.append("paid")
            return result()

        router.VALKEY.cmd.side_effect = budget
        router.tavily_search.side_effect = paid
        response = self.search()
        self.assertEqual(response["tier"], "paid")
        self.assertEqual(events, ["MGET", "EVAL", "paid"])
        router.upstream.assert_called_once_with("test query", router.FREE_ENGINES, "ru")
        router.tavily_search.assert_called_once_with("test query", "tavily", None)

    def test_exhausted_budget_blocks_paid(self):
        router.VALKEY.cmd.return_value = ["0", "1"]
        router.VALKEY.cmd.side_effect = None
        self.assertEqual(self.search()["tier"], "none")
        router.tavily_search.assert_not_called()
        self.assertEqual(router.VALKEY.cmd.call_count, 1)

    def test_valkey_down_blocks_paid(self):
        router.VALKEY.cmd.side_effect = ConnectionError("unavailable")
        response = self.search()
        self.assertEqual(response["tier"], "none")
        self.assertTrue(response["warnings"])
        router.tavily_search.assert_not_called()

    def test_lost_reservation_reply_blocks_paid_without_retry(self):
        router.VALKEY.cmd.side_effect = [["0", "0"], ConnectionError("lost reply")]
        self.assertFalse(self.search()["escalated"])
        router.tavily_search.assert_not_called()
        self.assertEqual(router.VALKEY.cmd.call_count, 2)

    def test_concurrent_budget_exhaustion_blocks_paid(self):
        router.VALKEY.cmd.side_effect = [["0", "0"], 0]
        self.assertFalse(self.search()["escalated"])
        router.tavily_search.assert_not_called()

    def test_timed_out_paid_attempt_is_not_refunded(self):
        router.tavily_search.side_effect = TimeoutError("unknown outcome")
        response = self.search()
        self.assertEqual(response["tier"], "none")
        self.assertTrue(response["escalated"])
        self.assertEqual([call.args[0] for call in router.VALKEY.cmd.call_args_list], ["MGET", "EVAL"])
        router.tavily_search.assert_called_once()

    def test_failed_key_falls_back_to_second_key(self):
        router.tavily_search.side_effect = [TimeoutError("first key"), result()]
        with patch.object(router, "PAID_ENGINES", ["tavily", "tavily-2"]), patch.dict(
                router.LIMITS, {"tavily-2": {"day": 1, "month": 10}}):
            response = self.search()
        self.assertEqual(response["engines_used"], ["tavily-2"])
        self.assertEqual([call.args[1] for call in router.tavily_search.call_args_list], ["tavily", "tavily-2"])
        self.assertEqual([call.args[0] for call in router.VALKEY.cmd.call_args_list],
                         ["MGET", "MGET", "EVAL", "EVAL"])

    def test_answer_without_links_does_not_stop_fallback(self):
        router.upstream.return_value = {"results": [], "answers": ["A plain answer"]}
        self.assertEqual(self.search()["tier"], "paid")
        router.tavily_search.assert_called_once()

    def test_malformed_free_payloads_fall_back(self):
        for payload in (None, [], "error", {}, {"results": None}, {"results": {}},
                        {"results": [None, "captcha", {}, {"url": 42}]}):
            with self.subTest(payload=payload):
                router.upstream.return_value = payload
                self.assertEqual(self.search()["tier"], "paid")

    def test_expected_transport_and_decode_errors_fall_back(self):
        errors = [TimeoutError(), ConnectionError(), http.client.IncompleteRead(b""),
                  json.JSONDecodeError("invalid", "<html>", 0),
                  urllib.error.HTTPError("http://upstream", 429, "limited", {}, None)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                router.upstream.side_effect = error
                self.assertEqual(self.search()["tier"], "paid")

    def test_partial_errors_and_malformed_metadata_preserve_valid_links(self):
        for dead in (None, {}, [None, [], ["google"], ["google", "access denied"]]):
            with self.subTest(dead=dead):
                router.upstream.return_value = dict(result(), unresponsive_engines=dead,
                                                    answers=[None, {"current": 42}], infoboxes=[None])
                self.assertEqual(self.search()["tier"], "free")
        router.tavily_search.assert_not_called()
        self.assertIn("access denied", self.search()["warnings"][0])

    def test_allowlist_rejects_free_and_accepts_paid(self):
        router.upstream.return_value = result("https://untrusted.org/page")
        router.tavily_search.return_value = dict(result("https://docs.python.org/page"),
                                                answers=["unattributed text"])
        with patch.object(router, "ALLOWED_DOMAINS", ["python.org"]):
            response = self.search()
        self.assertEqual(response["tier"], "paid")
        self.assertEqual(response["answers"], [])
        self.assertEqual(response["results"][0]["url"], "https://docs.python.org/page")

    def test_mixed_free_results_return_only_allowed_without_paid(self):
        router.upstream.return_value = {"results": [
            {"url": "https://evil.org/"}, {"url": "https://docs.python.org/"}],
            "answers": ["unattributed"]}
        with patch.object(router, "ALLOWED_DOMAINS", ["python.org"]):
            response = self.search()
        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(response["answers"], [])
        router.tavily_search.assert_not_called()

    def test_no_allowed_paid_links_returns_none(self):
        router.tavily_search.return_value = dict(result(), answers=["not a link"])
        with patch.object(router, "ALLOWED_DOMAINS", ["python.org"]):
            response = self.search()
        self.assertEqual(response["tier"], "none")
        self.assertEqual(response["results"], [])
        self.assertEqual(response["answers"], [])

    def test_malformed_paid_payload_advances_to_next_key(self):
        router.tavily_search.side_effect = [{"results": None}, result()]
        with patch.object(router, "PAID_ENGINES", ["tavily", "tavily-2"]), patch.dict(
                router.LIMITS, {"tavily-2": {"day": 1, "month": 10}}):
            self.assertEqual(self.search()["engines_used"], ["tavily-2"])


    def test_off_domain_free_result_does_not_stop_fallback(self):
        router.upstream.return_value = result("https://example.org/?ref=plato.stanford.edu")
        router.tavily_search.return_value = result("https://plato.stanford.edu/entries/test")
        self.assertEqual(self.search("philosophy")["tier"], "paid")
        router.upstream.assert_called_once_with("test query", ["philosophy"], "ru")


class PayloadTests(unittest.TestCase):
    def test_domain_filter_accepts_subdomains_and_rejects_spoofs(self):
        accepted = ["https://plato.stanford.edu/entry", "https://sub.plato.stanford.edu/entry"]
        rejected = ["https://example.org/?ref=plato.stanford.edu",
                    "https://plato.stanford.edu.evil.org/", "https://evilplato.stanford.edu/",
                    "https://plato.stanford.edu@evil.org/", "javascript:plato.stanford.edu",
                    "https://[broken"]
        payload = {"results": [{"url": url} for url in accepted + rejected]}
        self.assertEqual([item["url"] for item in router.accept(payload, "philosophy")], accepted)

    def test_optional_allowlist_config_validation(self):
        self.assertEqual(router.configured_domains("  "), [])
        self.assertEqual(router.configured_domains("Python.ORG., habr.com"), ["python.org", "habr.com"])
        for value in ("https://python.org", "*.python.org", "python.org/path", ",", "python.org,", "bad host"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                router.configured_domains(value)

    def test_global_and_pack_filters_intersect(self):
        urls = ["https://plato.stanford.edu/entry", "https://habr.com/page"]
        with patch.object(router, "ALLOWED_DOMAINS", ["habr.com"]):
            self.assertEqual(router.accept({"results": [{"url": u} for u in urls]}, "philosophy"), [])

    def test_global_filter_rejects_spoofs_and_malformed_urls(self):
        bad = ["https://python.org.evil.org/", "https://evilpython.org/",
               "https://evil.org/?ref=python.org", "https://python.org@evil.org/",
               "https://evil.org@python.org/", "https://python.org:bad/",
               "https://python.org/white space", "https://python.org\\@evil.org/",
               "javascript:python.org", 123, None]
        good = ["https://python.org/", "https://DOCS.PYTHON.ORG./page"]
        with patch.object(router, "ALLOWED_DOMAINS", ["python.org"]):
            self.assertEqual([x["url"] for x in router.accept(
                {"results": [{"url": u} for u in bad + good]}, None)], good)

    def test_tavily_receives_global_domains_and_skips_invalid_items(self):
        response = io.BytesIO(json.dumps({"results": [None, {"url": "https://python.org/"}]}).encode())
        with patch.object(router, "ALLOWED_DOMAINS", ["python.org"]), patch.object(
                router, "PAID_KEYS", {"tavily": "fake-key"}), patch.object(
                router.urllib.request, "urlopen", return_value=response) as request:
            payload = router.tavily_search("query", "tavily", None)
        self.assertEqual(json.loads(request.call_args.args[0].data)["include_domains"], ["python.org"])
        self.assertEqual(payload["results"][1]["engine"], "tavily")

    def test_tavily_serializes_quoted_unicode_and_pack_domains(self):
        query = '"точная фраза"\nновая строка \\ slash'
        response = io.BytesIO(json.dumps(result()).encode())
        with patch.object(router, "PAID_KEYS", {"tavily": "fake-key"}), patch.object(
                router.urllib.request, "urlopen", return_value=response) as urlopen:
            payload = router.tavily_search(query, "tavily", "philosophy")
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["query"], query)
        self.assertEqual(body["include_domains"], router.PACKS["philosophy"])
        self.assertEqual(body["search_depth"], "basic")
        self.assertFalse(body["auto_parameters"])
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-key")
        self.assertEqual(payload["results"][0]["engine"], "tavily")


class ValkeyTransportTests(unittest.TestCase):
    def test_uncertain_mutation_is_never_replayed(self):
        client = router.Valkey("unused", 0)
        client._sock = Mock()
        client._file = Mock()
        sock, stream = client._sock, client._file
        with patch.object(client, "_read", side_effect=ConnectionError("lost reply")), patch.object(
                client, "_connect") as connect:
            with self.assertRaises(ConnectionError):
                client.cmd("EVAL", "return 1", 0)
        sock.sendall.assert_called_once()
        sock.close.assert_called_once()
        stream.close.assert_called_once()
        connect.assert_not_called()
        self.assertIsNone(client._sock)


if __name__ == "__main__":
    unittest.main()
