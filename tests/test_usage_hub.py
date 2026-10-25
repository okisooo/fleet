import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import usage_hub


class FleetUsageHubTests(unittest.TestCase):
    def test_codex_go_identity_comes_from_its_configured_home(self):
        claims = base64.urlsafe_b64encode(json.dumps({"email": "sam@example.com"}).encode()).decode().rstrip("=")
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".codex").mkdir()
            (home / ".codex" / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": "native", "account_id": "native-acct"}}),
                encoding="utf-8",
            )
            go_home = home / "codex-go"
            go_home.mkdir()
            (go_home / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": "go", "account_id": "go-acct", "id_token": f"x.{claims}.x"}}),
                encoding="utf-8",
            )
            settings = home / ".t3" / "userdata"
            settings.mkdir(parents=True)
            (settings / "settings.json").write_text(
                json.dumps({"providerInstances": {"codex-go": {"enabled": True, "driver": "codex", "config": {"homePath": str(go_home)}}}}),
                encoding="utf-8",
            )
            with mock.patch.object(usage_hub.fleet, "HOME", home):
                self.assertEqual(usage_hub._codex_credentials(), ("go", "go-acct", "sam@example.com"))
                (go_home / "auth.json").write_text(
                    json.dumps({"tokens": {"access_token": "go", "account_id": "native-acct"}}),
                    encoding="utf-8",
                )
                self.assertIsNone(usage_hub._codex_credentials())

    def test_authorization_uses_the_persisted_management_key(self):
        hub = usage_hub.FleetUsageHub("secret-key-that-is-long-enough-for-testing")
        self.assertTrue(hub.authorized("Bearer secret-key-that-is-long-enough-for-testing"))
        self.assertFalse(hub.authorized("Bearer wrong"))
        self.assertFalse(hub.authorized(None))

    def test_lists_only_authenticated_codex_and_claude_accounts(self):
        hub = usage_hub.FleetUsageHub("key")
        with mock.patch.object(usage_hub, "_codex_credentials", return_value=("token", "acct", "sam@example.com")), mock.patch.object(
            usage_hub, "_claude_credentials", return_value="token"
        ):
            status, payload = hub.route("GET", "/v0/management/auth-files")
        self.assertEqual(status, 200)
        self.assertEqual(
            [(item["auth_index"], item["provider"]) for item in payload["files"]],
            [("fleet-codex-go", "codex"), ("fleet-claude", "claude")],
        )
        self.assertEqual(payload["files"][0]["email"], "sam@example.com")
        self.assertEqual(payload["files"][0]["id_token"]["chatgpt_account_id"], "acct")

    def test_codex_proxy_is_allowlisted_and_never_returns_the_token(self):
        hub = usage_hub.FleetUsageHub("key")
        response = json.dumps({"plan_type": "pro", "rate_limit": None}).encode()
        with mock.patch.object(
            usage_hub, "_codex_credentials", return_value=("private-token", "account", "sam@example.com")
        ), mock.patch.object(usage_hub.fleet, "_http", return_value=(200, {}, response)) as call:
            status, payload = hub.api_call(
                {
                    "auth_index": "fleet-codex-go",
                    "method": "GET",
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                }
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status_code"], 200)
        self.assertNotIn("private-token", json.dumps(payload))
        self.assertEqual(call.call_args.args[2]["Authorization"], "Bearer private-token")

        denied, _payload = hub.api_call(
            {
                "auth_index": "fleet-codex-go",
                "method": "GET",
                "url": "https://example.com/steal",
            }
        )
        self.assertEqual(denied, 403)

    def test_connection_file_keeps_a_stable_generated_key(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hub.json"
            first = usage_hub._load_or_create_key(path)
            usage_hub._atomic_json(path, {"management_key": first})
            second = usage_hub._load_or_create_key(path)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 32)

    def test_fleet_snapshot_exposes_real_metrics_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            (cache / "state.json").write_text(
                json.dumps({"ts": 1000, "data": [
                    {"agent": "Antigrav", "plan": "Google AI Pro", "err": "", "metrics": [
                        {"label": "Weekly", "pct": 14.5, "resets": "2026-09-25T04:39:37Z", "extra": ""}
                    ]},
                    {"agent": "OpenRouter", "plan": "", "err": "", "metrics": [
                        {"label": "Balance", "pct": None, "resets": None, "extra": "$5.06"}
                    ]},
                ]}), encoding="utf-8",
            )
            (cache / "antigravity_last.json").write_text(json.dumps({"ts": 900}), encoding="utf-8")
            hub = usage_hub.FleetUsageHub("key", cache)
            status, payload = hub.route("GET", "/v0/management/fleet-snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(payload["providers"][0]["metrics"][0]["usedPercent"], 14.5)
        self.assertEqual(payload["providers"][0]["observedAt"], 900)
        self.assertEqual(payload["providers"][1]["metrics"][0]["detail"], "$5.06")
        self.assertNotIn("access_token", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
