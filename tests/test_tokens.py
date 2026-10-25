"""Fleet reads other apps' logins; it must never sign them out or hammer them."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import fleet


def _codex_home(root, token="access-1", refresh="refresh-1"):
    auth = Path(root) / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(json.dumps({"tokens": {"access_token": token, "refresh_token": refresh, "account_id": "acct"}}))
    return Path(root)


class CodexTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        patches = [
            mock.patch.object(fleet, "CACHE", self.cache),
            mock.patch.dict(fleet.os.environ, {"CODEX_HOME": ""}),
            mock.patch.object(fleet, "REFRESH", False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.temp.cleanup)

    def test_an_expired_codex_token_is_never_refreshed_behind_the_cli(self):
        # OpenAI rotates refresh tokens: refreshing in memory without saving the
        # result retires the token Codex CLI holds and signs it out.
        _codex_home(self.root)
        with mock.patch.object(fleet, "HOME", self.root), mock.patch.object(
            fleet, "_jget", return_value=(401, {}, {})
        ), mock.patch.object(fleet, "_http", side_effect=AssertionError("refresh attempted")):
            result = fleet.api_codex()

        self.assertIn("run codex", result["err"])

    def test_a_rejected_token_is_not_sent_again_until_codex_replaces_it(self):
        _codex_home(self.root, token="dead")
        calls = []

        def usage(url, token, *args, **kwargs):
            calls.append(token)
            return (401, {}, {}) if token == "dead" else (200, {}, {"plan_type": "pro", "rate_limit": {
                "primary_window": {"used_percent": 7, "limit_window_seconds": 604800, "reset_at": 1}}})

        with mock.patch.object(fleet, "HOME", self.root), mock.patch.object(fleet, "_jget", side_effect=usage):
            fleet.api_codex()
            fleet.api_codex()
            self.assertEqual(calls, ["dead"])
            _codex_home(self.root, token="fresh")
            result = fleet.api_codex()

        self.assertEqual(calls, ["dead", "fresh"])
        self.assertEqual(result["metrics"][0]["pct"], 7)

    def test_the_dead_token_note_never_contains_the_token(self):
        fleet._mark_token_dead("codex", "secret-access-token")

        stored = (self.cache / "codex_dead_token").read_text()
        self.assertNotIn("secret", stored)
        self.assertTrue(fleet._token_dead("codex", "secret-access-token"))
        self.assertFalse(fleet._token_dead("codex", "another-token"))


CLI_USAGE = """You are currently using your subscription to power your Claude Code usage

Current session: 21% used \u00b7 resets Oct 15, 1:20am (Asia/Tokyo)
Current week (all models): 75% used \u00b7 resets Oct 16, 8am (Asia/Tokyo)
Current week (Fable): 0% used \u00b7 resets Oct 16, 8am (Asia/Tokyo)

What's contributing to your limits usage?
"""


def _claude_home(root, token="old", refresh="old-refresh", expires=1, **extra):
    cred = Path(root) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    oauth = {"accessToken": token, "refreshToken": refresh, "expiresAt": expires,
             "scopes": ["user:inference", "user:profile"], "subscriptionType": "pro"}
    oauth.update(extra)
    cred.write_text(json.dumps({"mcpOAuth": {"kept": True}, "claudeAiOauth": oauth}))
    return cred


class ClaudeRenewalTests(unittest.TestCase):
    """Claude Code renews its own login; Fleet only reads the result."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.calls = []
        patches = [
            mock.patch.object(fleet, "CACHE", self.cache),
            mock.patch.object(fleet, "HOME", self.root),
            mock.patch.object(fleet, "_claude_cli", return_value=r"C:\Tools\claude.exe"),
            mock.patch.object(fleet, "_claude_from_desktop", return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.temp.cleanup)

    def _claude_code(self, renews_to=None, code=0, text=CLI_USAGE):
        """A stand-in Claude Code: optionally saves a renewed login, prints /usage."""
        def start(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            if renews_to:
                _claude_home(self.root, token=renews_to, refresh="new-refresh",
                             expires=(time.time() + 28800) * 1000)
            proc = mock.Mock(returncode=code)
            proc.communicate.return_value = (text.encode("utf-8"), b"")
            return proc
        return mock.patch.object(fleet.subprocess, "Popen", side_effect=start)

    def test_claude_code_renews_its_own_login_and_fleet_calls_no_token_endpoint(self):
        cred = _claude_home(self.root)
        with self._claude_code(renews_to="new"), mock.patch.object(
            fleet, "_http", side_effect=AssertionError("Fleet sent a request")
        ):
            status, metrics = fleet._renew_claude_via_cli(cred, "old")

        self.assertEqual(status, "renewed")
        self.assertEqual([(m["label"], m["pct"]) for m in metrics],
                         [("Session", 21.0), ("Weekly", 75.0), ("Fable", 0.0)])
        self.assertEqual(json.loads(cred.read_text())["mcpOAuth"], {"kept": True})

    def test_the_run_is_hidden_local_and_leaves_nothing_behind(self):
        cred = _claude_home(self.root)
        with self._claude_code(renews_to="new"):
            fleet._renew_claude_via_cli(cred, "old")

        cmd, kwargs = self.calls[0]
        self.assertEqual(cmd[1:3], ["-p", "/usage"])
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--settings") + 1], '{"disableAllHooks":true}')
        self.assertEqual(kwargs["creationflags"], getattr(fleet.subprocess, "CREATE_NO_WINDOW", 0))
        self.assertIs(kwargs["stdin"], fleet.subprocess.DEVNULL)
        self.assertEqual(Path(kwargs["cwd"]), self.cache / "claude-probe")

    def test_the_flyout_shows_what_claude_code_printed_after_renewing(self):
        _claude_home(self.root)
        with self._claude_code(renews_to="new"), mock.patch.object(
            fleet, "_jget", side_effect=AssertionError("a second usage request")
        ):
            result = fleet.api_claude()

        self.assertEqual(result["err"], "")
        self.assertEqual([m["pct"] for m in result["metrics"]], [21.0, 75.0, 0.0])

    def test_a_failed_renewal_backs_off_harder_each_time(self):
        cred = _claude_home(self.root)
        waits = []
        with self._claude_code(code=1, text="Invalid API key"), self.assertLogs("fleet.runtime") as logs:
            for _ in range(4):
                self.assertEqual(fleet._renew_claude_via_cli(cred, "old", force=True)[0], "failed")
                waits.append(round(fleet._refresh_cooldown_left("claude_refresh") / 60))
        self.assertEqual(waits, [5, 10, 20, 40])
        self.assertIn("renewal failed", logs.output[0])
        with self._claude_code(renews_to="new"):
            self.assertEqual(fleet._renew_claude_via_cli(cred, "old", force=True)[0], "renewed")
        self.assertEqual(fleet._renew_strikes(), 0)
        self.assertEqual(fleet._refresh_cooldown_left("claude_refresh"), 0)

    def test_a_backoff_holds_every_caller_but_refresh_now(self):
        cred = _claude_home(self.root)
        fleet._set_cooldown("claude_refresh", 300)
        with self._claude_code(renews_to="new") as popen:
            self.assertEqual(fleet._renew_claude_via_cli(cred, "old")[0], "busy")
            self.assertEqual(popen.call_count, 0)
            self.assertEqual(fleet._renew_claude_via_cli(cred, "old", force=True)[0], "renewed")

    def test_a_stuck_claude_code_is_never_stopped_while_it_holds_its_refresh_lock(self):
        cred = _claude_home(self.root)
        lock = cred.parent / ".oauth_refresh.lock"
        lock.mkdir()
        kills = []

        def start(cmd, **kwargs):
            proc = mock.Mock(returncode=None)
            calls = []

            def communicate(timeout=None):
                calls.append(timeout)
                if len(calls) == 1:
                    raise fleet.subprocess.TimeoutExpired(cmd, timeout)
                return (b"", b"")

            proc.communicate.side_effect = communicate
            proc.kill.side_effect = lambda: kills.append(lock.exists())
            return proc

        naps = []

        def nap(seconds):
            naps.append(seconds)
            if len(naps) == 3:
                lock.rmdir()  # Claude Code finished saving

        with mock.patch.object(fleet.subprocess, "Popen", side_effect=start), mock.patch.object(
            fleet.time, "sleep", side_effect=nap
        ):
            fleet._run_claude_usage(cred)

        self.assertEqual(kills, [False])

    def test_without_claude_code_installed_the_flyout_asks_for_a_sign_in(self):
        _claude_home(self.root)
        with mock.patch.object(fleet, "_claude_cli", return_value=None):
            result = fleet.api_claude()

        self.assertIn(fleet.CLAUDE_EXPIRED, result["err"])
        self.assertIn("not installed", result["err"])

    def test_fleet_names_itself_to_anthropic(self):
        _claude_home(self.root, expires=(time.time() + 3600) * 1000)
        with mock.patch.object(fleet, "_jget", return_value=(200, {}, {"five_hour": {"utilization": 5}})) as usage:
            fleet.api_claude()

        self.assertNotIn("claude-code", usage.call_args.args[2]["User-Agent"])
        self.assertIn("Fleet", usage.call_args.args[2]["User-Agent"])

    def test_a_rejected_access_token_is_not_sent_again(self):
        _claude_home(self.root, token="revoked", expires=(time.time() + 3600) * 1000)
        with mock.patch.object(fleet, "_jget", return_value=(401, {}, {})) as usage:
            fleet.api_claude()
            fleet.api_claude()
            self.assertEqual(usage.call_count, 1)
            _claude_home(self.root, token="fresh", expires=(time.time() + 3600) * 1000)
            fleet.api_claude()
        self.assertEqual(usage.call_count, 2)
        self.assertNotIn("revoked", (self.cache / "claude_dead_token").read_text())

    def test_usage_text_resets_land_in_the_local_zone(self):
        tokyo = timezone(timedelta(hours=9))
        metrics = fleet._parse_claude_usage(CLI_USAGE, now=datetime(2026, 10, 14, 21, 10, tzinfo=tokyo))
        self.assertEqual([m["resets"] for m in metrics],
                         ["2026-10-15T01:20:00+09:00", "2026-10-16T08:00:00+09:00", "2026-10-16T08:00:00+09:00"])
        wrap = fleet._parse_claude_usage(
            "Current week (all models): 10% used \u00b7 resets Jan 2, 9am (Asia/Tokyo)",
            now=datetime(2026, 12, 30, 10, 0, tzinfo=tokyo))
        self.assertEqual(wrap[0]["resets"], "2027-01-02T09:00:00+09:00")
        self.assertEqual(fleet._parse_claude_usage("Invalid API key \u00b7 Please run /login"), [])


class PacingTests(unittest.TestCase):
    def setUp(self):
        fleet._LIVE.clear()
        self.addCleanup(fleet._LIVE.clear)

    def test_a_provider_is_called_at_most_once_per_window(self):
        calls = []

        def fetch():
            calls.append(1)
            return {"agent": "Codex", "metrics": [fleet.M("Weekly", 10)], "err": ""}

        first = fleet._paced("api_codex", fetch)
        second = fleet._paced("api_codex", fetch)

        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)

    def test_refresh_now_bypasses_the_window(self):
        calls = []

        def fetch():
            calls.append(1)
            return {"agent": "Codex", "metrics": [fleet.M("Weekly", 10)], "err": ""}

        fleet._paced("api_codex", fetch)
        fleet._paced("api_codex", fetch, force=True)

        self.assertEqual(len(calls), 2)

    def test_failures_and_cached_readings_are_not_held_back(self):
        calls = []

        def failing():
            calls.append(1)
            return {"agent": "Codex", "metrics": [fleet.M("Weekly", 10)], "err": "offline", "cached_at": 1.0}

        fleet._paced("api_codex", failing)
        fleet._paced("api_codex", failing)

        self.assertEqual(len(calls), 2)

    def test_the_window_expires(self):
        calls = []

        def fetch():
            calls.append(1)
            return {"agent": "Codex", "metrics": [fleet.M("Weekly", 10)], "err": ""}

        with mock.patch.object(fleet.time, "monotonic", side_effect=[100.0, 100.0 + fleet.PACE["api_codex"] + 1]):
            fleet._paced("api_codex", fetch)
            fleet._paced("api_codex", fetch)

        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
