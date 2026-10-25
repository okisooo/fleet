import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import apikeys
import fleet
import panel_model
import panel_wpf
import tray


SAMPLE_AGENTS = [
    {
        "agent": "Claude",
        "plan": "max",
        "err": "",
        "metrics": [
            {"label": "Session", "pct": 42.0, "resets": None, "extra": ""},
            {"label": "Claude Wk", "pct": 71.0, "resets": None, "extra": ""},
        ],
    },
    {
        "agent": "Codex",
        "plan": "pro",
        "err": "",
        "metrics": [{"label": "Session", "pct": 12.0, "resets": None, "extra": ""}],
    },
    {
        "agent": "Grok",
        "plan": "supergrok",
        "err": "",
        "metrics": [{"label": "Daily", "pct": 96.0, "resets": None, "extra": ""}],
    },
    {
        "agent": "Antigrav",
        "plan": "pro",
        "err": "",
        "metrics": [{"label": "Monthly", "pct": 33.0, "resets": None, "extra": ""}],
    },
]


def _varint(value):
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number, value):
    value = value.encode() if isinstance(value, str) else value
    return _varint(number << 3 | 2) + _varint(len(value)) + value


class AntigravityTokenTests(unittest.TestCase):
    def test_extracts_tokens_from_nested_base64_protobuf(self):
        access = "ya29.test-access-token"
        refresh = "1//test-refresh-token"
        token_info = (
            _field(1, access)
            + _field(2, "Bearer")
            + _field(3, refresh)
            + _field(4, b"\x08\x80\x80\x80\x01")
        )
        outer = _field(1, "oauthTokenInfoSentinelKey") + _field(
            2, base64.b64encode(token_info)
        )
        stored = base64.b64encode(outer)

        self.assertEqual(fleet._agy_extract_tokens(stored), (access, refresh))


class PanelStateTests(unittest.TestCase):
    def test_stale_state_renders_without_blocking_collection(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            expected = [{"agent": "Claude", "plan": "pro", "metrics": [], "err": "reauth"}]
            state.write_text(json.dumps({"data": expected, "ts": time.time() - 3600}))

            with mock.patch.object(tray, "STATE", state), mock.patch.object(
                tray.fleet, "collect_api", side_effect=AssertionError("must not collect")
            ):
                data, ts = tray._panel_data()

        self.assertEqual(data, expected)
        self.assertLess(ts, time.time() - 3500)


class PanelDataTests(unittest.TestCase):
    def test_a_torn_state_file_never_triggers_a_network_collect(self):
        # _panel_data runs on the panel host's WPF dispatcher thread. Falling back
        # to collect_api() on a torn read froze the UI for seconds on every click
        # that landed while poll() was rewriting state.json.
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            state.write_text('{"data": [{"agent": "Claude"}], "ts": 100.0}')
            with mock.patch.object(tray, "STATE", state), mock.patch.dict(
                tray._LAST_PANEL_DATA, {"value": None}
            ):
                data, ts = tray._panel_data()
                self.assertEqual(ts, 100.0)

                state.write_text('{"data": [{"agent": "Cla')  # half-written
                with mock.patch.object(
                    tray.fleet, "collect_api", side_effect=AssertionError("must not collect")
                ):
                    torn_data, torn_ts = tray._panel_data()

        self.assertEqual(torn_data, data)
        self.assertEqual(torn_ts, 100.0)

    def test_missing_state_returns_empty_rather_than_collecting(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(tray, "STATE", Path(temp) / "absent.json"), mock.patch.dict(
                tray._LAST_PANEL_DATA, {"value": None}
            ), mock.patch.object(
                tray.fleet, "collect_api", side_effect=AssertionError("must not collect")
            ):
                data, ts = tray._panel_data()

        self.assertEqual(data, [])
        self.assertIsInstance(ts, float)

    def test_state_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            with mock.patch.object(tray, "STATE", state), mock.patch.object(
                tray.fleet, "collect_api", return_value=[{"agent": "Codex"}]
            ):
                tray.poll()

            self.assertEqual(json.loads(state.read_text())["data"], [{"agent": "Codex"}])
            # The staging file must not survive the replace.
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_failed_poll_keeps_the_last_state(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            expected = [{"agent": "Codex", "metrics": [{"pct": 20}]}]
            state.write_text(json.dumps({"data": expected, "ts": 100.0}))
            with mock.patch.object(tray, "STATE", state), mock.patch.object(
                tray.fleet, "collect_api", side_effect=RuntimeError("offline")
            ):
                actual = tray.poll()

        self.assertEqual(actual, expected)


class ProviderCacheTests(unittest.TestCase):
    def test_fallback_carries_the_real_data_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            (cache / "claude_last.json").write_text(
                json.dumps({"ts": 100.0, "plan": "max", "metrics": [{"pct": 42}]})
            )
            with mock.patch.object(fleet, "CACHE", cache), mock.patch.object(
                fleet.time, "time", return_value=400.0
            ):
                result = fleet._cached_or(
                    {"agent": "Claude", "plan": "", "metrics": [], "err": "offline"},
                    "claude",
                )

        self.assertEqual(result["cached_at"], 100.0)

    def test_grok_soft_cache_keeps_its_original_timestamp_and_cached_status(self):
        metrics = [fleet.M("Weekly", 32.0)]
        with mock.patch.object(
            fleet, "_load_last", return_value=(metrics, "SuperGrok", "1m", 123.0)
        ), mock.patch.object(fleet, "_jget", side_effect=AssertionError("network called")):
            result = fleet.api_grok()

        self.assertEqual(result["metrics"], metrics)
        self.assertEqual(result["plan"], "SuperGrok")
        self.assertEqual(result["err"], "soft cache")
        self.assertEqual(result["cached_at"], 123.0)


class ProviderConnectionTests(unittest.TestCase):
    def test_missing_claude_credentials_are_not_an_error_state(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            fleet, "HOME", Path(temp)
        ), mock.patch.object(fleet, "APPDATA", Path(temp)):
            result = fleet.api_claude()

        self.assertFalse(result["configured"])
        self.assertFalse(result["known"])
        self.assertIn("credentials", result["err"])

    def test_seen_claude_install_without_a_token_stays_discoverable(self):
        with tempfile.TemporaryDirectory() as temp:
            credentials = Path(temp) / ".claude" / ".credentials.json"
            credentials.parent.mkdir()
            credentials.write_text(json.dumps({"claudeAiOauth": {"subscriptionType": "pro"}}))
            with mock.patch.object(fleet, "HOME", Path(temp)), mock.patch.object(
                fleet, "APPDATA", Path(temp)
            ):
                result = fleet.api_claude()

        self.assertFalse(result["configured"])
        self.assertTrue(result["known"])
        # The panel turns exactly this state into a one-click sign-in.
        self.assertEqual(result["err"], fleet.CLAUDE_SIGNED_OUT)

    def test_a_desktop_app_session_keeps_claude_live_without_cli_credentials(self):
        """Signing in through the app alone leaves claudeAiOauth empty."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            credentials = root / ".claude" / ".credentials.json"
            credentials.parent.mkdir()
            credentials.write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "subscriptionType": "pro",
                            "accessToken": "",
                            "refreshToken": "",
                            "expiresAt": 0,
                        }
                    }
                )
            )
            history = root / "Claude" / "plan-usage-history.json"
            history.parent.mkdir()
            history.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "samples": [
                            {"t": (time.time() - 3600) * 1000, "u": {"fh": 4, "sd": 9}},
                            {"t": (time.time() - 300) * 1000, "u": {"fh": 12, "sd": 15}},
                        ],
                    }
                )
            )
            with mock.patch.object(fleet, "HOME", root), mock.patch.object(
                fleet, "APPDATA", root
            ), mock.patch.object(fleet, "_jget") as request:
                result = fleet.api_claude()

        request.assert_not_called()
        self.assertNotIn("configured", result)
        self.assertEqual(result["plan"], "pro")
        self.assertEqual(result["source"], "desktop")
        self.assertEqual(
            result["metrics"], [fleet.M("Session", 12), fleet.M("Weekly", 15)]
        )

    def test_a_stale_desktop_sample_is_not_passed_off_as_a_reading(self):
        with tempfile.TemporaryDirectory() as temp:
            history = Path(temp) / "history.json"
            history.write_text(
                json.dumps(
                    {"samples": [{"t": (time.time() - 86400) * 1000, "u": {"fh": 12}}]}
                )
            )
            self.assertIsNone(fleet._claude_desktop_metrics(history))

    def test_desktop_samples_without_usage_are_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            history = Path(temp) / "history.json"
            now = time.time() * 1000
            history.write_text(
                json.dumps(
                    {
                        "samples": [
                            {"t": now - 600_000, "u": {"fh": 7, "sd": 8}},
                            {"t": now, "u": {}},
                        ]
                    }
                )
            )
            metrics, ts = fleet._claude_desktop_metrics(history)

        self.assertEqual(metrics, [fleet.M("Session", 7), fleet.M("Weekly", 8)])
        self.assertLess(time.time() - ts, 1200)

    def test_missing_codex_credentials_are_not_an_error_state(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            fleet, "HOME", Path(temp)
        ), mock.patch.dict(fleet.os.environ, {"CODEX_HOME": str(Path(temp) / "codex")}, clear=False):
            result = fleet.api_codex()

        self.assertFalse(result["configured"])
        self.assertFalse(result["known"])
        self.assertIn("credentials", result["err"])

    def test_an_installed_but_signed_out_grok_stays_listed(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / ".grok").mkdir()
            with mock.patch.object(fleet, "HOME", Path(temp)), mock.patch.object(
                fleet, "_load_last", return_value=None
            ):
                result = fleet.api_grok()

        self.assertTrue(result["known"])
        self.assertIn("signed out", result["err"])
        model = panel_model.build([result], time.time())
        self.assertEqual([(i["name"], i["label"]) for i in model["inactive"]], [("Grok", "Signed out")])

    def test_missing_grok_credentials_are_not_an_error_state(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            fleet, "HOME", Path(temp)
        ), mock.patch.object(
            fleet, "_load_last", return_value=None
        ):
            result = fleet.api_grok()

        self.assertFalse(result["configured"])
        self.assertFalse(result["known"])
        self.assertIn("credentials", result["err"])


class GrokAuthOwnershipTests(unittest.TestCase):
    def test_usage_poll_never_refreshes_or_rewrites_grok_cli_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth_file = root / ".grok" / "auth.json"
            auth_file.parent.mkdir()
            original = json.dumps(
                {
                    "https://auth.x.ai::account": {
                        "key": "cli-access",
                        "refresh_token": "cli-refresh",
                        "oidc_client_id": "cli-client",
                        "expires_at": "2099-01-01T00:00:00Z",
                    }
                },
                indent=2,
            ).encode()
            auth_file.write_bytes(original)

            def grok_api(url, token, *_args, **_kwargs):
                if "billing?format=credits" in url and token == "cli-access":
                    return 401, {}, {}
                return 200, {}, {"config": {}}

            refreshed = json.dumps(
                {"access_token": "fleet-refreshed", "expires_in": 3600}
            ).encode()
            with mock.patch.object(fleet, "HOME", root), mock.patch.object(
                fleet, "_load_last", return_value=None
            ), mock.patch.object(fleet, "_jget", side_effect=grok_api), mock.patch.object(
                fleet, "_http", return_value=(200, {}, refreshed)
            ) as oauth_refresh:
                result = fleet.api_grok()

            self.assertEqual(auth_file.read_bytes(), original)
            oauth_refresh.assert_not_called()
            self.assertIn("expired", result["err"])

    def test_an_expired_login_is_read_from_the_file_without_a_request(self):
        """auth.json states its own expiry; a doomed 401 round trip proves nothing."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth_file = root / ".grok" / "auth.json"
            auth_file.parent.mkdir()
            auth_file.write_text(
                json.dumps(
                    {
                        "https://auth.x.ai::account": {
                            "key": "cli-access",
                            "refresh_token": "cli-refresh",
                            "expires_at": "2026-08-16T00:00:00Z",
                        }
                    }
                )
            )
            with mock.patch.object(fleet, "HOME", root), mock.patch.object(
                fleet, "_load_last", return_value=None
            ), mock.patch.object(fleet, "_jget") as request:
                result = fleet.api_grok()

            request.assert_not_called()
            self.assertIn("expired", result["err"])
            self.assertIn("run grok", result["err"])


class AntigravitySummaryTests(unittest.TestCase):
    """Quota groups come from Antigravity's own summary, whatever it lists."""

    SUMMARY = {"response": {"groups": [
        {"displayName": "Gemini Models", "buckets": [
            {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.99, "resetTime": "2026-10-29T09:49:51Z"},
            {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.8, "resetTime": "2026-10-22T22:24:56Z"}]},
        {"displayName": "Claude and GPT models", "buckets": [
            {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 1, "resetTime": "2026-10-29T17:33:14Z"},
            {"bucketId": "3p-5h", "window": "5h", "resetTime": "2026-10-22T22:33:14Z"},
            {"bucketId": "3p-daily", "window": "daily", "remainingFraction": 0.5, "disabled": True}]},
        {"displayName": "Gemini 4 Argon", "buckets": [
            {"bucketId": "gemini-argon-wtu-weekly", "window": "weekly", "remainingFraction": 0.75,
             "resetTime": "2026-10-29T17:23:54Z"}]}]}}

    def test_every_group_and_window_is_kept_under_antigravity_names(self):
        metrics = fleet._agy_parse_summary(self.SUMMARY)

        self.assertEqual([(m["group"], m["window"], round(m["pct"])) for m in metrics],
                         [("Gemini", "weekly", 1), ("Gemini", "5h", 20), ("Claude & GPT", "weekly", 0),
                          ("Gemini 4 Argon", "weekly", 25)])
        # Labels stay what older readings used, so cached data and T3 keep working.
        self.assertEqual([m["label"] for m in metrics][:3], ["Weekly", "Session", "Claude Wk"])

    def test_the_flyout_groups_rows_the_way_antigravity_does(self):
        metrics = fleet._agy_parse_summary(self.SUMMARY)
        model = panel_model.build([{"agent": "Antigrav", "err": "", "metrics": metrics}], 1000.0, now=1000.0)

        groups = model["providers"][0]["groups"]
        self.assertEqual([(g["title"], [row["label"] for row in g["rows"]]) for g in groups],
                         [("Gemini", ["Weekly limit", "5-hour limit"]), ("Claude & GPT", ["Weekly limit"]),
                          ("Gemini 4 Argon", ["Weekly limit"])])
        self.assertEqual([m["short"] for m in model["providers"][0]["meters"]], ["Gemini", "Claude & GPT", "Gemini 4 Argon"])

    def test_the_cloud_is_asked_as_the_installed_antigravity_version(self):
        fleet._agy_user_agent.cache_clear()
        try:
            with tempfile.TemporaryDirectory() as temp, mock.patch.dict(fleet.os.environ, {"LOCALAPPDATA": temp}):
                exe = Path(temp) / "Programs" / "antigravity" / "Antigravity.exe"
                exe.parent.mkdir(parents=True)
                exe.write_bytes(b"")
                with mock.patch.object(fleet, "_exe_version", return_value="2.25.0"):
                    self.assertTrue(fleet._agy_user_agent().startswith("antigravity/2.25.0 windows/amd64 Fleet"))
                fleet._agy_user_agent.cache_clear()
                with mock.patch.object(fleet, "_exe_version", return_value=None):
                    self.assertIn(f"antigravity/{fleet.AGY_FALLBACK_VERSION} ", fleet._agy_user_agent())
        finally:
            fleet._agy_user_agent.cache_clear()


class GrokRenewalTests(unittest.TestCase):
    """The Grok CLI renews its own six-hour login; Fleet only asks it to run."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".grok" / "bin").mkdir(parents=True)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.auth = self.root / ".grok" / "auth.json"
        self.write_login("old-access", "2026-08-16T00:00:00Z")
        self.runs = []
        self.patches = [
            mock.patch.object(fleet, "HOME", self.root),
            mock.patch.object(fleet, "CACHE", self.cache),
            mock.patch.object(fleet, "_load_last", return_value=None),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.temp.cleanup()

    def write_login(self, key, expires):
        self.auth.write_text(json.dumps(
            {"https://auth.x.ai::account": {"key": key, "refresh_token": "cli-refresh", "expires_at": expires}}))

    def cli(self, renews=True):
        """A Grok CLI on disk whose run renews the login (or fails to)."""
        (self.root / ".grok" / "bin" / "grok.exe").write_bytes(b"")
        test = self

        class Run:
            def __init__(self, args, **_kwargs):
                test.runs.append(args)
                if renews:
                    test.write_login("new-access", "2099-01-01T00:00:00Z")

            def wait(self, timeout=None):
                return 0

        return mock.patch.object(fleet.subprocess, "Popen", Run)

    def test_an_expired_login_is_renewed_by_the_cli_and_then_read(self):
        def grok_api(url, token, *_args, **_kwargs):
            self.assertEqual(token, "new-access")
            return 200, {}, {"config": {"creditUsagePercent": 12,
                                        "currentPeriod": {"type": "WEEKLY", "end": "2099-01-01T00:00:00Z"}}}

        with self.cli(), mock.patch.object(fleet, "_jget", side_effect=grok_api):
            result = fleet.api_grok()

        self.assertEqual([args[1:] for args in self.runs], [["models"]])
        self.assertEqual(result["err"], "")
        self.assertEqual((result["metrics"][0]["label"], result["metrics"][0]["pct"]), ("Weekly", 12))

    def test_a_failed_renewal_backs_off_and_says_the_login_expired(self):
        with self.cli(renews=False), mock.patch.object(fleet, "_jget") as request:
            first = fleet.api_grok()
            second = fleet.api_grok()

        self.assertEqual(len(self.runs), 1)  # the second poll is inside the backoff
        request.assert_not_called()
        self.assertIn("run grok", first["err"])
        self.assertIn("run grok", second["err"])
        self.assertGreater(fleet._refresh_cooldown_left("grok_refresh"), 200)

    def test_without_the_cli_beside_the_login_nothing_is_run(self):
        with mock.patch.object(fleet.subprocess, "Popen") as popen, mock.patch.object(fleet, "_jget") as request:
            result = fleet.api_grok()

        popen.assert_not_called()
        request.assert_not_called()
        self.assertIn("expired", result["err"])

    def test_a_live_login_is_left_alone(self):
        self.write_login("live-access", "2099-01-01T00:00:00Z")
        with self.cli(), mock.patch.object(fleet, "_jget", return_value=(200, {}, {"config": {}})):
            fleet.api_grok()

        self.assertEqual(self.runs, [])


class GrokUsageTests(unittest.TestCase):
    _AUTH = {
        "https://auth.x.ai::account": {
            "key": "cli-access",
            "refresh_token": "cli-refresh",
            "oidc_client_id": "cli-client",
            # Far future on purpose: these cases cover live billing parsing, and
            # a fixed near-term date silently turned them into expiry tests.
            "expires_at": "2099-01-01T00:00:00Z",
        }
    }

    def _grok_home(self, root):
        auth_file = Path(root) / ".grok" / "auth.json"
        auth_file.parent.mkdir()
        auth_file.write_text(json.dumps(self._AUTH))
        return Path(root)

    def _api(self, credits, settings=None, billing=None):
        settings = settings or {"subscription_tier_display": "X Premium"}
        billing = billing or {
            "config": {"monthlyLimit": {"val": 0}, "used": {"val": 0}, "onDemandCap": {"val": 0}}
        }

        def grok_api(url, token, *_args, **_kwargs):
            self.assertEqual(token, "cli-access")
            if "format=credits" in url:
                return 200, {}, credits
            if url.endswith("/settings"):
                return 200, {}, settings
            if url.endswith("/billing"):
                return 200, {}, billing
            raise AssertionError(url)

        return grok_api

    def test_unified_billing_weekly_percent_stays_live(self):
        credits = {
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "end": "2026-09-06T23:49:28.623539+00:00",
                },
                "creditUsagePercent": 1.0,
                "onDemandCap": {"val": 0},
                "productUsage": [{"product": "GrokBuild", "usagePercent": 1.0}],
                "isUnifiedBillingUser": True,
            }
        }
        with tempfile.TemporaryDirectory() as temp:
            root = self._grok_home(temp)
            cache = Path(temp) / "cache"
            cache.mkdir()
            with mock.patch.object(fleet, "HOME", root), mock.patch.object(
                fleet, "CACHE", cache
            ), mock.patch.object(
                fleet, "_load_last", return_value=None
            ), mock.patch.object(fleet, "_jget", side_effect=self._api(credits)):
                result = fleet.api_grok()

        weekly = next(metric for metric in result["metrics"] if metric["label"] == "Weekly")
        self.assertEqual(result["plan"], "X Premium")
        self.assertEqual(weekly["pct"], 1.0)
        self.assertEqual(
            [metric["label"] for metric in result["metrics"] if metric.get("pct") is not None],
            ["Weekly"],
        )
        live, inactive = panel_wpf._partition_agents([result])
        self.assertEqual([agent["agent"] for agent in live], ["Grok"])
        self.assertEqual(inactive, [])

    def test_paygo_only_cache_is_not_treated_as_usage(self):
        credits = {
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "end": "2026-09-06T00:00:00+00:00",
                },
                "creditUsagePercent": 12.5,
                "onDemandCap": {"val": 0},
            }
        }
        paygo = [fleet.M("Pay-go", None, None, "off")]
        with tempfile.TemporaryDirectory() as temp:
            root = self._grok_home(temp)
            # A successful read is cached; keep it out of the real cache folder.
            with mock.patch.object(fleet, "HOME", root), mock.patch.object(
                fleet, "CACHE", root
            ), mock.patch.object(
                fleet, "_load_last", return_value=(paygo, "X Premium", "1m", 123.0)
            ), mock.patch.object(fleet, "_jget", side_effect=self._api(credits)):
                result = fleet.api_grok()

        weekly = next(metric for metric in result["metrics"] if metric["label"] == "Weekly")
        self.assertEqual(weekly["pct"], 12.5)
        self.assertNotEqual(result.get("err"), "soft cache")

    def test_paygo_only_success_does_not_poison_the_usage_cache(self):
        credits = {"config": {"onDemandCap": {"val": 0}}}
        with tempfile.TemporaryDirectory() as temp:
            root = self._grok_home(temp)
            cache = Path(temp) / "cache"
            cache.mkdir()
            with mock.patch.object(fleet, "HOME", root), mock.patch.object(
                fleet, "CACHE", cache
            ), mock.patch.object(
                fleet, "_load_last", return_value=None
            ), mock.patch.object(fleet, "_jget", side_effect=self._api(credits)):
                result = fleet.api_grok()

            self.assertFalse(_quota := fleet._quota_metrics(result["metrics"]))
            self.assertFalse((cache / "grok_last.json").exists())

    def test_an_unused_week_reads_as_zero_not_as_missing(self):
        # Live response from 2026-10-04: protobuf JSON drops creditUsagePercent=0.
        credits = {
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "end": "2026-10-04T23:49:28.623539+00:00",
                },
                "onDemandCap": {"val": 0},
                "isUnifiedBillingUser": True,
            }
        }
        metrics = fleet._grok_metrics(credits, {"config": {"monthlyLimit": {"val": 0}}})

        weekly = next(metric for metric in metrics if metric["label"] == "Weekly")
        self.assertEqual(weekly["pct"], 0.0)
        self.assertEqual(weekly["resets"], "2026-10-04T23:49:28.623539+00:00")
        self.assertTrue(fleet._quota_metrics(metrics))

    def test_wrapped_credit_percent_is_unwrapped(self):
        metrics = fleet._grok_metrics(
            {"config": {"creditUsagePercent": {"val": "8"}, "currentPeriod": {"type": "weekly"}}},
            {"config": {"monthlyLimit": {"val": 0}, "used": {"val": 0}}},
        )
        weekly = next(metric for metric in metrics if metric["label"] == "Weekly")
        self.assertEqual(weekly["pct"], 8.0)


class ClaudeUsageTests(unittest.TestCase):
    def test_a_throttled_usage_endpoint_asks_claude_code_itself(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cred = root / ".claude" / ".credentials.json"
            cred.parent.mkdir()
            cred.write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "access",
                            "refreshToken": "refresh",
                            "expiresAt": (time.time() + 3600) * 1000,
                            "subscriptionType": "max",
                        }
                    }
                )
            )
            printed = "Current session: 25% used\nCurrent week (all models): 50% used\n"
            with mock.patch.object(fleet, "HOME", root), mock.patch.object(
                fleet, "CACHE", root
            ), mock.patch.object(
                fleet, "_jget", return_value=(429, {"Retry-After": "120"}, {})
            ), mock.patch.object(
                fleet, "_run_claude_usage", return_value=(0, printed)
            ) as cli:
                result = fleet.api_claude()
                again = fleet.api_claude()
                backoff = fleet._refresh_cooldown_left("claude_usage")

            self.assertEqual([m["pct"] for m in result["metrics"]], [25.0, 50.0])
            self.assertEqual(result["err"], "")
            # Both throttles hold: no second usage request, no second CLI run.
            self.assertEqual(cli.call_count, 1)
            self.assertIn("cached", again["err"])
            self.assertGreaterEqual(backoff, 290)

    def test_force_bypasses_a_renewal_backoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cred = root / ".claude" / ".credentials.json"
            cred.parent.mkdir()
            cred.write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "expired",
                            "refreshToken": "refresh",
                            "expiresAt": (time.time() - 60) * 1000,
                        }
                    }
                )
            )
            (root / "claude_refresh_backoff").write_text(str(time.time() + 300))
            with mock.patch.object(fleet, "HOME", root), mock.patch.object(
                fleet, "CACHE", root
            ), mock.patch.object(
                fleet, "_renew_claude_via_cli", return_value=("failed", [])
            ) as renew:
                fleet.api_claude(force=True)

            renew.assert_called_once_with(cred, "expired", force=True)


class ClaudeSignInTests(unittest.TestCase):
    def test_sign_in_runs_the_cli_in_its_own_console_then_reports_back(self):
        finished = threading.Event()
        process = SimpleNamespace(wait=lambda: 0)
        with mock.patch.object(
            fleet, "_claude_cli", return_value=r"C:\Tools\claude.exe"
        ), mock.patch.object(fleet.subprocess, "Popen", return_value=process) as popen:
            self.assertTrue(fleet.claude_sign_in(on_exit=finished.set))

        self.assertTrue(finished.wait(5))
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], ["cmd.exe", "/c"])
        exe = command.index(r"C:\Tools\claude.exe")
        self.assertEqual(command[exe:exe + 3], [r"C:\Tools\claude.exe", "auth", "login"])
        self.assertEqual(
            popen.call_args.kwargs["creationflags"], fleet.subprocess.CREATE_NEW_CONSOLE
        )
        # Fleet never reads what the sign-in prints; it holds no pipe to it.
        self.assertNotIn("stdout", popen.call_args.kwargs)

    def test_sign_in_without_claude_code_installed_does_nothing(self):
        with mock.patch.object(fleet, "_claude_cli", return_value=None), mock.patch.object(
            fleet.subprocess, "Popen", side_effect=AssertionError("must not launch")
        ):
            self.assertFalse(fleet.claude_sign_in())

    def test_the_native_installer_location_is_found_without_path(self):
        with tempfile.TemporaryDirectory() as temp:
            native = Path(temp) / ".local" / "bin" / "claude.exe"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"")
            with mock.patch.object(fleet.shutil, "which", return_value=None), mock.patch.object(
                fleet, "HOME", Path(temp)
            ):
                self.assertEqual(fleet._claude_cli(), str(native))


class PublicReleaseTests(unittest.TestCase):
    def test_antigravity_uses_existing_access_token_without_a_copied_client(self):
        with mock.patch.object(fleet, "cache_get", return_value=None), mock.patch.object(
            fleet, "_agy_stored_tokens", return_value=("access", "refresh")
        ):
            self.assertEqual(fleet._agy_token(), "access")

        self.assertFalse(hasattr(fleet, "_AGY_CLIENT_SECRET"))
        self.assertFalse(hasattr(fleet, "_refresh_agy_token"))

    def test_refresh_debug_never_includes_response_bodies(self):
        source = Path(fleet.__file__).read_text(encoding="utf-8")

        self.assertNotIn('dbg("claude refresh http", st, bd', source)
        self.assertNotIn('dbg("grok refresh http", st, bd', source)

    def test_packaged_app_keeps_data_outside_the_install_folder(self):
        source = Path(fleet.__file__).read_text(encoding="utf-8")
        tray_source = Path(tray.__file__).read_text(encoding="utf-8")
        root = 'Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Fleet"'

        self.assertIn(root, source)
        self.assertIn(root, tray_source)
        self.assertIn('return [sys.executable, "--panel-host"]', tray_source)


class AntigravityRefreshTests(unittest.TestCase):
    def test_refresh_uses_runtime_client_metadata_and_fleet_cache_only(self):
        response = json.dumps({"access_token": "fresh-access", "expires_in": 300}).encode()
        with mock.patch.object(
            fleet, "_agy_oauth_client_candidates", return_value=[("client", "value")]
        ), mock.patch.object(
            fleet, "_http", return_value=(200, {}, response)
        ) as http, mock.patch.object(fleet, "cache_put") as cache_put:
            self.assertEqual(fleet._agy_refresh_access_token("refresh-value"), "fresh-access")

        args, kwargs = http.call_args
        self.assertEqual(args[0:2], ("POST", "https://oauth2.googleapis.com/token"))
        self.assertTrue(kwargs["form"])
        self.assertEqual(args[3]["grant_type"], "refresh_token")
        cache_put.assert_called_once()
        self.assertEqual(cache_put.call_args.args[0], "antigravity")

    def test_expired_cloud_token_refreshes_then_loads_project_before_quota(self):
        cloud = mock.Mock(side_effect=[
            (401, {}),
            (200, {"cloudaicompanionProject": "project", "paidTier": {"name": "Pro"}}),
            (200, {"groups": [{"buckets": [{
                "bucketId": "gemini-weekly", "remainingFraction": 0.25,
                "resetTime": "2026-08-02T00:00:00Z",
            }]}]}),
        ])
        with mock.patch.object(fleet, "_agy_ls_endpoint", return_value=None), mock.patch.object(
            fleet, "_agy_token", return_value="old-access"
        ), mock.patch.object(
            fleet, "_agy_stored_tokens", return_value=("old-access", "refresh-value")
        ), mock.patch.object(
            fleet, "_agy_refresh_access_token", return_value="new-access"
        ) as refresh, mock.patch.object(fleet, "_agy_cloud_call", cloud), mock.patch.object(
            fleet, "_save_last"
        ):
            out = fleet.api_antigravity()

        refresh.assert_called_once_with("refresh-value")
        self.assertEqual(cloud.call_args_list[1].args[0], "new-access")
        self.assertEqual(cloud.call_args_list[2].args[2], {"project": "project"})
        self.assertEqual(out["plan"], "Pro")
        self.assertEqual(out["metrics"][0]["label"], "Weekly")
        self.assertEqual(out["metrics"][0]["extra"], "")


OPENROUTER_PAYLOAD = {
    "data": {
        "total_credits": 20.0,
        "total_usage": 15.0,
    }
}


class PathResolverTests(unittest.TestCase):
    def test_walks_objects_indices_and_filters(self):
        payload = {
            "a": {"b": [{"v": 1}, {"v": 2}]},
            "data": [{"type": "TOKENS_LIMIT", "percentage": 41.5},
                     {"type": "TIME_LIMIT", "percentage": 3.0}],
        }

        self.assertEqual(apikeys.resolve(payload, "a.b[1].v"), 2)
        self.assertEqual(apikeys.resolve(payload, "data[type=TOKENS_LIMIT].percentage"), 41.5)

    def test_missing_or_wrong_shape_is_none_not_a_crash(self):
        payload = {"a": {"b": 1}}

        self.assertIsNone(apikeys.resolve(payload, "a.missing.deep"))
        self.assertIsNone(apikeys.resolve(payload, "a.b[0]"))
        self.assertIsNone(apikeys.resolve(payload, "data[type=nope].x"))
        self.assertIsNone(apikeys.resolve(payload, ""))


class MetricMappingTests(unittest.TestCase):
    def test_money_metric_formats_the_currency_it_was_given(self):
        payload = {"balance_infos": [{"total_balance": "110.00", "currency": "CNY"}]}
        specs = [{"label": "Balance", "kind": "money",
                  "value": "balance_infos[0].total_balance",
                  "currency": "balance_infos[0].currency"}]

        metrics = apikeys.build_metrics(payload, specs, fleet.M)

        self.assertEqual(metrics[0]["extra"], "¥110.00")
        self.assertIsNone(metrics[0]["pct"])

    def test_openrouter_shows_remaining_balance_without_a_meter(self):
        specs = apikeys.BUILTIN_BY_ID["openrouter"]["metrics"]

        metrics = apikeys.build_metrics(OPENROUTER_PAYLOAD, specs, fleet.M)

        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]["label"], "Balance")
        self.assertIsNone(metrics[0]["pct"])
        self.assertEqual(metrics[0]["extra"], "$5.00")  # remaining budget

    def test_a_remaining_count_is_converted_to_used(self):
        payload = {"quota": {"total": 200, "left": 50}}
        specs = [{"label": "Quota", "kind": "pct", "limit": "quota.total",
                  "remaining": "quota.left"}]

        self.assertEqual(apikeys.build_metrics(payload, specs, fleet.M)[0]["pct"], 75.0)

    def test_metrics_with_no_data_are_dropped_instead_of_showing_zero(self):
        specs = [{"label": "Balance", "kind": "money", "value": "nope"},
                 {"label": "Cap", "kind": "pct", "used": "a", "limit": "b"}]

        self.assertEqual(apikeys.build_metrics({}, specs, fleet.M), [])


class ProviderEnablementTests(unittest.TestCase):
    def test_a_key_in_the_environment_turns_its_provider_on(self):
        specs = apikeys.enabled_specs({}, {"OPENROUTER_API_KEY": "sk-or-test"})

        self.assertEqual([spec["id"] for spec in specs], ["openrouter"])

    def test_a_key_written_to_the_windows_registry_turns_its_provider_on(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            apikeys,
            "_get_registry_env",
            side_effect=lambda name: "registry-key" if name == "OPENROUTER_API_KEY" else None,
        ):
            specs = apikeys.enabled_specs({})

        self.assertEqual([spec["id"] for spec in specs], ["openrouter"])

    def test_probe_providers_stay_off_until_opted_in(self):
        environ = {"GROQ_API_KEY": "gsk-test"}  # pragma: allowlist secret

        self.assertEqual(apikeys.enabled_specs({}, environ), [])

        opted = apikeys.enabled_specs({"providers": {"groq": {"probe": True}}}, environ)

        self.assertEqual([spec["id"] for spec in opted], ["groq"])

    def test_config_can_disable_a_provider_or_point_at_another_variable(self):
        environ = {"OPENROUTER_API_KEY": "a", "WORK_KEY": "b", "DEEPSEEK_API_KEY": "c"}
        config = {"providers": {"openrouter": {"enabled": False},
                                "deepseek": {"env": "WORK_KEY"}}}

        specs = apikeys.enabled_specs(config, environ)

        self.assertEqual([spec["id"] for spec in specs], ["deepseek"])
        self.assertEqual(specs[0]["env"], ["WORK_KEY"])

    def test_custom_entries_need_a_url_and_at_least_one_metric(self):
        config = {"custom": [
            {"id": "proxy", "label": "Proxy", "url": "https://p.example/usage",
             "env": "PROXY_KEY", "metrics": [{"label": "Spend", "kind": "money", "value": "spend"}]},
            {"id": "broken", "url": "https://p.example/usage"},
            {"id": "alsobroken", "metrics": [{"label": "x", "value": "y"}]},
        ]}

        specs = apikeys.enabled_specs(config, {})

        self.assertEqual([spec["id"] for spec in specs], ["proxy"])


class ApiKeyCollectionTests(unittest.TestCase):
    def _http(self, status=200, payload=None, headers=None, record=None):
        def fake(method, url, request_headers=None, body=None, form=False, timeout=12):
            if record is not None:
                record.append((method, url, dict(request_headers or {}), body))
            return status, dict(headers or {}), json.dumps(payload or {}).encode()

        return fake

    def test_reads_a_usage_endpoint_into_fleet_metrics(self):
        record = []
        spec = dict(apikeys.BUILTIN_BY_ID["openrouter"], env=["OPENROUTER_API_KEY"])

        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):  # pragma: allowlist secret
            out = apikeys.collect(
                spec, self._http(payload=OPENROUTER_PAYLOAD, record=record), fleet._header, fleet.M
            )

        self.assertEqual(out["agent"], "OpenRouter")
        self.assertTrue(out["compact"])
        self.assertEqual(out["err"], "")
        self.assertEqual(out["metrics"][0]["extra"], "$5.00")
        self.assertEqual(record[0][0], "GET")
        self.assertEqual(record[0][2]["Authorization"], "Bearer sk-or-test")

    def test_openrouter_regular_key_falls_back_to_key_budget_endpoint(self):
        record = []
        responses = iter([
            (403, {}, b'{"error":"management key required"}'),
            (200, {}, json.dumps({"data": {
                "limit_remaining": 6.5,
                "usage_monthly": 3.0,
                "is_free_tier": False,
            }}).encode()),
        ])

        def http(method, url, request_headers=None, body=None, form=False, timeout=12):
            record.append((method, url, dict(request_headers or {}), body))
            return next(responses)

        spec = dict(apikeys.BUILTIN_BY_ID["openrouter"], env=["OPENROUTER_API_KEY"])
        with mock.patch.dict(
            "os.environ", {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False  # pragma: allowlist secret
        ):
            out = apikeys.collect(spec, http, fleet._header, fleet.M)

        self.assertEqual([call[1] for call in record], [
            "https://openrouter.ai/api/v1/credits",
            "https://openrouter.ai/api/v1/key",
        ])
        self.assertEqual(out["err"], "")
        self.assertEqual(out["plan"], "paid")
        self.assertEqual(out["metrics"][0]["label"], "Balance")
        self.assertEqual(out["metrics"][0]["extra"], "$6.50")

    def test_a_provider_without_its_key_reports_instead_of_calling_out(self):
        record = []
        spec = dict(apikeys.BUILTIN_BY_ID["deepseek"], env=["DEEPSEEK_API_KEY"])

        with mock.patch.dict("os.environ", {}, clear=True):
            out = apikeys.collect(spec, self._http(record=record), fleet._header, fleet.M)

        self.assertEqual(out["err"], "no key")
        self.assertEqual(record, [])

    def test_custom_entries_over_plain_http_are_refused(self):
        record = []
        spec = apikeys.normalize_custom({
            "id": "insecure", "url": "http://example.com/usage", "env": "K",
            "metrics": [{"label": "Spend", "kind": "money", "value": "spend"}],
        })

        with mock.patch.dict("os.environ", {"K": "secret"}, clear=False):
            out = apikeys.collect(spec, self._http(record=record), fleet._header, fleet.M)

        self.assertEqual(out["err"], "refused: not https")
        self.assertEqual(record, [])

    def test_a_self_hosted_proxy_may_use_loopback_http(self):
        spec = dict(apikeys.BUILTIN_BY_ID["litellm"], env=["LITELLM_API_KEY"])
        payload = {"info": {"spend": 3.0, "max_budget": 12.0}}
        record = []

        with mock.patch.dict(
            "os.environ",
            {"LITELLM_API_KEY": "sk-litellm", "LITELLM_BASE_URL": "http://127.0.0.1:4000"},  # pragma: allowlist secret
            clear=False,
        ):
            out = apikeys.collect(spec, self._http(payload=payload, record=record), fleet._header, fleet.M)

        self.assertEqual(record[0][1], "http://127.0.0.1:4000/key/info")
        self.assertEqual(out["metrics"][0]["pct"], 25.0)

    def test_an_error_status_never_invents_metrics(self):
        spec = dict(apikeys.BUILTIN_BY_ID["deepseek"], env=["DEEPSEEK_API_KEY"])

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=False):
            out = apikeys.collect(spec, self._http(status=401), fleet._header, fleet.M)

        self.assertEqual(out["metrics"], [])
        self.assertEqual(out["err"], "http 401")

    def test_probe_reads_rate_limit_headers_from_a_one_token_request(self):
        record = []
        spec = dict(apikeys.BUILTIN_BY_ID["groq"], env=["GROQ_API_KEY"], _settings={})
        headers = {
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": "900",
            "x-ratelimit-reset-requests": "1m30s",
            "x-ratelimit-limit-tokens": "6000",
            "x-ratelimit-remaining-tokens": "3000",
        }

        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "gsk"}, clear=False):  # pragma: allowlist secret
            out = apikeys.collect(
                spec, self._http(headers=headers, record=record), fleet._header, fleet.M
            )

        method, url, sent, body = record[0]
        self.assertEqual((method, url), ("POST", "https://api.groq.com/openai/v1/chat/completions"))
        self.assertEqual(body["max_tokens"], 1)
        self.assertEqual([m["label"] for m in out["metrics"]], ["Requests", "Tokens"])
        self.assertEqual(out["metrics"][0]["pct"], 10.0)
        self.assertEqual(out["metrics"][1]["pct"], 50.0)
        self.assertAlmostEqual(out["metrics"][0]["resets"] - time.time(), 90, delta=2)

    def test_anthropic_probe_uses_its_own_header_family_and_key_header(self):
        record = []
        spec = dict(apikeys.BUILTIN_BY_ID["anthropic"], env=["ANTHROPIC_API_KEY"], _settings={})
        headers = {
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-requests-remaining": "45",
            "anthropic-ratelimit-requests-reset": "2026-08-01T10:00:00Z",
        }

        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant"}, clear=False):  # pragma: allowlist secret
            out = apikeys.collect(
                spec, self._http(headers=headers, record=record), fleet._header, fleet.M
            )

        self.assertEqual(record[0][2]["x-api-key"], "sk-ant")
        self.assertNotIn("Authorization", record[0][2])
        self.assertEqual(out["metrics"][0]["pct"], 10.0)

    def test_a_broken_config_file_does_not_stop_polling(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / apikeys.CONFIG_NAME).write_text("{not json", encoding="utf-8")
            config, err = apikeys.load_config(root)

        self.assertEqual(config, {})
        self.assertIn(apikeys.CONFIG_NAME, err)


class PackagingTests(unittest.TestCase):
    REPO = Path(__file__).resolve().parent.parent

    def test_packaged_start_creates_its_data_root_from_nothing(self):
        """A portable build is the first thing to create LocalAppData\\Fleet."""
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, LOCALAPPDATA=temp, PYTHONPATH=str(self.REPO))
            proc = subprocess.run(
                [sys.executable, "-c", "import sys; sys.frozen = True; import fleet"],
                capture_output=True,
                env=env,
                text=True,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((Path(temp) / "Fleet" / ".cache").is_dir())

    def test_release_build_strips_the_tag_v_prefix(self):
        script = (self.REPO / "scripts" / "build-release.ps1").read_text(encoding="utf-8")

        self.assertIn("$Version = $Version -replace '^[vV]', ''", script)

    def test_release_build_bundles_provider_artwork_and_notices(self):
        provider_dir = self.REPO / "assets" / "providers"
        expected = {
            "antigravity.png",
            "claude.png",
            "codex.png",
            "grok.ico",
            "SOURCES.md",
        }
        script = (self.REPO / "scripts" / "build-release.ps1").read_text(encoding="utf-8")

        self.assertTrue(expected.issubset({path.name for path in provider_dir.iterdir()}))
        self.assertIn('--add-data "${providerAssets}:assets/providers"', script)
        self.assertIn("Copy-Item $thirdPartyNotices $out", script)

    def test_each_local_provider_artwork_path_resolves(self):
        for provider in ("Claude", "Codex", "Grok", "Antigravity"):
            with self.subTest(provider=provider):
                self.assertIsNotNone(tray._provider_asset_path(provider))

    def test_python_sources_do_not_contain_hidden_control_bytes(self):
        for path in (self.REPO / "tray.py", self.REPO / "panel_wpf.py"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                hidden = [char for char in text if ord(char) < 32 and char not in "\n\r\t"]
                self.assertEqual(hidden, [])

    def test_installer_guards_a_running_tray_and_removes_cached_state(self):
        setup = (self.REPO / "installer" / "Fleet.iss").read_text(encoding="utf-8")

        self.assertIn("AppMutex=Local\\fleet-tray", setup)
        self.assertIn('Type: filesandordirs; Name: "{app}\\.cache"', setup)
        self.assertIn('Source: "..\\LICENSE"', setup)
        self.assertIn('Source: "..\\THIRD_PARTY_NOTICES.md"', setup)

    def test_single_instance_uses_the_mutex_the_installer_waits_on(self):
        tray_source = Path(tray.__file__).read_text(encoding="utf-8")

        self.assertIn(r'claim_single_instance(r"Local\fleet-tray")', tray_source)

    def test_generated_icon_carries_every_shell_size(self):
        sys.path.insert(0, str(self.REPO / "scripts"))
        try:
            import build_assets
        finally:
            sys.path.pop(0)

        with tempfile.TemporaryDirectory() as temp:
            icon = Path(temp) / "fleet.ico"
            build_assets.write_icon(icon)
            from PIL import Image

            with Image.open(icon) as img:
                sizes = {size for size in img.info["sizes"]}

        self.assertEqual(sizes, {(s, s) for s in build_assets.ICON_SIZES})

    def test_version_resource_accepts_a_tag_shaped_version(self):
        sys.path.insert(0, str(self.REPO / "scripts"))
        try:
            import build_assets
        finally:
            sys.path.pop(0)

        self.assertEqual(build_assets._quad("v0.1.0"), (0, 1, 0, 0))
        self.assertEqual(build_assets._quad("1.2.3.4"), (1, 2, 3, 4))
        self.assertEqual(build_assets._quad("dev"), (0, 0, 0, 0))


class TrayAnchorTests(unittest.TestCase):
    def test_a_tray_click_hands_the_foreground_to_the_panel_host(self):
        # The shell grants the foreground to the icon's owner, not to the panel
        # host that draws the flyout; without the handoff it opened unfocused.
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            heartbeat = Path(temp) / "panel-heartbeat.json"
            heartbeat.write_text("{}")
            host = SimpleNamespace(pid=4242, poll=lambda: None)
            with mock.patch.object(tray, "_PANEL_PROCESS", host), mock.patch.object(
                tray, "PANEL_HEARTBEAT", heartbeat
            ), mock.patch.object(
                tray, "_allow_panel_foreground",
                side_effect=lambda pid: calls.append(("allow", pid)),
            ), mock.patch.object(
                tray, "_write_panel_signal",
                side_effect=lambda icon: calls.append(("signal", icon)),
            ):
                tray.ensure_panel_host(show=True, icon="icon")

        self.assertEqual(calls, [("allow", 4242), ("signal", "icon")])

    def test_show_signal_carries_the_notification_icon_rectangle(self):
        with tempfile.TemporaryDirectory() as temp:
            signal = Path(temp) / "panel.show"
            with mock.patch.object(tray, "PANEL_SIGNAL", signal), mock.patch.object(
                tray,
                "_tray_icon_rect",
                return_value=(1581, 1032, 1613, 1080),
            ):
                tray._write_panel_signal(object())

            payload = json.loads(signal.read_text())

        self.assertEqual(payload["anchor"], [1581, 1032, 1613, 1080])
        self.assertIsInstance(payload["ts"], int)


class TrayRefreshTests(unittest.TestCase):
    def test_refresh_now_bypasses_claude_backoff(self):
        icon = object()
        with mock.patch.object(
            tray, "poll", return_value=SAMPLE_AGENTS
        ) as poll, mock.patch.object(tray, "apply") as apply:
            tray.refresh(icon)

        poll.assert_called_once_with(block=False, force_claude=True)
        apply.assert_called_once_with(icon)


class SingleInstanceTests(unittest.TestCase):
    def test_a_second_instance_is_refused_while_the_first_holds_the_name(self):
        name = r"Local\fleet-test-" + str(time.time_ns())

        self.assertTrue(tray.claim_single_instance(name))
        self.assertFalse(tray.claim_single_instance(name))

    def test_unrelated_names_do_not_block_each_other(self):
        stamp = str(time.time_ns())

        self.assertTrue(tray.claim_single_instance(r"Local\fleet-test-a-" + stamp))
        self.assertTrue(tray.claim_single_instance(r"Local\fleet-test-b-" + stamp))


class CodexWindowTests(unittest.TestCase):
    def test_labels_windows_from_actual_duration(self):
        self.assertEqual(fleet._window_label({"limit_window_seconds": 18_000}), "Session")
        self.assertEqual(fleet._window_label({"limit_window_seconds": 604_800}), "Weekly")

    def test_unmetered_spark_limit_is_not_a_metric(self):
        self.assertIsNone(
            fleet._codex_additional_limit_metric(
                {"name": "GPT-5.3-Codex-Spark", "used_percent": None, "reset_at": 123}
            )
        )

    def test_metered_spark_limit_uses_a_compact_label(self):
        self.assertEqual(
            fleet._codex_additional_limit_metric(
                {"name": "GPT-5.3-Codex-Spark", "used_percent": 25, "reset_at": 123}
            ),
            fleet.M("Spark", 25, 123),
        )

    def test_zero_codex_credits_are_not_a_metric(self):
        self.assertIsNone(fleet._codex_credit_metric(0))
        self.assertIsNone(fleet._codex_credit_metric("0.00"))
        self.assertEqual(
            fleet._codex_credit_metric(6.5),
            fleet.M("Credits", None, None, "$6.5"),
        )


class PanelDismissalTests(unittest.TestCase):
    def test_click_inside_panel_is_not_outside(self):
        self.assertFalse(
            panel_wpf._point_outside((100, 100, 500, 600), (300, 350))
        )

    def test_click_beyond_each_panel_edge_is_outside(self):
        rect = (100, 100, 500, 600)
        for point in ((99, 350), (500, 350), (300, 99), (300, 600)):
            with self.subTest(point=point):
                self.assertTrue(panel_wpf._point_outside(rect, point))

    def test_tray_click_is_reserved_for_toggle(self):
        panel = (100, 100, 500, 600)
        tray_icon = (700, 900, 732, 932)

        self.assertFalse(panel_wpf._should_dismiss(panel, (716, 916), tray_icon))
        self.assertTrue(panel_wpf._should_dismiss(panel, (600, 700), tray_icon))


class PanelLayoutTests(unittest.TestCase):
    def test_bottom_right_position_preserves_shadow_clearance(self):
        left, top = panel_wpf._panel_origin(
            work_rect_px=(0, 0, 1920, 1040),
            cursor_px=(1900, 1000),
            window_size_dip=(360, 440),
            dpi_scale=(1.0, 1.0),
        )

        self.assertEqual((left, top), (1548.0, 588.0))

    def test_position_converts_monitor_pixels_to_wpf_dips(self):
        left, top = panel_wpf._panel_origin(
            work_rect_px=(0, 0, 2560, 1400),
            cursor_px=(2500, 1300),
            window_size_dip=(360, 440),
            dpi_scale=(1.5, 1.5),
        )

        self.assertAlmostEqual(left, 1334.6666666667)
        self.assertAlmostEqual(top, 481.3333333333)

    def test_panel_anchors_above_the_actual_notification_icon(self):
        left, top = panel_wpf._anchored_panel_origin(
            work_rect_px=(0, 0, 1920, 1032),
            anchor_rect_px=(1872, 1032, 1896, 1080),
            window_size_dip=(360, 438),
            dpi_scale=(1.0, 1.0),
        )

        self.assertEqual((left, top), (1550.0, 584.0))

    def test_panel_handles_a_left_docked_taskbar(self):
        left, top = panel_wpf._anchored_panel_origin(
            work_rect_px=(48, 0, 1920, 1080),
            anchor_rect_px=(0, 500, 48, 524),
            window_size_dip=(360, 438),
            dpi_scale=(1.0, 1.0),
        )

        self.assertEqual((left, top), (58.0, 293.0))


class CompactProviderTests(unittest.TestCase):
    def _rows(self, agent):
        markup = panel_wpf._card(agent, panel_wpf._theme(True))
        return markup.count('Tag="MetricUsage"') + markup.count('Tag="MetricAmount"')

    def test_an_api_provider_collapses_to_one_row(self):
        agent = {
            "agent": "OpenRouter",
            "compact": True,
            "metrics": [
                {"label": "Key cap", "pct": 75.0, "extra": "$15.00"},
                {"label": "Spend mo", "pct": None, "extra": "$9.50"},
                {"label": "Spend day", "pct": None, "extra": "$1.25"},
            ],
        }

        self.assertEqual(self._rows(agent), 1)
        # Currency extras take the value slot; the bar still encodes utilization.
        markup = panel_wpf._card(agent, panel_wpf._theme(True))
        self.assertIn("$15.00", markup)
        self.assertNotIn("75%", markup)

    def test_openrouter_balance_row_does_not_smash_name_and_label(self):
        agent = {
            "agent": "OpenRouter",
            "compact": True,
            "metrics": [{"label": "Balance", "pct": None, "extra": "$6.55"}],
        }
        markup = panel_wpf._card(agent, panel_wpf._theme(True))
        self.assertIn("OpenRouter", markup)
        self.assertIn("$6.55", markup)
        self.assertIn("ClipToBounds", markup)
        self.assertIn(f'MaxWidth="{panel_wpf.NAME_MAX_WIDTH:.0f}"', markup)
        self.assertIn('Tag="ProviderHeader"', markup)
        self.assertIn('Text="balance"', markup)

    def test_zero_money_rows_are_hidden_without_hiding_zero_usage(self):
        agents = [
            {
                "agent": "OpenRouter",
                "compact": True,
                "metrics": [{"label": "Balance", "pct": None, "extra": "$0.00"}],
            },
            {
                "agent": "Codex",
                "metrics": [
                    {"label": "Weekly", "pct": 0, "extra": ""},
                    {"label": "Credits", "pct": None, "extra": "$0"},
                ],
            },
        ]

        visible = panel_wpf._visible_agents(agents)
        self.assertEqual([agent["agent"] for agent in visible], ["Codex"])
        markup = panel_wpf._card(visible[0], panel_wpf._theme(True))
        self.assertIn('Text="week"', markup)
        self.assertNotIn('Text="credits"', markup)
        self.assertNotIn("$0", markup)

    def test_an_off_only_provider_moves_to_inactive(self):
        agents = [
            {
                "agent": "Grok",
                "metrics": [{"label": "Pay-go", "pct": None, "extra": "off"}],
                "err": "",
            }
        ]

        live, inactive = panel_wpf._partition_agents(agents)

        self.assertEqual(live, [])
        self.assertEqual([agent["agent"] for agent in inactive], ["Grok"])
        self.assertEqual(inactive[0]["_inactive_reason"], "off")

        markup = panel_wpf._xaml(
            agents,
            time.time(),
            True,
            SimpleNamespace(_until=lambda _: "", _age=lambda _: "8m"),
        )
        self.assertIn('Tag="InactiveProviders"', markup)
        self.assertIn('Text="Grok"', markup)
        self.assertIn('Text="off"', markup)
        self.assertNotIn('Tag="ProviderRow"', markup)

    def test_a_local_cli_provider_keeps_every_window(self):
        agent = {
            "agent": "Claude",
            "metrics": [
                {"label": "Session", "pct": 21.0, "extra": ""},
                {"label": "Weekly", "pct": 3.0, "extra": ""},
            ],
        }

        self.assertEqual(self._rows(agent), 2)

    def test_the_headline_row_is_the_worst_window_not_the_first(self):
        metrics = [{"label": "a", "pct": 10.0}, {"label": "b", "pct": 80.0}]

        self.assertEqual(panel_wpf._headline(metrics)["label"], "b")

    def test_a_balance_only_provider_shows_its_amount(self):
        agent = {"agent": "DeepSeek", "compact": True,
                 "metrics": [{"label": "Balance", "pct": None, "extra": "$4.00"}]}

        markup = panel_wpf._card(agent, panel_wpf._theme(True))

        self.assertEqual(self._rows(agent), 1)
        self.assertIn("$4.00", markup)

    def test_unknown_providers_get_a_compact_neutral_monogram(self):
        self.assertEqual(panel_wpf._mark_for("OpenRouter"), "OR")
        self.assertEqual(panel_wpf._mark_for("openrouter"), "OP")


class PanelSurfaceTests(unittest.TestCase):
    def _markup(self, light=True):
        return panel_wpf._xaml(
            SAMPLE_AGENTS,
            time.time(),
            light,
            SimpleNamespace(_until=lambda _: "3h"),
        )

    def test_text_tokens_stay_fully_opaque(self):
        # Desktop can modulate translucent greys into grey-on-grey. Foreground
        # tokens must stay solid even when the acrylic surface is translucent.
        for light in (True, False):
            for acrylic in (True, False):
                theme = panel_wpf._theme(light, acrylic=acrylic)
                for token in ("fg", "muted", "faint", "chip"):
                    with self.subTest(light=light, acrylic=acrylic, token=token):
                        self.assertRegex(theme[token], r"^#FF[0-9A-Fa-f]{6}$")

    def test_acrylic_theme_uses_translucent_surface(self):
        for light in (True, False):
            theme = panel_wpf._theme(light, acrylic=True)
            alpha = int(theme["surface"][1:3], 16)
            with self.subTest(light=light):
                self.assertLess(alpha, 255)
                self.assertGreaterEqual(alpha, 0x80)

    def test_opaque_theme_stays_solid(self):
        for light in (True, False):
            theme = panel_wpf._theme(light, acrylic=False)
            for token, value in sorted(theme.items()):
                with self.subTest(light=light, token=token):
                    self.assertRegex(value, r"^#FF[0-9A-Fa-f]{6}$")

    def test_the_window_uses_system_acrylic_not_layered_transparency(self):
        # System backdrops refuse WS_EX_LAYERED. AllowsTransparency must stay
        # False; the transparent window brush is cleared via HwndSource instead.
        markup = self._markup()

        self.assertIn('AllowsTransparency="False"', markup)
        self.assertIn('Background="Transparent"', markup)
        self.assertIn('x:Name="SlideTransform"', markup)
        for dead in ("BackdropHost", "BackdropBlur", "ImageBrush", "BlurEffect"):
            with self.subTest(element=dead):
                self.assertNotIn(dead, markup)

    def test_the_panel_stays_compact(self):
        # A small width increase buys full-width meters without becoming a window.
        self.assertLessEqual(panel_wpf.PANEL_WIDTH, 320)
        self.assertGreaterEqual(panel_wpf.PANEL_WIDTH, 300)
        self.assertIn(f'Width="{panel_wpf.PANEL_WIDTH:.0f}"', self._markup())

    def test_percentage_rows_show_their_reset_without_gaining_height(self):
        data = [
            {
                "agent": "Claude",
                "metrics": [{"label": "Session", "pct": 42, "resets": 123}],
                "err": "",
            }
        ]
        markup = panel_wpf._xaml(
            data,
            time.time(),
            True,
            SimpleNamespace(_until=lambda _: "3h"),
        )

        # The reset has its own column now, so it needs no separator glyph.
        self.assertIn('Text="3h"', markup)
        self.assertEqual(markup.count('Tag="MetricUsage"'), 1)
        self.assertIn(f'Height="{panel_wpf.ROW_HEIGHT:.0f}"', markup)
        self.assertIn(f'Height="{panel_wpf.BAR_HEIGHT:.0f}"', markup)

    def test_panel_uses_native_variable_type(self):
        self.assertIn('FontFamily="Segoe UI Variable Text"', self._markup())

    def test_mixed_freshness_shows_real_cache_age(self):
        data = [
            {
                "agent": "Claude",
                "metrics": [{"label": "Session", "pct": 42, "resets": None}],
                "err": "offline",
                "cached_at": 100.0,
            },
            {
                "agent": "Codex",
                "metrics": [{"label": "Session", "pct": 12, "resets": None}],
                "err": "",
            },
        ]
        markup = panel_wpf._xaml(
            data,
            999.0,
            True,
            SimpleNamespace(_until=lambda _: "", _age=lambda _: "8m"),
        )

        self.assertIn("updated", markup)
        self.assertEqual(markup.count('Tag="ProviderRow"'), 2)
        self.assertIn('ToolTip="offline · 8m old"', markup)

    def test_cached_readings_stay_visible_and_are_labelled(self):
        data = [
            {
                "agent": "Claude",
                "metrics": [{"label": "Session", "pct": 42, "resets": None}],
                "err": "",
            },
            {
                "agent": "Codex",
                "metrics": [{"label": "Session", "pct": 12, "resets": None}],
                "err": "offline",
                "cached_at": 100.0,
            },
            {"agent": "Grok", "metrics": [], "err": "offline"},
        ]
        markup = panel_wpf._xaml(
            data,
            999.0,
            True,
            SimpleNamespace(_until=lambda _: "", _age=lambda _: "8m"),
        )

        self.assertEqual(markup.count('Tag="ProviderRow"'), 2)
        self.assertEqual(markup.count('Tag="InactiveProvider"'), 1)
        self.assertIn('ToolTip="offline · 8m old"', markup)
        self.assertIn('ToolTip="offline"', markup)
        self.assertNotIn('Tag="ProviderStatus"', markup)

    def test_not_connected_providers_are_neutral_and_do_not_poison_health(self):
        data = [
            {
                "agent": "Claude",
                "metrics": [],
                "err": "no Claude Code credentials",
                "configured": False,
                "known": True,
            },
            {
                "agent": "Codex",
                "metrics": [{"label": "Session", "pct": 12, "resets": None}],
                "err": "",
            },
        ]
        markup = panel_wpf._xaml(
            data,
            999.0,
            True,
            SimpleNamespace(_until=lambda _: "", _age=lambda _: "8m"),
        )

        self.assertIn("updated", markup)
        self.assertNotIn("1/2 live", markup)
        self.assertIn('Tag="InactiveProviders"', markup)
        self.assertIn('Text="1 inactive"', markup)
        self.assertIn('ToolTip="no Claude Code credentials"', markup)
        self.assertNotIn(panel_wpf.GRAD["r"], markup)
        self.assertLess(markup.index('Text="Codex"'), markup.index('Text="Claude"'))

    def test_completely_absent_providers_are_omitted(self):
        data = [
            {
                "agent": "Claude",
                "metrics": [],
                "err": "no Claude Code credentials",
                "configured": False,
                "known": False,
            },
            {
                "agent": "Codex",
                "metrics": [{"label": "Session", "pct": 12, "resets": None}],
                "err": "",
            },
        ]
        markup = panel_wpf._xaml(
            data,
            999.0,
            True,
            SimpleNamespace(_until=lambda _: "", _age=lambda _: "8m"),
        )

        self.assertIn('Text="Codex"', markup)
        self.assertNotIn('Text="Claude"', markup)
        self.assertNotIn('Tag="InactiveProviders"', markup)

    def test_panel_uses_provider_artwork(self):
        markup = self._markup()

        # Provider-owned identifiers from the documented assets directory.
        self.assertIn("<Image", markup)
        self.assertIn("claude.png", markup)
        self.assertIn("codex.png", markup)

    def test_each_metric_occupies_exactly_one_fixed_height_row(self):
        # Each provider owns its identity once, above one visual row per metric.
        markup = self._markup()
        rows = (
            markup.count('Tag="MetricUsage"')
            + markup.count('Tag="MetricAmount"')
            + markup.count('Tag="MetricCompact"')
        )
        metrics = sum(len(agent["metrics"]) for agent in SAMPLE_AGENTS)

        self.assertEqual(rows, metrics)
        self.assertEqual(markup.count('Tag="ProviderHeader"'), len(SAMPLE_AGENTS))

    def test_each_live_provider_floats_on_its_own_glass_platter(self):
        # Glass reads as glass where the blur shows between panes; one shared
        # slab with dividers hid it behind a single opaque-looking card.
        markup = self._markup()

        self.assertEqual(markup.count('Tag="LiveSurface"'), 1)
        self.assertEqual(markup.count('Tag="ProviderRow"'), len(SAMPLE_AGENTS))
        self.assertNotIn('Tag="ProviderDivider"', markup)

    def test_a_warm_shell_accent_never_becomes_the_calm_meter_colour(self):
        # Amber and coral mean pressure. A user whose Windows accent is red must
        # not see every low quota painted as a warning.
        for rgb in ((0xE8, 0x11, 0x23), (0xFF, 0xB9, 0x00)):
            with self.subTest(rgb=rgb):
                self.assertTrue(panel_wpf._is_warm(rgb))
        for rgb in ((0x75, 0xB7, 0xEA), (0x10, 0x89, 0x3E), (0x74, 0x4D, 0xA9)):
            with self.subTest(rgb=rgb):
                self.assertFalse(panel_wpf._is_warm(rgb))

    def test_the_meter_ramp_keeps_pressure_distinct_from_the_accent(self):
        with mock.patch.dict(panel_wpf._ACCENT, {"pair": None}):
            ramp = panel_wpf._meter(False)

        self.assertEqual(ramp["meter_g"], panel_wpf.METER_FALLBACK["dark"])
        self.assertEqual(ramp["meter_y"], panel_wpf.METER_PRESSURE["y"])
        self.assertEqual(ramp["meter_r"], panel_wpf.METER_PRESSURE["r"])

    def test_a_quota_under_pressure_colours_its_value_too(self):
        theme = panel_wpf._theme(False)
        calm = panel_wpf._metric_row({"label": "week", "pct": 20}, theme)
        critical = panel_wpf._metric_row({"label": "week", "pct": 92}, theme)

        self.assertIn(f'Foreground="{theme["fg"]}"', calm)
        self.assertIn(f'Foreground="{theme["meter_r"]}"', critical)
        # The reading stays legible without the meter being read at all.
        self.assertEqual(critical.count(theme["meter_r"]), 2)

    def test_the_disclosure_replaces_the_stock_expander_chrome(self):
        markup = panel_wpf._xaml(
            SAMPLE_AGENTS + [{"agent": "Grok", "metrics": [], "err": "offline", "known": True}],
            time.time(),
            False,
            SimpleNamespace(_until=lambda _: "3h", _age=lambda _: "8m"),
        )

        self.assertIn('Tag="InactiveProviders"', markup)
        # Stock WPF draws a circular chevron button floating outside the surface.
        self.assertIn("<Expander.Template>", markup)
        self.assertIn(panel_wpf.GLYPH_CHEVRON_DOWN, markup)
        self.assertIn(panel_wpf.GLYPH_CHEVRON_UP, markup)

    def test_a_quota_row_spends_the_content_width_exactly(self):
        # One line per quota: name, meter, value, reset. The columns are declared
        # in DIPs, so a track that does not match its column silently clips or
        # leaves a gap at the end of every meter.
        self.assertEqual(
            panel_wpf.CONTENT_WIDTH,
            panel_wpf.PANEL_WIDTH
            - 2 * panel_wpf.PANEL_BORDER
            - 2 * panel_wpf.WINDOW_PADDING
            - 2 * panel_wpf.CARD_BORDER
            - 2 * panel_wpf.CARD_PADDING,
        )
        self.assertEqual(
            panel_wpf.LABEL_COLUMN
            + panel_wpf.METER_COLUMN
            + panel_wpf.VALUE_COLUMN
            + panel_wpf.RESET_COLUMN,
            panel_wpf.CONTENT_WIDTH,
        )
        self.assertEqual(
            panel_wpf.BAR_TRACK,
            panel_wpf.METER_COLUMN
            - panel_wpf.METER_INSET_LEFT
            - panel_wpf.METER_INSET_RIGHT,
        )

    def test_a_bar_never_exceeds_its_track_and_zero_stays_empty(self):
        for pct in (0, 1, 41, 88, 100):
            with self.subTest(pct=pct):
                self.assertLessEqual(panel_wpf._bar_fill(pct), panel_wpf.BAR_TRACK)
        self.assertEqual(panel_wpf._bar_fill(0), 0.0)
        self.assertEqual(panel_wpf._bar_fill(100), panel_wpf.BAR_TRACK)

    def test_amount_rows_do_not_draw_empty_meters(self):
        usage = panel_wpf._metric_row({"label": "week", "pct": 42}, panel_wpf._theme(True))
        amount = panel_wpf._metric_row(
            {"label": "balance", "pct": None, "extra": "$6.50"},
            panel_wpf._theme(True),
        )

        self.assertIn('Tag="MetricUsage"', usage)
        self.assertIn(f'Height="{panel_wpf.BAR_HEIGHT:.0f}"', usage)
        self.assertIn('Tag="MetricAmount"', amount)
        self.assertNotIn(f'Height="{panel_wpf.BAR_HEIGHT:.0f}"', amount)

    def test_antigravity_quotas_are_always_visible_in_compact_light_and_dark(self):
        agent = {
            "agent": "Antigrav",
            "compact": True,
            "metrics": [
                {"label": "Weekly", "pct": 0, "_reset_text": "6d"},
                {"label": "Session", "pct": 17, "_reset_text": "4h"},
                {"label": "Claude Wk", "pct": 54, "_reset_text": "5d"},
                {"label": "Claude", "pct": 92, "_reset_text": "1h"},
            ],
        }

        for light in (True, False):
            with self.subTest(light=light):
                markup = panel_wpf._card(agent, panel_wpf._theme(light))

                self.assertEqual(markup.count('Tag="MetricCompact"'), 4)
                self.assertEqual(markup.count('Tag="MetricCompact" Height="28"'), 4)
                self.assertEqual(markup.count('Tag="MetricUsage"'), 0)
                self.assertEqual(markup.count('Tag="MetricAmount"'), 0)
                self.assertIn('Tag="QuotaGrid" Height="40"', markup)
                self.assertIn('Tag="QuotaValues" Grid.Row="1" Columns="4"', markup)
                self.assertEqual(markup.count('Tag="QuotaGroup"'), 2)
                self.assertEqual(markup.count('Tag="QuotaWindow"'), 4)
                self.assertNotIn("Expander", markup)
                self.assertNotIn("ZeroUsage", markup)
                self.assertIn('Text="gemini"', markup)
                self.assertIn('Text="claude"', markup)
                self.assertEqual(markup.count('Text="week"'), 2)
                self.assertEqual(markup.count('Text="session"'), 2)
                for value in ("0%", "17%", "54%", "92%"):
                    self.assertIn(f'Text="{value}"', markup)

    def test_platters_are_lit_glass_rather_than_flat_cards(self):
        # A pane of glass is brightest along its top edge and falls away below.
        for light in (True, False):
            theme = panel_wpf._theme(light)
            markup = panel_wpf._card(SAMPLE_AGENTS[0], theme)
            with self.subTest(light=light):
                self.assertIn(f'<GradientStop Color="{theme["card_hi"]}" Offset="0"/>', markup)
                self.assertIn(f'<GradientStop Color="{theme["card_bg"]}" Offset="1"/>', markup)
                self.assertIn(f'<GradientStop Color="{theme["rim_top"]}" Offset="0"/>', markup)
                self.assertIn(f'<GradientStop Color="{theme["rim_bot"]}" Offset="1"/>', markup)

    def test_a_signed_out_claude_offers_a_one_click_sign_in(self):
        for err in (fleet.CLAUDE_SIGNED_OUT, fleet.CLAUDE_EXPIRED):
            data = [
                {"agent": "Claude", "metrics": [], "err": err, "configured": False, "known": True},
                {"agent": "Codex", "metrics": [{"label": "Weekly", "pct": 12, "resets": None}], "err": ""},
            ]
            markup = panel_wpf._content_xaml(
                data, time.time(), True, SimpleNamespace(_until=lambda _: "", _age=lambda _: "8m")
            )
            with self.subTest(err=err):
                self.assertEqual(markup.count('Tag="ClaudeSignIn"'), 1)
                self.assertIn('Text="Sign in"', markup)
                self.assertIn("claude auth login", markup)

    def test_only_a_signed_out_claude_gets_the_sign_in_button(self):
        data = [
            {"agent": "Grok", "metrics": [], "err": "offline", "known": True},
            {"agent": "Claude", "metrics": [], "err": "offline", "known": True},
            {"agent": "Claude", "metrics": [], "err": fleet.CLAUDE_SIGNED_OUT, "known": True,
             "cached_at": 100.0},
        ]
        markup = panel_wpf._content_xaml(
            data, time.time(), True, SimpleNamespace(_until=lambda _: "", _age=lambda _: "8m")
        )

        self.assertNotIn("ClaudeSignIn", markup)

    def test_accent_acrylic_is_the_default_material(self):
        # On build 29648 the official system backdrop drew a flat fill even for
        # a focused flyout; accent acrylic kept blurring.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLEET_MATERIAL", None)
            os.environ.pop("FLEET_OPAQUE", None)
            self.assertEqual(panel_wpf._material(), "accent")
        for value, expected in (("system", "system"), ("OPAQUE", "opaque"), ("bogus", "accent")):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"FLEET_MATERIAL": value}):
                self.assertEqual(panel_wpf._material(), expected)
        with mock.patch.dict(os.environ, {"FLEET_OPAQUE": "1", "FLEET_MATERIAL": "accent"}):
            self.assertEqual(panel_wpf._material(), "opaque")

    def test_the_accent_tint_stays_translucent(self):
        for light in (True, False):
            alpha = panel_wpf._accent_tint(light) >> 24
            with self.subTest(light=light):
                self.assertGreater(alpha, 0)
                self.assertLess(alpha, 0xC0)

    def test_the_glass_is_lit_even_when_the_flyout_lacks_focus(self):
        # DWM draws an inactive system backdrop as a flat grey fill; the panel
        # host cannot always win the foreground from a tray click.
        sent = []
        panel_wpf._light_backdrop(4242, send=lambda *args: sent.append(args))
        panel_wpf._light_backdrop(0, send=lambda *args: sent.append(("unexpected",)))

        self.assertEqual(len(sent), 1)
        hwnd, message, wparam, lparam = sent[0]
        self.assertEqual(hwnd.value, 4242)
        self.assertEqual((message, wparam, lparam), (0x0086, 1, 0))
        source = Path(panel_wpf.__file__).read_text(encoding="utf-8")
        opening = source.split("def begin_open_slide():", 1)[1].split("def ", 1)[0]
        self.assertIn("light_backdrop()", opening)
        self.assertIn("_defer_to_dispatcher(window.Dispatcher, light_backdrop)", source)

    def test_the_provider_name_appears_once_per_provider(self):
        markup = self._markup()

        for agent in SAMPLE_AGENTS:
            name = "Antigravity" if agent["agent"] == "Antigrav" else agent["agent"]
            with self.subTest(agent=name):
                self.assertEqual(markup.count(f'Text="{name}"'), 1)


class PanelSilhouetteTests(unittest.TestCase):
    def test_every_silhouette_layer_uses_the_same_corner_radius(self):
        # DWM rounds the window to PANEL_CORNER_RADIUS. Drawing the rim or the
        # clip at a different radius puts several curves on screen at once, which
        # is what made the corners look mislayered and then black.
        markup = panel_wpf._xaml(
            [],
            time.time(),
            True,
            SimpleNamespace(_until=lambda _: ""),
        )
        radius = f'CornerRadius="{panel_wpf.PANEL_CORNER_RADIUS:.0f}"'

        self.assertIn(f'x:Name="GlassClip" {radius}', markup)
        # One rounded layer only. The decorative rim, inner stroke and highlight
        # each drew their own arc, which is how three curves ended up on screen.
        self.assertGreaterEqual(markup.count("CornerRadius="), 1)


class PanelMaterialTests(unittest.TestCase):
    def test_opaque_fallback_uses_tokens_that_exist(self):
        source = Path(panel_wpf.__file__).read_text(encoding="utf-8")

        self.assertIn('opaque["border_top"]', source)
        self.assertNotIn('opaque["border"]', source)
        self.assertIn('acrylic=chrome["acrylic"]', source)

    def test_rapid_toggle_reverses_closing_state(self):
        calls = []
        window = SimpleNamespace(IsVisible=True)
        closing = {"value": True}

        def begin_open_slide():
            closing["value"] = False
            calls.append("open_slide")

        def close_animated():
            calls.append("close_animated")

        # Simulate present when window is visible and closing
        anchor = {"rect": None}
        def present(anchor_rect=None):
            anchor["rect"] = anchor_rect
            if window.IsVisible:
                if closing["value"]:
                    begin_open_slide()
                else:
                    close_animated()
                return

        present()
        self.assertFalse(closing["value"])
        self.assertEqual(calls, ["open_slide"])

    def test_nothing_in_the_panel_reports_a_size_from_a_bitmap(self):
        # History: the backdrop was an <Image>, which reports a desired size. Under
        # SizeToContent="Height" each captured frame fed its own height back into
        # the window, growing it 421 -> 2540 DIP. No element above the content may
        # size itself from an image again.
        markup = panel_wpf._xaml(
            [],
            time.time(),
            True,
            SimpleNamespace(_until=lambda _: ""),
        )

        self.assertNotIn("<Image ", markup.split("ContentRoot")[0])
        self.assertIn('SizeToContent="Height"', markup)

    def test_hidden_host_stays_invisible_until_the_backdrop_is_ready(self):
        markup = panel_wpf._xaml(
            [],
            time.time(),
            True,
            SimpleNamespace(_until=lambda _: ""),
        )

        self.assertIn('Topmost="True" Opacity="0"', markup)
        self.assertIn('<StackPanel x:Name="ContentRoot" Opacity="1">', markup)

    def test_foreground_is_taken_on_its_own_dispatcher_frame(self):
        """0x8001010d: a COM call-out inside an input-sync dispatch kills the host."""
        calls = []
        queued = []
        window = SimpleNamespace(Dispatcher=object())
        window.Opacity = 0.0
        window.UpdateLayout = lambda: None
        window.Show = lambda: calls.append("show")
        window.Activate = lambda: calls.append("activate")

        with mock.patch.object(
            panel_wpf,
            "_defer_to_dispatcher",
            lambda dispatcher, callback: queued.append((dispatcher, callback)),
        ):
            panel_wpf._present_prepared_window(
                window, None, lambda anchor: None, lambda: None, lambda: None
            )

        self.assertEqual(calls, ["show"])
        self.assertEqual(len(queued), 1)
        self.assertIs(queued[0][0], window.Dispatcher)
        queued[0][1]()
        self.assertEqual(calls, ["show", "activate"])

    def test_a_named_local_source_is_not_labelled_as_a_fleet_cache(self):
        data = [
            {
                "agent": "Claude",
                "plan": "pro",
                "err": "",
                "source": "desktop",
                "cached_at": time.time() - 720,
                "metrics": [fleet.M("Session", 12), fleet.M("Weekly", 15)],
            }
        ]
        markup = panel_wpf._content_xaml(data, time.time(), True, fleet)

        self.assertIn('Text="desktop · 12m old"', markup)
        self.assertNotIn('Text="cached · 12m old"', markup)

    def test_prepared_window_is_final_before_show(self):
        calls = []
        window = SimpleNamespace()
        window.Opacity = 0.0
        window.UpdateLayout = lambda: calls.append("layout")
        window.Show = lambda: calls.append(("show", window.Opacity))
        window.Activate = lambda: calls.append("activate")

        def prepare():
            window.Opacity = 1.0
            calls.append("prepare")

        panel_wpf._present_prepared_window(
            window,
            (1, 2, 3, 4),
            lambda anchor: calls.append(("position", anchor)),
            prepare,
            lambda: calls.append("slide"),
        )

        self.assertEqual(
            calls,
            [
                ("position", (1, 2, 3, 4)),
                "layout",
                "prepare",
                ("show", 1.0),
                "slide",
                "activate",
            ],
        )


if __name__ == "__main__":
    unittest.main()
