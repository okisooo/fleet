"""Offline billing, credential-source, and presentation regressions."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import apikeys
import fleet
import panel_model


NOW = datetime(2026, 10, 25, 15, 37, tzinfo=timezone.utc).timestamp()
KEY = "fixture-key-for-offline-tests"


def bucket(day, *amounts):
    return {"starting_at": f"2026-10-{day:02}T00:00:00Z",
            "results": [{"amount": amount, "currency": "USD"} for amount in amounts]}


class AnthropicApiTests(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.dict(os.environ, {}, clear=True),
                        mock.patch.object(apikeys, "_get_registry_env", return_value=None),
                        mock.patch.object(apikeys.api_credentials, "read", return_value=None),
                        mock.patch.object(fleet, "_LIVE", {}),
                        mock.patch.object(apikeys.time, "time", return_value=NOW)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def collect(self, payloads, status=200, ordinary=False):
        pid = "anthropic" if ordinary else "anthropic-admin"
        spec = dict(apikeys.BUILTIN_BY_ID[pid])
        if ordinary:
            spec["connection_only"] = True
        replies = iter(payloads)
        calls = []

        def http(method, url, headers=None, body=None, timeout=0):
            calls.append((method, url, headers, body))
            payload = next(replies)
            return status, {}, payload if isinstance(payload, bytes) else json.dumps(payload).encode()

        with mock.patch.dict(os.environ, {spec["env"][0]: KEY}):
            out = apikeys.collect(spec, http, fleet._header, fleet.M)
        self.assertNotIn(KEY, json.dumps(out))
        return out, calls

    def test_sums_all_result_rows_and_converts_decimal_cents(self):
        out, calls = self.collect([{"data": [bucket(1, "100"), bucket(25, "123.45", "76.55")]}])
        self.assertEqual(out["err"], "")
        self.assertEqual([m["extra"] for m in out["metrics"]], ["$3.00", "$2.00"])
        self.assertEqual(out["account"]["mode"], "spend")
        self.assertEqual(calls[0][0], "GET")
        query = parse_qs(urlparse(calls[0][1]).query)
        self.assertEqual(query["starting_at"], ["2026-10-01T00:00:00Z"])
        self.assertNotIn("ending_at", query)  # the API defaults to now; never ask for future data
        self.assertEqual(query["limit"], ["31"])
        self.assertEqual(calls[0][2]["x-api-key"], KEY)
        self.assertEqual(calls[0][2]["anthropic-version"], "2023-06-01")
        self.assertNotIn("Authorization", calls[0][2])
        self.assertIsNone(calls[0][3])

    def test_follows_pagination_before_publishing_totals(self):
        out, calls = self.collect([
            {"data": [bucket(1, "100")], "has_more": True, "next_page": "next one"},
            {"data": [bucket(25, "200")], "has_more": False},
        ])
        self.assertEqual([m["extra"] for m in out["metrics"]], ["$3.00", "$2.00"])
        self.assertEqual(parse_qs(urlparse(calls[1][1]).query)["page"], ["next one"])

    def test_malformed_and_repeating_pagination_does_not_publish_partial_totals(self):
        for payloads in ([{"data": [], "has_more": True}], [
            {"data": [bucket(1, "100")], "has_more": True, "next_page": "repeat"},
            {"data": [bucket(25, "100")], "has_more": True, "next_page": "repeat"},
        ]):
            with self.subTest(payloads=payloads):
                out, _ = self.collect(payloads)
                self.assertEqual(out["metrics"], [])
                self.assertTrue(out["err"])

    def test_empty_report_is_a_live_zero_spend_in_the_panel(self):
        for data in ([], [bucket(25)]):
            with self.subTest(data=data):
                out, _ = self.collect([{"data": data}])
                provider = panel_model.build([out], NOW)["providers"][0]
                self.assertEqual(provider["balance"], {"value": "$0.00", "label": "This month"})
                self.assertEqual(len(provider["groups"][0]["rows"]), 2)
                self.assertIsNone(provider["spend"])
                self.assertEqual(provider["account"], {"mode": "spend"})

    def test_invalid_report_is_never_displayed_as_zero(self):
        for payload in ({}, None, {"data": {}}, {"data": [bucket(25, "NaN")]},
                        {"data": [bucket(25, "Infinity")]}, {"data": [bucket(25, "-1")]},
                        {"data": [{"starting_at": "bad", "results": []}]}, b"not JSON"):
            with self.subTest(payload=payload):
                out, _ = self.collect([payload])
                self.assertEqual(out["metrics"], [])
                self.assertEqual(out["err"], "bad billing report")

    def test_http_errors_discard_response_bodies_and_have_no_metrics(self):
        for code in (0, 401, 403, 404, 429, 500):
            with self.subTest(code=code):
                out, _ = self.collect([{"secret": KEY}], status=code)
                self.assertEqual(out["metrics"], [])
                self.assertTrue(out["err"])

    def test_large_report_is_refused(self):
        out, _ = self.collect([b" " * 1_000_001])
        self.assertEqual(out["err"], "payload too large")

    def test_regular_key_gets_only_a_free_models_request(self):
        out, calls = self.collect([{}], ordinary=True)
        self.assertEqual([(c[0], urlparse(c[1]).path) for c in calls], [("GET", "/v1/models")])
        self.assertEqual(out["account"]["mode"], "connection")
        self.assertIsNone(out["metrics"][0]["pct"])
        self.assertEqual(out["metrics"][0]["extra"], "View in Console")
        self.assertIsNone(calls[0][3])

    def test_regular_key_auto_enables_connection_but_not_a_paid_probe(self):
        specs = apikeys.enabled_specs({}, {"ANTHROPIC_API_KEY": KEY})
        self.assertTrue(specs[0]["connection_only"])
        specs = apikeys.enabled_specs({"providers": {"anthropic": {"probe": True}}}, {"ANTHROPIC_API_KEY": KEY})
        self.assertNotIn("connection_only", specs[0])

    def test_admin_report_avoids_a_duplicate_regular_connection(self):
        specs = apikeys.enabled_specs({}, {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_ADMIN_KEY": KEY})
        self.assertEqual([spec["id"] for spec in specs], ["anthropic-admin"])

    def test_saved_key_enables_an_account_and_environment_takes_precedence(self):
        with mock.patch.object(apikeys.api_credentials, "read", side_effect=lambda pid: KEY if pid == "anthropic-admin" else None):
            self.assertEqual([s["id"] for s in apikeys.enabled_specs({})], ["anthropic-admin"])
            metadata = apikeys.account_status()
            self.assertEqual(metadata[1]["source"], "windows")
            self.assertNotIn(KEY, json.dumps(metadata))
            with mock.patch.dict(os.environ, {"ANTHROPIC_ADMIN_KEY": "environment-fixture"}):
                self.assertEqual(apikeys._key_for(apikeys.BUILTIN_BY_ID["anthropic-admin"]),
                                 ("environment", "environment-fixture"))

    def test_explicit_environment_mapping_never_reads_windows_credentials(self):
        with mock.patch.object(apikeys.api_credentials, "read") as read:
            self.assertEqual(apikeys.enabled_specs({}, {}), [])
        read.assert_not_called()

    def test_no_admin_key_is_inferred_from_an_ordinary_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY}):
            self.assertEqual(apikeys._key_for(apikeys.BUILTIN_BY_ID["anthropic-admin"]), (None, None))

    def test_failed_report_reuses_only_a_cache_for_the_same_key(self):
        good, _ = self.collect([{"data": [bucket(25, "100")]}])
        failed = dict(good, metrics=[], err="offline")
        spec = apikeys.BUILTIN_BY_ID["anthropic-admin"]
        for cached, expected in ((good, True), (dict(good, credential_id="other-account"), False),
                                 ({"metrics": [fleet.M("Cost 24h", extra="$100.00")]}, False)):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp, \
                    mock.patch.object(fleet, "CACHE", Path(temp)), \
                    mock.patch.object(apikeys, "collect", return_value=failed):
                fleet._save_last("api_anthropic-admin", cached)
                out = fleet._collect_one_spec(spec)
                self.assertEqual(bool(out["metrics"]), expected)
                if expected:
                    self.assertFalse(out["compact"])
                    self.assertEqual(out["account"]["mode"], "spend")
                    self.assertEqual(out["cached_at"], NOW)

    def test_no_key_is_written_to_provider_cache_or_panel_model(self):
        good, _ = self.collect([{"data": [bucket(25, "100")]}])
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(fleet, "CACHE", Path(temp)), \
                mock.patch.object(apikeys, "collect", return_value=good):
            out = fleet._collect_one_spec(apikeys.BUILTIN_BY_ID["anthropic-admin"])
            self.assertNotIn(KEY, json.dumps(panel_model.build([out], NOW)))
            self.assertNotIn(KEY, (Path(temp) / "api_anthropic-admin_last.json").read_text())

    def test_rejected_key_has_a_settings_repair_action(self):
        model = panel_model.build([{"agent": "Claude API", "api_id": "anthropic-admin", "metrics": [], "err": "key rejected"}], NOW)
        self.assertEqual(model["inactive"][0]["action"], "api-settings")
        self.assertEqual(model["inactive"][0]["label"], "Key rejected")

    def test_rejected_key_cannot_keep_a_previous_spend_row_alive(self):
        good, _ = self.collect([{"data": [bucket(25, "100")]}])
        failed = dict(good, metrics=[], err="key rejected")
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(fleet, "CACHE", Path(temp)), \
                mock.patch.object(apikeys, "collect", return_value=failed):
            fleet._save_last("api_anthropic-admin", good)
            out = fleet._collect_one_spec(apikeys.BUILTIN_BY_ID["anthropic-admin"])
        self.assertEqual(out["metrics"], [])
        self.assertEqual(panel_model.build([out], NOW)["inactive"][0]["action"], "api-settings")

    def test_yesterdays_cache_does_not_become_today_or_this_month(self):
        good, _ = self.collect([{"data": [bucket(25, "100")]}])
        failed = dict(good, metrics=[], err="offline")
        for hours_ago in (23, 24, 24 * 30):
            with self.subTest(hours_ago=hours_ago), tempfile.TemporaryDirectory() as temp, \
                    mock.patch.object(fleet, "CACHE", Path(temp)), mock.patch.object(apikeys, "collect", return_value=failed):
                last = dict(good, ts=NOW - hours_ago * 3600)
                (Path(temp) / "api_anthropic-admin_last.json").write_text(json.dumps(last))
                self.assertEqual(fleet._collect_one_spec(apikeys.BUILTIN_BY_ID["anthropic-admin"])["metrics"], [])

    def test_failures_are_paced_and_explicit_refresh_can_retry(self):
        fetch = mock.Mock(return_value={"metrics": [], "err": "key rejected"})
        with mock.patch.object(fleet.time, "monotonic", side_effect=[0, 10, 20]):
            fleet._paced("api_key:anthropic-admin", fetch)
            fleet._paced("api_key:anthropic-admin", fetch)
            self.assertEqual(fetch.call_count, 1)
            fleet._paced("api_key:anthropic-admin", fetch, force=True)
            self.assertEqual(fetch.call_count, 2)

    def test_retry_after_extends_automatic_failure_backoff(self):
        self.assertEqual(apikeys._retry_at({"Retry-After": "600"}), NOW + 600)
        self.assertIsNone(apikeys._retry_at({"Retry-After": "not a date"}))
        fetch = mock.Mock(return_value={"metrics": [], "err": "rate limited", "retry_at": NOW + 600})
        with mock.patch.object(fleet.time, "monotonic", side_effect=[0, 400]):
            fleet._paced("api_key:anthropic-admin", fetch)
            fleet._paced("api_key:anthropic-admin", fetch)
        self.assertEqual(fetch.call_count, 1)

    def test_setup_metadata_honors_remapped_and_disabled_providers(self):
        config = {"providers": {"anthropic-admin": {"env": "WORK_BILLING_KEY", "enabled": False}}}
        with mock.patch.dict(os.environ, {"WORK_BILLING_KEY": KEY}):
            metadata = apikeys.account_status(config)
        self.assertEqual(metadata[1], {"id": "anthropic-admin", "source": "environment", "enabled": False})

    def test_account_metadata_is_allowlisted_and_does_not_join_local_api_estimates(self):
        out, _ = self.collect([{"data": []}])
        out["account"]["unexpected"] = KEY
        model = panel_model.build([out], NOW, accounts=[{"id": "anthropic", "source": "windows", "unexpected": KEY}])
        self.assertNotIn(KEY, json.dumps(model))
        self.assertEqual(model["api_accounts"], [{"id": "anthropic", "source": "windows", "enabled": True}])
        self.assertIsNone(model["summary"])
        self.assertIsNone(model["providers"][0]["headline"])


if __name__ == "__main__":
    unittest.main()
