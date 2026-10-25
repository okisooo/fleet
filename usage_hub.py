"""Local CLIProxyAPI-compatible usage bridge for T3 Code.

T3 currently understands Codex and Claude accounts through its usage-hub
interface. Fleet exposes only those two providers here and keeps the existing
collectors responsible for every provider shown in the tray.
"""
from __future__ import annotations

import base64
import hmac
import json
import math
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import fleet


DEFAULT_PORT = 8318
MAX_BODY_BYTES = 1024 * 1024
CODEX_ORIGIN = "https://chatgpt.com"
CODEX_PREFIX = "/backend-api/wham/"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
FLEET_LAST_FILES = {
    "Claude": "claude_last.json",
    "Codex": "codex_last.json",
    "Grok": "grok_last.json",
    "Antigrav": "antigravity_last.json",
    "OpenRouter": "api_openrouter_last.json",
}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".tmp")
    staging.write_text(json.dumps(value, indent=2), encoding="utf-8")
    try:
        os.chmod(staging, 0o600)
    except OSError:
        pass
    os.replace(staging, path)


def _load_or_create_key(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        key = value.get("management_key")
        if isinstance(key, str) and len(key) >= 32:
            return key
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass
    return secrets.token_urlsafe(32)


def _codex_auth(root: Path, refresh_token: bool = False):
    # Read-only by default: OpenAI rotates refresh tokens, so refreshing here
    # without saving would retire the token that Codex home's CLI holds.
    try:
        auth = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    tokens = auth.get("tokens") or auth
    token = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if token and refresh and refresh_token and fleet._jwt_exp(token) < time.time() + 60:
        token = fleet._refresh_codex(refresh) or token
    if not token:
        return None
    email = None
    try:
        encoded = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        email = claims.get("email")
    except (KeyError, IndexError, ValueError, TypeError):
        pass
    return token, tokens.get("account_id") or auth.get("account_id"), email


def _codex_credentials():
    """Bridge T3's separate Codex Go account, never its native Codex account twice."""
    try:
        settings = json.loads(
            (fleet.HOME / ".t3" / "userdata" / "settings.json").read_text(encoding="utf-8")
        )
        instance = settings["providerInstances"]["codex-go"]
        if not instance.get("enabled") or instance.get("driver") != "codex":
            return None
        home = Path(instance["config"]["homePath"])
        if not home.is_absolute():
            return None
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError):
        return None
    go = _codex_auth(home)
    native = _codex_auth(fleet.HOME / ".codex")
    if go and native and go[1] and go[1] == native[1]:
        return None
    return go


def _claude_credentials():
    path = fleet.HOME / ".claude" / ".credentials.json"
    try:
        full = json.loads(path.read_text(encoding="utf-8"))
        oauth = full.get("claudeAiOauth") or {}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    token = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    expires = fleet._claude_expiry(oauth)
    if token and refresh and expires and expires < time.time() + fleet.CLAUDE_REFRESH_MARGIN:
        # Claude Code renews its own login; Fleet never calls the token endpoint.
        fleet._renew_claude_via_cli(path, token)
        try:
            token = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] or token
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return token if token else None


class FleetUsageHub:
    def __init__(self, management_key: str, cache_dir: Path | None = None):
        self.management_key = management_key
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None

    def authorized(self, header: str | None) -> bool:
        expected = f"Bearer {self.management_key}"
        return isinstance(header, str) and hmac.compare_digest(header, expected)

    def auth_files(self):
        files = []
        codex = _codex_credentials()
        if codex:
            _token, account_id, email = codex
            files.append(
                {
                    "id": "fleet-codex-go",
                    "auth_index": "fleet-codex-go",
                    "provider": "codex",
                    "disabled": False,
                    **({"email": email} if email else {}),
                    **({"id_token": {"chatgpt_account_id": account_id}} if account_id else {}),
                }
            )
        if _claude_credentials():
            files.append(
                {
                    "id": "fleet-claude",
                    "auth_index": "fleet-claude",
                    "provider": "claude",
                    "disabled": False,
                }
            )
        return {"files": files}

    def fleet_snapshot(self):
        if self.cache_dir is None:
            return 503, {"error": "Fleet state is unavailable"}
        try:
            state = json.loads((self.cache_dir / "state.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return 503, {"error": "Fleet state is unavailable"}
        providers = []
        for entry in state.get("data", []):
            name = entry.get("agent")
            last_file = FLEET_LAST_FILES.get(name)
            if not last_file:
                continue
            try:
                last = json.loads((self.cache_dir / last_file).read_text(encoding="utf-8"))
                observed_at = last.get("ts")
            except (FileNotFoundError, OSError, ValueError, TypeError):
                observed_at = None
            metrics = []
            for metric in entry.get("metrics", []):
                used = metric.get("pct")
                if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used):
                    used = None
                else:
                    used = max(0, min(100, used))
                metrics.append({
                    "label": str(metric.get("label") or "")[:80],
                    "usedPercent": used,
                    "resetsAt": metric.get("resets"),
                    "detail": str(metric.get("extra") or "")[:100],
                })
            providers.append({
                "name": name,
                "plan": str(entry.get("plan") or "")[:80],
                "observedAt": observed_at if isinstance(observed_at, (int, float)) else None,
                "available": not bool(entry.get("err")),
                "metrics": metrics,
            })
        return 200, {"version": 1, "updatedAt": state.get("ts"), "providers": providers}

    def api_call(self, request: dict):
        account = request.get("auth_index")
        method = str(request.get("method") or "GET").upper()
        url = str(request.get("url") or "")
        raw_data = request.get("data")
        data = None
        if raw_data is not None:
            try:
                data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            except ValueError:
                return 400, {"error": "invalid provider request data"}

        if account == "fleet-codex-go":
            parsed = urlparse(url)
            if f"{parsed.scheme}://{parsed.netloc}" != CODEX_ORIGIN or not parsed.path.startswith(
                CODEX_PREFIX
            ):
                return 403, {"error": "provider URL is not allowed"}
            credentials = _codex_credentials()
            if not credentials:
                return 409, {"error": "Codex is not authenticated"}
            token, account_id, _email = credentials
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "codex-1",
                "Originator": "Codex Desktop",
            }
            if account_id:
                headers["Chatgpt-Account-Id"] = account_id
        elif account == "fleet-claude":
            if url != CLAUDE_USAGE_URL or method != "GET":
                return 403, {"error": "provider URL is not allowed"}
            token = _claude_credentials()
            if not token:
                return 409, {"error": "Claude is not authenticated"}
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": fleet.FLEET_UA,
            }
        else:
            return 404, {"error": "account not found"}

        if method not in {"GET", "POST"}:
            return 405, {"error": "method not allowed"}
        status, _headers, body = fleet._http(method, url, headers, data)
        return 200, {
            "status_code": status,
            "body": body.decode("utf-8", errors="replace"),
        }

    def route(self, method: str, path: str, body: object | None = None):
        if method == "GET" and path == "/v0/management/auth-files":
            return 200, self.auth_files()
        if method == "GET" and path == "/v0/management/fleet-snapshot":
            return self.fleet_snapshot()
        if method == "POST" and path == "/v0/management/api-call" and isinstance(body, dict):
            return self.api_call(body)
        if method == "POST" and path == "/v0/management/reset-quota":
            return 200, {"ok": True}
        return 404, {"error": "not found"}


def _handler(hub: FleetUsageHub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FleetUsageHub/1"

        def log_message(self, _format, *_args):
            return

        def _reply(self, status: int, value: object):
            payload = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _serve(self):
            if not hub.authorized(self.headers.get("Authorization")):
                self._reply(401, {"error": "unauthorized"})
                return
            body = None
            if self.command == "POST":
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                    if size < 0 or size > MAX_BODY_BYTES:
                        self._reply(413, {"error": "request too large"})
                        return
                    body = json.loads(self.rfile.read(size) or b"{}")
                except (ValueError, TypeError):
                    self._reply(400, {"error": "invalid json"})
                    return
            status, value = hub.route(self.command, urlparse(self.path).path, body)
            self._reply(status, value)

        do_GET = _serve
        do_POST = _serve

    return Handler


def start_usage_hub(cache_dir: Path, stop_event: threading.Event, logger=None):
    """Start Fleet's local T3 bridge and return its connection settings."""
    config_path = Path(cache_dir) / "t3-usage-hub.json"
    management_key = _load_or_create_key(config_path)
    port = int(os.environ.get("FLEET_T3_HUB_PORT", DEFAULT_PORT))
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(FleetUsageHub(management_key, cache_dir)))
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_port}"
    config = {"url": url, "management_key": management_key}
    _atomic_json(config_path, config)

    threading.Thread(target=server.serve_forever, daemon=True, name="fleet-t3-hub").start()

    def stop_when_requested():
        stop_event.wait()
        server.shutdown()
        server.server_close()

    threading.Thread(target=stop_when_requested, daemon=True, name="fleet-t3-hub-stop").start()
    if logger is not None:
        logger.info("T3 usage hub listening at %s", url)
    return config
