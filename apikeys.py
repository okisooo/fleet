#!/usr/bin/env python
"""apikeys.py - public API-key providers and user-defined custom entries.

Every provider here is one declarative spec: where the key comes from, one HTTPS
GET, and a few dotted paths that turn the JSON into Fleet metrics. Custom entries
from the user's config go through the same engine, so anything Fleet does not
ship can still be added without code.

Two kinds of reading:
  * balance/quota endpoints - free to call, enabled as soon as the key's
    environment variable exists.
  * rate-limit header probes - only available by sending a real 1-token request,
    so they cost a fraction of a cent and stay opt-in per provider.

Keys come from environment variables or Windows Credential Manager. Fleet never
puts keys in JSON or logs; config holds variable *names*, not secrets.
"""
from __future__ import annotations

import hashlib
import email.utils
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import api_credentials

# ---------- config ----------
CONFIG_NAME = "providers.json"


def config_path(root: Path) -> Path:
    return root / CONFIG_NAME


def load_config(root: Path):
    """Read providers.json. A missing or broken file must never break polling."""
    try:
        raw = json.loads(config_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as e:
        return {}, f"{CONFIG_NAME}: {e.__class__.__name__}"
    if not isinstance(raw, dict):
        return {}, f"{CONFIG_NAME}: expected an object"
    return raw, ""


# ---------- dotted path language ----------
_INDEX = re.compile(r"^([^\[\]]*)\[([^\[\]]+)\]$")


def resolve(data, path):
    """Walk 'a.b[0].c' or 'a.b[type=TOKENS].c' over parsed JSON.

    The filter form picks the first list item whose key equals the value, which
    is what quota endpoints that return one row per limit type need.
    """
    if not path:
        return None
    node = data
    for part in str(path).split("."):
        if node is None:
            return None
        match = _INDEX.match(part)
        selector = None
        if match:
            part, selector = match.group(1), match.group(2)
        if part:
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        if selector is None:
            continue
        if not isinstance(node, list):
            return None
        if "=" in selector:
            key, _, want = selector.partition("=")
            node = next(
                (
                    item
                    for item in node
                    if isinstance(item, dict) and str(item.get(key)) == want
                ),
                None,
            )
        else:
            try:
                node = node[int(selector)]
            except (ValueError, IndexError):
                return None
    return node


def _number(value):
    """Accept numbers and numeric strings; balance endpoints return both."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _money(value, currency):
    amount = _number(value)
    if amount is None:
        return ""
    symbol = {"USD": "$", "CNY": "¥", "EUR": "€"}.get(str(currency or "USD").upper(), "")
    unit = "" if symbol else f" {currency}" if currency else ""
    return f"{symbol}{amount:,.2f}{unit}"


def _pct(used, limit):
    used, limit = _number(used), _number(limit)
    if used is None or not limit:
        return None
    return max(0.0, min(100.0, used / limit * 100.0))


def _count(used, limit):
    used, limit = _number(used), _number(limit)
    if used is None:
        return ""
    return f"{used:,.0f}/{limit:,.0f}" if limit else f"{used:,.0f}"


def build_metrics(payload, specs, M):
    """Turn a JSON payload into Fleet metric dicts using the spec's paths."""
    metrics = []
    for spec in specs:
        label = spec.get("label") or ""
        kind = spec.get("kind") or "pct"
        resets = resolve(payload, spec.get("resets")) if spec.get("resets") else None
        pct = extra = None
        if kind == "money":
            currency = resolve(payload, spec.get("currency")) or spec.get("currency_literal")
            if spec.get("show_remaining_money") and (spec.get("used") is not None or spec.get("limit") is not None or spec.get("remaining") is not None):
                # Remaining budget = limit - used, or a direct remaining path.
                if spec.get("remaining"):
                    rem = _number(resolve(payload, spec.get("remaining")))
                else:
                    used = _number(resolve(payload, spec.get("used")))
                    limit = _number(resolve(payload, spec.get("limit")))
                    rem = (limit - used) if limit is not None and used is not None else None
                extra = _money(rem, currency)
            else:
                extra = _money(resolve(payload, spec.get("value")), currency)
            if not extra:
                continue
        elif kind == "count":
            used = resolve(payload, spec.get("used"))
            limit = resolve(payload, spec.get("limit"))
            pct = _pct(used, limit)
            extra = _count(used, limit)
            if pct is None and not extra:
                continue
        else:
            if spec.get("pct"):
                pct = _number(resolve(payload, spec["pct"]))
                if pct is not None and spec.get("pct_scale"):
                    pct *= float(spec["pct_scale"])
                if pct is not None and spec.get("pct_is_remaining"):
                    pct = 100.0 - pct
            else:
                used = resolve(payload, spec.get("used"))
                limit = resolve(payload, spec.get("limit"))
                if used is None and spec.get("remaining"):
                    remaining = _number(resolve(payload, spec["remaining"]))
                    total = _number(limit)
                    used = None if remaining is None or total is None else total - remaining
                pct = _pct(used, limit)
                if pct is not None and spec.get("show_amounts"):
                    if spec.get("show_remaining_money"):
                        u, l = _number(used), _number(limit)
                        rem = (l - u) if l is not None and u is not None else None
                        extra = _money(rem, spec.get("currency_literal"))
                    elif spec.get("money_amounts"):
                        extra = _money(used, spec.get("currency_literal"))
                    else:
                        extra = _count(used, limit)
            if pct is None:
                continue
            pct = max(0.0, min(100.0, pct))
        metrics.append(M(label, pct, resets, extra or ""))
    return metrics


# ---------- built-in specs ----------
# Each entry: how to authenticate, one GET, and the metric mapping. `probe`
# providers have no free usage endpoint; they read rate-limit headers off a
# 1-token request and stay off until the user opts in.
BUILTIN = [
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "env": ["OPENROUTER_API_KEY"],
        "url": "https://openrouter.ai/api/v1/credits",
        "metrics": [
            # Just the remaining credit balance — no usage bar.
            {"label": "Balance", "kind": "money", "used": "data.total_usage",
             "limit": "data.total_credits", "show_remaining_money": True,
             "currency_literal": "USD"},
        ],
        # The account-wide credits endpoint accepts management keys only. A
        # regular API key can still report its own remaining budget and spend.
        "fallback": {
            "statuses": [403],
            "url": "https://openrouter.ai/api/v1/key",
            "plan": "data.is_free_tier",
            "plan_map": {"True": "free", "False": "paid"},
            "metrics": [
                {"label": "Balance", "kind": "money", "value": "data.limit_remaining",
                 "currency_literal": "USD"},
                {"label": "Spend mo", "kind": "money", "value": "data.usage_monthly",
                 "currency_literal": "USD"},
            ],
        },
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "env": ["DEEPSEEK_API_KEY"],
        "url": "https://api.deepseek.com/user/balance",
        "metrics": [
            {"label": "Balance", "kind": "money", "value": "balance_infos[0].total_balance",
             "currency": "balance_infos[0].currency"},
            {"label": "Topped up", "kind": "money", "value": "balance_infos[0].topped_up_balance",
             "currency": "balance_infos[0].currency"},
        ],
    },
    {
        "id": "moonshot",
        "label": "Kimi",
        "env": ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
        "url": "https://api.moonshot.ai/v1/users/me/balance",
        "metrics": [
            {"label": "Balance", "kind": "money", "value": "data.available_balance",
             "currency_literal": "USD"},
            {"label": "Cash", "kind": "money", "value": "data.cash_balance",
             "currency_literal": "USD"},
        ],
    },
    {
        "id": "minimax",
        "label": "MiniMax",
        "env": ["MINIMAX_API_KEY"],
        "url": "https://api.minimax.io/v1/api/openplatform/coding_plan/remains",
        "plan": "plan_name",
        "metrics": [
            {"label": "Prompts", "kind": "count",
             "used": "model_remains[0].current_interval_usage_count",
             "limit": "model_remains[0].current_interval_total_count",
             "resets": "model_remains[0].end_time"},
        ],
    },
    {
        # Undocumented console endpoint: treated as best effort, and it is the
        # only way to see a GLM coding-plan window without scraping the site.
        "id": "zai",
        "label": "Z.AI",
        "env": ["ZAI_API_KEY", "ZHIPUAI_API_KEY", "GLM_API_KEY"],
        "url": "https://api.z.ai/api/monitor/usage/quota/limit",
        "metrics": [
            {"label": "Tokens", "kind": "pct", "pct": "data[type=TOKENS_LIMIT].percentage",
             "resets": "data[type=TOKENS_LIMIT].nextResetTime"},
            {"label": "Calls", "kind": "count", "used": "data[type=TIME_LIMIT].currentValue",
             "limit": "data[type=TIME_LIMIT].usage"},
        ],
    },
    {
        "id": "openai-admin",
        "label": "OpenAI org",
        "env": ["OPENAI_ADMIN_KEY"],
        "url": "https://api.openai.com/v1/organization/costs",
        "query": {"bucket_width": "1d", "limit": "1"},
        "since": "start_time",
        "metrics": [
            {"label": "Cost 24h", "kind": "money",
             "value": "data[0].results[0].amount.value",
             "currency": "data[0].results[0].amount.currency"},
        ],
    },
    {
        "id": "anthropic-admin",
        "label": "Claude API",
        "env": ["ANTHROPIC_ADMIN_KEY"],
        "auth": "x-api-key",
        "url": "https://api.anthropic.com/v1/organizations/cost_report",
        "headers": {"anthropic-version": "2023-06-01"},
        "collector": "anthropic_cost",
        "metrics": [],
    },
    {
        # Self-hosted proxy: the base URL is the user's own deployment.
        "id": "litellm",
        "label": "LiteLLM",
        "env": ["LITELLM_API_KEY"],
        "base_env": ["LITELLM_BASE_URL", "LITELLM_PROXY_URL"],
        "path": "/key/info",
        "allow_http_loopback": True,
        "metrics": [
            {"label": "Spend", "kind": "pct", "used": "info.spend",
             "limit": "info.max_budget", "resets": "info.budget_reset_at",
             "show_amounts": True, "money_amounts": True},
            {"label": "Spent", "kind": "money", "value": "info.spend"},
        ],
    },
]

# OpenAI-compatible endpoints that only expose quota through rate-limit headers.
PROBES = [
    ("openai", "OpenAI", ["OPENAI_API_KEY"], "https://api.openai.com/v1", "gpt-4o-mini"),
    ("groq", "Groq", ["GROQ_API_KEY"], "https://api.groq.com/openai/v1", "llama-3.1-8b-instant"),
    ("mistral", "Mistral", ["MISTRAL_API_KEY"], "https://api.mistral.ai/v1", "mistral-small-latest"),
    ("together", "Together", ["TOGETHER_API_KEY"], "https://api.together.xyz/v1",
     "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    ("fireworks", "Fireworks", ["FIREWORKS_API_KEY"], "https://api.fireworks.ai/inference/v1",
     "accounts/fireworks/models/llama-v3p1-8b-instruct"),
    ("cerebras", "Cerebras", ["CEREBRAS_API_KEY"], "https://api.cerebras.ai/v1", "llama3.1-8b"),
    ("deepinfra", "DeepInfra", ["DEEPINFRA_API_KEY"], "https://api.deepinfra.com/v1/openai",
     "meta-llama/Meta-Llama-3.1-8B-Instruct"),
    ("nebius", "Nebius", ["NEBIUS_API_KEY"], "https://api.studio.nebius.ai/v1",
     "meta-llama/Meta-Llama-3.1-8B-Instruct"),
    ("xai", "xAI API", ["XAI_API_KEY"], "https://api.x.ai/v1", "grok-3-mini"),
    ("perplexity", "Perplexity", ["PERPLEXITY_API_KEY"], "https://api.perplexity.ai", "sonar"),
]

for _id, _label, _env, _base, _model in PROBES:
    BUILTIN.append({
        "id": _id,
        "label": _label,
        "env": list(_env),
        "probe": {"base": _base, "model": _model, "style": "openai"},
        "metrics": [],
    })

BUILTIN.append({
    "id": "anthropic",
    "label": "Claude API",
    "env": ["ANTHROPIC_API_KEY"],
    "auth": "x-api-key",
    "probe": {"base": "https://api.anthropic.com/v1", "style": "anthropic",
              "model": "claude-haiku-4-5-20251001"},
    "metrics": [],
})

BUILTIN_BY_ID = {spec["id"]: spec for spec in BUILTIN}


# ---------- rate-limit headers ----------
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")


def parse_reset(value):
    """OpenAI-style resets are durations like '6m0s'; others are RFC 3339."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    units = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
    matches = _DURATION.findall(text)
    if matches and not text[0].isalpha():
        seconds = sum(float(amount) * units[unit] for amount, unit in matches)
        return time.time() + seconds
    return value


def metrics_from_headers(headers, header_get, M):
    """Build metrics from x-ratelimit-* (OpenAI) or anthropic-ratelimit-* headers."""
    def number(name):
        return _number(header_get(headers, name))

    metrics = []
    for label, prefix in (("Requests", "requests"), ("Tokens", "tokens")):
        limit = number(f"x-ratelimit-limit-{prefix}")
        remaining = number(f"x-ratelimit-remaining-{prefix}")
        if limit is None or remaining is None:
            limit = number(f"anthropic-ratelimit-{prefix}-limit")
            remaining = number(f"anthropic-ratelimit-{prefix}-remaining")
            reset = header_get(headers, f"anthropic-ratelimit-{prefix}-reset")
        else:
            reset = header_get(headers, f"x-ratelimit-reset-{prefix}")
        if limit is None or remaining is None or not limit:
            continue
        metrics.append(
            M(label, max(0.0, min(100.0, (limit - remaining) / limit * 100.0)),
              parse_reset(reset), f"{remaining:,.0f} left")
        )
    return metrics


# ---------- collection ----------
def _get_registry_env(name: str) -> str | None:
    import sys
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value) if value else None
    except Exception:
        return None


def _first_env(names, environ=None):
    use_registry = environ is None
    env_map = os.environ if environ is None else environ
    for name in names or []:
        value = env_map.get(name) or (_get_registry_env(name) if use_registry else None)
        if value and str(value).strip():
            return name, str(value).strip()
    return None, None


def _key_for(spec, environ=None):
    name, key = _first_env(spec.get("env"), environ)
    if key:
        return "environment", key
    # Explicit test environments never consult the user's credential store.
    if environ is None and spec.get("id") in api_credentials.TARGETS and not spec.get("custom"):
        key = api_credentials.read(spec["id"])
        if key:
            return "windows", key
    return None, None


def account_status(config=None):
    """Non-secret setup metadata for the native panel, including disabled keys."""
    settings = (config or {}).get("providers") or {}
    if not isinstance(settings, dict):
        settings = {}
    out = []
    for pid in ("anthropic", "anthropic-admin"):
        user = settings.get(pid) or {}
        if not isinstance(user, dict):
            user = {}
        spec = BUILTIN_BY_ID[pid]
        names = [user["env"]] if user.get("env") else spec["env"]
        out.append({"id": pid, "source": _key_for(dict(spec, env=names))[0] or "",
                    "enabled": user.get("enabled") is not False})
    return out


def _auth_headers(spec, key):
    style = spec.get("auth") or "bearer"
    if style == "x-api-key":
        return {"x-api-key": key}
    if style == "none":
        return {}
    return {"Authorization": f"Bearer {key}"}


def _url_for(spec, settings):
    if spec.get("url"):
        return spec["url"]
    base = settings.get("base_url") or _first_env(spec.get("base_env"))[1]
    if not base:
        return None
    return base.rstrip("/") + (spec.get("path") or "")


def _check_url(url, spec):
    """HTTPS only, except a loopback proxy the user is running themselves."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme == "https":
        return ""
    loopback = parts.hostname in ("localhost", "127.0.0.1", "::1", "[::1]")
    if parts.scheme == "http" and loopback and spec.get("allow_http_loopback"):
        return ""
    return "refused: not https"


def _with_since(url, spec, query):
    query = dict(query or {})
    field = spec.get("since")
    if field:
        start = time.time() - 86400
        query[field] = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))
            if spec.get("since_format") == "iso"
            else str(int(start))
        )
    if not query:
        return url
    joiner = "&" if "?" in url else "?"
    return url + joiner + urllib.parse.urlencode(query)


def enabled_specs(config, environ=None):
    """Specs the user actually has keys for, plus their custom entries.

    A built-in turns itself on as soon as its environment variable exists. On
    Windows, values written by `setx` are also read from the user registry,
    so the next poll can discover them without restarting Fleet. Config can force
    a spec off, point it at a different variable, or opt into a paid probe.
    """
    settings = config.get("providers") or {}
    out = []
    for spec in BUILTIN:
        user = settings.get(spec["id"]) or {}
        if not isinstance(user, dict):
            user = {}
        names = [user["env"]] if user.get("env") else spec.get("env") or []
        # A real process lookup may also see values written by `setx` after
        # Fleet started. Explicit mappings in tests/config checks stay isolated.
        has_key = bool(_key_for(dict(spec, env=names), environ)[1])
        wants_probe = bool(user.get("probe"))
        if user.get("enabled") is False or not has_key:
            continue
        if spec.get("probe") and not wants_probe and spec["id"] != "anthropic":
            continue
        merged = dict(spec)
        merged["env"] = names
        merged["_settings"] = user
        if spec["id"] == "anthropic" and not wants_probe:
            merged["connection_only"] = True
        out.append(merged)
    # Both credentials describe the same account. Prefer its billing report;
    # keep an explicitly enabled rate-limit probe as a separate opt-in signal.
    if any(spec["id"] == "anthropic-admin" for spec in out):
        out = [spec for spec in out if not spec.get("connection_only")]
    for index, entry in enumerate(config.get("custom") or []):
        spec = normalize_custom(entry, index)
        if spec:
            out.append(spec)
    return out


def normalize_custom(entry, index=0):
    """Validate one user-defined entry. Bad entries are skipped, not fatal."""
    if not isinstance(entry, dict) or not entry.get("url"):
        return None
    spec = {
        "id": str(entry.get("id") or f"custom{index}")[:64],
        "label": str(entry.get("label") or entry.get("id") or "Custom")[:24],
        "url": str(entry["url"]),
        "auth": entry.get("auth") or ("bearer" if entry.get("env") else "none"),
        "env": [entry["env"]] if entry.get("env") else [],
        "headers": entry.get("headers") if isinstance(entry.get("headers"), dict) else {},
        "metrics": [m for m in (entry.get("metrics") or []) if isinstance(m, dict)],
        "plan": entry.get("plan"),
        "allow_http_loopback": bool(entry.get("allow_http_loopback")),
        "custom": True,
        "_settings": {},
    }
    return spec if spec["metrics"] else None


def _plan_text(spec, payload):
    if not spec.get("plan"):
        return ""
    value = resolve(payload, spec["plan"])
    if value is None:
        return ""
    return str(spec.get("plan_map", {}).get(str(value), value))[:16]


def _anthropic_error(status):
    if status in (401, 403):
        return "key rejected"
    if status == 404:
        return "billing reports unavailable for this account"
    if status == 429:
        return "rate limited (http 429)"
    return f"http {status}" if status else "offline"


def _retry_at(headers):
    value = next((v for k, v in (headers or {}).items() if k.casefold() == "retry-after"), None)
    if value is None:
        return None
    try:
        seconds = float(value)
        if 0 <= seconds <= 86400:
            return time.time() + seconds
    except (TypeError, ValueError):
        try:
            return min(time.time() + 86400, email.utils.parsedate_to_datetime(str(value)).timestamp())
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def _anthropic_cost(spec, key, http, M, timeout):
    """Organization spend in UTC days; amounts are decimal cents, not credits.

    Publish only complete reports so pagination failures retain the last reading.
    """
    out = dict(agent=spec["label"], api_id=spec["id"], plan="Organization",
               credential_id=hashlib.sha256(key.encode()).hexdigest(),
               metrics=[], err="", compact=False)
    now = datetime.fromtimestamp(time.time(), timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    query = {"starting_at": today.replace(day=1).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "bucket_width": "1d", "limit": "31"}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Accept": "application/json"}
    month_cents = today_cents = Decimal(0)
    seen = set()
    for _ in range(16):
        url = spec["url"] + "?" + urllib.parse.urlencode(query)
        status, response_headers, body = http("GET", url, headers, timeout=timeout)
        if status != 200:
            out["err"] = _anthropic_error(status)
            if status == 429:
                out["retry_at"] = _retry_at(response_headers)
            return out
        if len(body or b"") > 1_000_000:
            out["err"] = "payload too large"
            return out
        try:
            payload = json.loads(body)
            buckets = payload["data"]
            if not isinstance(buckets, list):
                raise ValueError()
            for bucket in buckets:
                day = datetime.fromisoformat(bucket["starting_at"].replace("Z", "+00:00"))
                results = bucket["results"]
                if day.tzinfo is None or not isinstance(results, list):
                    raise ValueError()
                if not today.replace(day=1) <= day < today + timedelta(days=1):
                    raise ValueError()
                for result in results:
                    if result.get("currency", "USD").upper() != "USD":
                        raise ValueError()
                    cents = Decimal(str(result["amount"]))
                    if not cents.is_finite() or not 0 <= cents < Decimal("1e18"):
                        raise ValueError()
                    month_cents += cents
                    if day >= today:
                        today_cents += cents
            more = payload.get("has_more", False)
            if not isinstance(more, bool):
                raise ValueError()
            if not more:
                break
            page = payload["next_page"]
            if not isinstance(page, str) or not page or page in seen:
                raise ValueError()
            seen.add(page)
            query["page"] = page
        except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation, OverflowError):
            out["err"] = "bad billing report"
            return out
    else:
        out["err"] = "billing report incomplete"
        return out
    out["metrics"] = [dict(M(label, extra=f"${cents / 100:,.2f}"), keep_zero=True)
                      for label, cents in (("This month", month_cents), ("Today (UTC)", today_cents))]
    out["account"] = {"mode": "spend", "billing_url": True}
    return out


def collect(spec, http, header_get, M, timeout=10):
    """Read one spec and return Fleet's provider dict."""
    settings = spec.get("_settings") or {}
    out = dict(agent=spec["label"], plan="", metrics=[], err="", api_id=spec["id"], compact=True)
    _, key = _key_for(spec)
    if not key and spec.get("auth") != "none":
        out["err"] = "no key"
        return out

    if spec.get("collector") == "anthropic_cost":
        return _anthropic_cost(spec, key, http, M, timeout)
    if spec.get("connection_only"):
        out["credential_id"] = hashlib.sha256(key.encode()).hexdigest()
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Accept": "application/json"}
        status, response_headers, _ = http("GET", "https://api.anthropic.com/v1/models?limit=1", headers, timeout=timeout)
        out.update(plan="API key", account={"mode": "connection", "billing_url": True})
        if status == 200:
            out["metrics"] = [M("Credit balance", extra="View in Console")]
        else:
            out["err"] = _anthropic_error(status)
            if status == 429:
                out["retry_at"] = _retry_at(response_headers)
        return out

    probe = spec.get("probe")
    if probe:
        model = settings.get("model") or probe.get("model")
        base = settings.get("base_url") or probe["base"]
        url = base.rstrip("/") + ("/messages" if probe["style"] == "anthropic" else "/chat/completions")
        refusal = _check_url(url, spec)
        if refusal:
            out["err"] = refusal
            return out
        headers = _auth_headers(spec, key)
        headers.update(spec.get("headers") or {})
        if probe["style"] == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            body = {"model": model, "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}]}
        else:
            body = {"model": model, "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}]}
        status, response_headers, _ = http("POST", url, headers, body, timeout=timeout)
        out["metrics"] = metrics_from_headers(response_headers, header_get, M)
        if not out["metrics"]:
            out["err"] = "no rate headers" if status in (200, 429) else f"probe {status or 'offline'}"
        out["plan"] = "probe"
        return out

    url = _url_for(spec, settings)
    if not url:
        out["err"] = "no base url"
        return out
    refusal = _check_url(url, spec)
    if refusal:
        out["err"] = refusal
        return out
    url = _with_since(url, spec, spec.get("query"))
    headers = dict(spec.get("headers") or {})
    headers.setdefault("Accept", "application/json")
    headers.update(_auth_headers(spec, key))
    status, _, body = http("GET", url, headers, timeout=timeout)
    response_spec = spec
    fallback = spec.get("fallback") or {}
    if status in (fallback.get("statuses") or []):
        fallback_url = fallback.get("url")
        refusal = _check_url(fallback_url or "", spec)
        if refusal:
            out["err"] = refusal
            return out
        fallback_url = _with_since(fallback_url, fallback, fallback.get("query"))
        fallback_headers = dict(headers)
        fallback_headers.update(fallback.get("headers") or {})
        status, _, body = http("GET", fallback_url, fallback_headers, timeout=timeout)
        response_spec = {**spec, **fallback}
    if status != 200:
        out["err"] = f"http {status}" if status else "offline"
        return out
    if len(body or b"") > 1_000_000:  # a usage endpoint returning a megabyte is not one
        out["err"] = "payload too large"
        return out
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        payload = None
    if not isinstance(payload, (dict, list)):
        out["err"] = "bad payload"
        return out
    out["metrics"] = build_metrics(payload, response_spec.get("metrics") or [], M)
    out["plan"] = _plan_text(response_spec, payload)
    if not out["metrics"]:
        out["err"] = "no usage fields"
    return out
