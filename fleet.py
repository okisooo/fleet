#!/usr/bin/env python
"""
fleet.py - unified live usage view for Claude Code, Codex, Grok, Antigravity (Windows).

Two data modes per agent:
  * API  - real subscription limits (session %, weekly %, resets, credits, $)
           via each vendor's usage endpoint, same ones openusage uses.
  * logs - local token tallies scraped from session JSONL (fallback / --logs).

Token handling:
  * Reads credentials only from local CLI/app state.
  * Claude refreshes through Claude Code first; Grok may atomically update its
    own CLI credential file when its provider rotates a token.
  * Runtime cache stays in ./.cache during development and LocalAppData\\Fleet
    when packaged. Tokens never enter logs.

Run:  uv run --with rich python fleet.py            # live API view, 30s refresh
      uv run --with rich python fleet.py --once
      uv run --with rich python fleet.py --logs      # force log-parse mode
      uv run --with rich python fleet.py --debug     # print provider errors
Endpoints/field shapes cribbed from openusage (MIT, robinebers/openusage).
"""
from __future__ import annotations
import argparse, base64, concurrent.futures, email.utils, glob, json, os, re, sqlite3, subprocess, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apikeys

for _s in (sys.stdout, sys.stderr):  # Windows consoles default to cp932/cp1252
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HOME = Path(os.path.expanduser("~"))
APPDATA = Path(os.path.expandvars("%APPDATA%"))
_BUNDLED = bool(getattr(sys, "frozen", False))
_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Fleet" if _BUNDLED else Path(__file__).resolve().parent
CACHE = _ROOT / ".cache"
# parents=True: a portable (unzipped) build is the first thing to create
# LocalAppData\Fleet, so the parent does not exist yet on a clean machine.
CACHE.mkdir(parents=True, exist_ok=True)
DEBUG = False
REFRESH = False  # Codex-only opt-in refresh; may rotate its CLI token


def dbg(*a):
    if DEBUG:
        print("[dbg]", *a, file=sys.stderr)


# ---------- tiny http ----------
def _http(method, url, headers=None, body=None, form=False, timeout=12):
    data = None
    headers = dict(headers or {})
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:
        dbg("http", type(e).__name__)
        return 0, {}, str(e).encode()


def _jget(url, token, extra_headers=None, timeout=12):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    h.update(extra_headers or {})
    st, hd, bd = _http("GET", url, h, timeout=timeout)
    try:
        return st, hd, json.loads(bd) if bd else {}
    except ValueError:
        return st, hd, {}


def _header(headers, name):
    """Case-insensitive HTTP header lookup (urllib preserves server casing)."""
    wanted = name.casefold()
    return next((v for k, v in headers.items() if k.casefold() == wanted), None)


def _retry_after(headers, default=300):
    raw = _header(headers, "Retry-After")
    if raw:
        try:
            return max(1, int(float(raw)))
        except ValueError:
            try:
                return max(1, int(email.utils.parsedate_to_datetime(raw).timestamp() - time.time()))
            except (TypeError, ValueError):
                pass
    return default


# ---------- token cache (fleet-owned, never the CLI's files) ----------
def cache_get(agent):
    f = CACHE / f"{agent}.json"
    if f.exists():
        try:
            d = json.loads(f.read_text())
            if d.get("access_token") and d.get("exp", 0) > time.time() + 60:
                return d
        except Exception:
            pass
    return None


def cache_put(agent, access_token, exp, **extra):
    try:
        (CACHE / f"{agent}.json").write_text(json.dumps(dict(access_token=access_token, exp=exp, **extra)))
    except Exception as e:
        dbg("cache_put", agent, e)


def _jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
    except Exception:
        return 0


# ---------- helpers ----------
def _pct_bar(pct, w=14):
    pct = max(0.0, min(100.0, float(pct)))
    fill = int(round(pct / 100 * w))
    color = "green" if pct < 60 else ("yellow" if pct < 85 else "red")
    return f"[{color}]{'█'*fill}{'░'*(w-fill)}[/] {pct:4.0f}%"


def _until(iso_or_ms):
    """human 'in 3h' from ISO8601 str or epoch ms/sec."""
    ts = None
    if iso_or_ms in (None, "", 0):
        return "-"
    try:
        if isinstance(iso_or_ms, (int, float)):
            ts = float(iso_or_ms) / (1000 if iso_or_ms > 1e12 else 1)
        else:
            s = str(iso_or_ms).replace("Z", "+00:00")
            ts = datetime.fromisoformat(s).timestamp()
    except Exception:
        return "-"
    d = ts - time.time()
    if d <= 0:
        return "now"
    if d < 5400:
        return f"{int(d/60)}m"
    if d < 172800:
        return f"{int(d/3600)}h"
    return f"{int(d/86400)}d"


def M(label, pct=None, resets=None, extra=""):
    return dict(label=label, pct=pct, resets=resets, extra=extra)


def _save_last(agent, out):
    """Persist a successful reading so we can show it (with age) when the live source is down."""
    if not out.get("metrics"):
        return
    try:
        (CACHE / f"{agent}_last.json").write_text(json.dumps(
            {"ts": time.time(), "plan": out.get("plan", ""), "metrics": out["metrics"]}))
    except Exception:
        pass


def _load_last(agent, max_age=86400):
    """Return (metrics, plan, age_str, timestamp), or None if missing/too old."""
    try:
        d = json.loads((CACHE / f"{agent}_last.json").read_text())
        age = time.time() - d["ts"]
        if age > max_age:
            return None
        return d["metrics"], d.get("plan", ""), _age(d["ts"]), d["ts"]
    except Exception:
        return None


# ================= PROVIDERS (API) =================
# each returns dict: agent, plan, metrics[list of M], err

_CLAUDE_UA = None


def _claude_user_agent():
    global _CLAUDE_UA
    if _CLAUDE_UA is None:
        try:
            p = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            version = re.search(r"\d+(?:\.\d+)+", p.stdout)
            _CLAUDE_UA = f"claude-code/{version.group()}" if version else "claude-code/2.1.0"
        except Exception:
            _CLAUDE_UA = "claude-code/2.1.0"
    return _CLAUDE_UA


def _claude_metrics_from_headers(headers):
    """Read the same unified 5h/7d headers Claude Usage Tracker uses."""
    def number(name):
        try:
            return float(_header(headers, name))
        except (TypeError, ValueError):
            return None

    five = number("anthropic-ratelimit-unified-5h-utilization")
    week = number("anthropic-ratelimit-unified-7d-utilization")
    if five is None and week is None:
        return []
    metrics = []
    if five is not None:
        metrics.append(M("Session", five * 100, number("anthropic-ratelimit-unified-5h-reset")))
    if week is not None:
        metrics.append(M("Weekly", week * 100, number("anthropic-ratelimit-unified-7d-reset")))
    return metrics


def _claude_messages_metrics(token, headers):
    """One-token fallback used only when the dedicated usage endpoint is unavailable."""
    h = dict(headers)
    h["Authorization"] = f"Bearer {token}"
    h["anthropic-version"] = "2023-06-01"
    st, hd, _ = _http(
        "POST", "https://api.anthropic.com/v1/messages", h,
        {"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
         "messages": [{"role": "user", "content": "hi"}]},
    )
    metrics = _claude_metrics_from_headers(hd)
    return metrics if st in (200, 429) else []


def api_claude(force=False):
    out = dict(agent="Claude", plan="", metrics=[], err="")
    cred = HOME / ".claude" / ".credentials.json"
    try:
        full = json.loads(cred.read_text())
        o = full.get("claudeAiOauth", {})
        out["plan"] = str(o.get("subscriptionType") or o.get("rateLimitTier") or "")
    except Exception as e:
        out["err"] = f"no creds ({e.__class__.__name__})"
        return out
    rt = o.get("refreshToken")
    tok = o.get("accessToken")
    exp = o.get("expiresAt", 0)
    exp = exp / 1000 if exp and exp > 1e12 else exp
    if not tok:
        out["err"] = "no accessToken"
        return out
    # Auth and usage have separate throttles: a busy usage endpoint must never
    # stop an expired token from being refreshed.
    auth_cool = 0 if force else _refresh_cooldown_left("claude_refresh")
    if rt and exp and exp < time.time() + 60 and auth_cool:
        out["err"] = f"auth retry in {max(1, int(auth_cool/60))}m"
        return _cached_or(out, "claude")
    # Proactive refresh if expired, then persist atomically so Claude Code stays in sync.
    if rt and exp and exp < time.time() + 60:
        refreshed = _refresh_claude_writeback(rt, cred, full)
        if refreshed:
            o = json.loads(cred.read_text()).get("claudeAiOauth", {})
            tok = o.get("accessToken", tok)
            exp = o.get("expiresAt", 0)
            exp = exp / 1000 if exp and exp > 1e12 else exp
        elif _refresh_cooldown_left("claude_refresh"):
            out["err"] = "auth temporarily busy"
            return _cached_or(out, "claude")
    usage_cool = 0 if force else _refresh_cooldown_left("claude_usage")
    if usage_cool:
        out["err"] = f"usage retry in {max(1, int(usage_cool/60))}m"
        return _cached_or(out, "claude")
    hdrs = {"anthropic-beta": "oauth-2025-04-20", "User-Agent": _claude_user_agent(),
            "Content-Type": "application/json"}
    st, hd, j = _jget("https://api.anthropic.com/api/oauth/usage", tok, hdrs)
    if st == 401 and rt and (force or not _refresh_cooldown_left("claude_refresh")):
        if _refresh_claude_writeback(rt, cred, full):
            tok = json.loads(cred.read_text()).get("claudeAiOauth", {}).get("accessToken", tok)
            st, hd, j = _jget("https://api.anthropic.com/api/oauth/usage", tok, hdrs)
    if st == 401:
        out["err"] = "token expired (login Claude Code)"
        return _cached_or(out, "claude")
    if st != 200 or not isinstance(j, dict):
        # The dedicated endpoint is frequently throttled/disabled. A minimal
        # Messages call exposes exact subscription utilization in response headers,
        # including on 429. Cache it for five minutes so this never becomes polling spam.
        _set_cooldown("claude_usage", _retry_after(hd))
        out["metrics"] = _claude_messages_metrics(tok, hdrs)
        if not out["metrics"]:
            out["err"] = f"http {st}"
            return _cached_or(out, "claude")
        _save_last("claude", out)
        return out
    if isinstance(j.get("five_hour"), dict):
        out["metrics"].append(M("Session", j["five_hour"].get("utilization", j["five_hour"].get("percent")),
                                j["five_hour"].get("resets_at")))
    elif j.get("five_hour") is not None:
        out["metrics"].append(M("Session", j.get("five_hour"), j.get("resets_at")))
    if j.get("seven_day") is not None:
        sd = j["seven_day"]
        out["metrics"].append(M("Weekly", sd.get("utilization", sd.get("percent")) if isinstance(sd, dict) else sd,
                                sd.get("resets_at") if isinstance(sd, dict) else None))
    for lim in (j.get("limits") or []):
        if lim.get("kind") == "weekly_scoped":
            nm = (((lim.get("scope") or {}).get("model") or {}).get("display_name")) or "model"
            out["metrics"].append(M(nm, lim.get("percent"), lim.get("resets_at")))
    ex = j.get("extra_usage") or {}
    if ex.get("is_enabled"):
        out["metrics"].append(M("Extra $", None, None, f"${ex.get('used_credits',0)/100:.2f}"))
    _save_last("claude", out)
    return out


def _cached_or(out, agent):
    """If a provider failed (no metrics), fall back to the last good reading with an age note."""
    if out.get("metrics"):
        return out
    last = _load_last(agent)
    if last:
        mets, plan, age, cached_at = last
        return dict(agent=out["agent"], plan=out.get("plan") or plan, metrics=mets,
                    err=f"cached {age} ago · {out.get('err','')}".strip(" ·"),
                    cached_at=cached_at)
    return out


def _refresh_cooldown_left(agent, secs=300):
    """Seconds left in a backoff; accepts old start timestamps and new expiry timestamps."""
    f = CACHE / f"{agent}_backoff"
    try:
        stamp = float(f.read_text())
        return max(0, stamp - time.time()) if stamp > time.time() else max(0, secs - (time.time() - stamp))
    except Exception:
        return 0


def _set_cooldown(agent, secs=300):
    try:
        (CACHE / f"{agent}_backoff").write_text(str(time.time() + max(1, secs)))
    except Exception:
        pass


def _refresh_claude_via_cli(cred_path):
    """Let Claude Code rotate its own credentials; never inspect or expose its output."""
    try:
        subprocess.run(
            ["claude", "-p", "/status", "--output-format", "json", "--no-session-persistence"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        o = json.loads(cred_path.read_text()).get("claudeAiOauth", {})
        exp = o.get("expiresAt", 0)
        exp = exp / 1000 if exp and exp > 1e12 else exp
        return bool(o.get("accessToken") and exp > time.time() + 60)
    except Exception as e:
        dbg("claude cli refresh", e.__class__.__name__)
        return False


def _refresh_claude_writeback(rt, cred_path, full):
    """Refresh, then persist new access+refresh+expiry back to .credentials.json (atomic),
    preserving every other field — mirrors openusage save(), keeps Claude Code in sync."""
    if _refresh_claude_via_cli(cred_path):
        try:
            (CACHE / "claude_refresh_backoff").unlink()
        except OSError:
            pass
        return True
    st, hd, bd = _http("POST", "https://platform.claude.com/v1/oauth/token",
                       {"User-Agent": _claude_user_agent(), "Accept": "application/json"},
                       {"grant_type": "refresh_token", "refresh_token": rt,
                        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e"})
    dbg("claude refresh http", st)
    if st == 429:
        _set_cooldown("claude_refresh", _retry_after(hd))
        return False
    try:
        j = json.loads(bd)
        at = j["access_token"]
    except Exception:
        return False
    o = dict(full.get("claudeAiOauth", {}))
    o["accessToken"] = at
    if j.get("refresh_token"):
        o["refreshToken"] = j["refresh_token"]
    o["expiresAt"] = int((time.time() + j.get("expires_in", 1800)) * 1000)
    full = dict(full)
    full["claudeAiOauth"] = o
    try:
        tmp = cred_path.with_suffix(".json.fleet-tmp")
        tmp.write_text(json.dumps(full, indent=2))
        os.replace(tmp, cred_path)  # atomic
    except Exception as e:
        dbg("claude writeback", e)
        return False
    try:
        (CACHE / "claude_refresh_backoff").unlink()
    except OSError:
        pass
    return True


def _window_label(window, fallback="Usage"):
    """Name a rate-limit window from its duration instead of API field position."""
    seconds = window.get("limit_window_seconds") or window.get("window_seconds") or 0
    if seconds and seconds <= 6 * 3600:
        return "Session"
    if seconds and seconds <= 36 * 3600:
        return "Daily"
    if seconds and 5 * 86400 <= seconds <= 9 * 86400:
        return "Weekly"
    if seconds and 25 * 86400 <= seconds <= 35 * 86400:
        return "Monthly"
    return fallback


def api_codex():
    out = dict(agent="Codex", plan="", metrics=[], err="")
    tok = acct = None
    for p in [os.environ.get("CODEX_HOME"), HOME / ".codex", HOME / ".config" / "codex"]:
        if not p:
            continue
        f = Path(p) / "auth.json"
        try:
            a = json.loads(f.read_text())
        except Exception:
            continue
        t = a.get("tokens") or a
        tok = t.get("access_token")
        acct = t.get("account_id") or a.get("account_id")
        rt = t.get("refresh_token")
        if REFRESH and tok and _jwt_exp(tok) < time.time() + 60 and rt:
            r = _refresh_codex(rt)
            if r:
                tok = r
        break
    if not tok:
        out["err"] = "no codex token"
        return out
    hdr = {"User-Agent": "OpenUsage"}
    if acct:
        hdr["ChatGPT-Account-Id"] = acct
    st, hd, j = _jget("https://chatgpt.com/backend-api/wham/usage", tok, hdr)
    if st == 401 and rt:  # expired -> refresh in memory, retry once
        r = _refresh_codex(rt)
        if r:
            tok = r
            st, hd, j = _jget("https://chatgpt.com/backend-api/wham/usage", tok, hdr)
    if st != 200 or not isinstance(j, dict):
        out["err"] = ("token expired (run codex once)" if st == 401 else f"http {st}")
        return _cached_or(out, "codex")
    out["plan"] = j.get("plan_type", "")
    for source, prefix in (("rate_limit", ""), ("code_review_rate_limit", "Review ")):
        rl = j.get(source) or {}
        windows = [(key, rl.get(key) or {}) for key in ("primary_window", "secondary_window")]
        populated = [(key, window) for key, window in windows if window]
        for key, window in populated:
            fallback = "Session" if key == "primary_window" else "Weekly"
            label = _window_label(window, fallback)
            if prefix:
                label = "Review" if len(populated) == 1 else prefix + label
            out["metrics"].append(
                M(
                    label,
                    window.get("used_percent"),
                    window.get("reset_at")
                    or (time.time() + window.get("reset_after_seconds", 0)),
                )
            )
    for w in (j.get("additional_rate_limits") or []):
        out["metrics"].append(M(w.get("name") or w.get("limit_name") or "extra", w.get("used_percent"),
                                w.get("reset_at") or (time.time() + (w.get("reset_after_seconds", 0) or 0))))
    cr = j.get("credits") or {}
    if cr:
        out["metrics"].append(M("Credits", None, None, f"${cr.get('balance',0)}"))
    _save_last("codex", out)
    return out


def _refresh_codex(rt):
    st, hd, bd = _http("POST", "https://auth.openai.com/oauth/token", None,
                       {"grant_type": "refresh_token", "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                        "refresh_token": rt}, form=True)
    try:
        return json.loads(bd)["access_token"]
    except Exception:
        return None


def api_grok():
    out = dict(agent="Grok", plan="", metrics=[], err="")
    try:
        auth = json.loads((HOME / ".grok" / "auth.json").read_text())
    except Exception as e:
        out["err"] = f"no creds ({e.__class__.__name__})"
        return out
    ekey, entry = None, None
    if isinstance(auth, dict):
        for k, v in auth.items():
            if isinstance(v, dict) and v.get("key"):
                ekey, entry = k, v
                break
    if not entry:
        out["err"] = "no grok entry"
        return out
    tok = (entry.get("key") or "").strip()
    try:
        exp = float(entry.get("expires_at") or 0)
    except (TypeError, ValueError):
        exp = 0
    if exp > 1e12:
        exp /= 1000
    if exp and exp < time.time() + 60 and entry.get("refresh_token"):  # proactive
        r = _refresh_grok_writeback(auth, ekey, entry)
        if r:
            tok = r
    h = {"X-XAI-Token-Auth": "xai-grok-cli", "User-Agent": "OpenUsage"}
    st, hd, cred = _jget("https://cli-chat-proxy.grok.com/v1/billing?format=credits", tok, h)
    if st == 401 and entry.get("refresh_token"):  # reactive: token died early, refresh + retry
        r = _refresh_grok_writeback(auth, ekey, entry)
        if r:
            tok = r
            st, hd, cred = _jget("https://cli-chat-proxy.grok.com/v1/billing?format=credits", tok, h)
    if st != 200:
        out["err"] = ("token expired (run grok once)" if st == 401 else f"http {st}")
        return _cached_or(out, "grok")
    _, _, settings = _jget("https://cli-chat-proxy.grok.com/v1/settings", tok, h)
    out["plan"] = (settings or {}).get("subscription_tier_display", "") if isinstance(settings, dict) else ""

    def _v(x):  # unwrap {"val": n} or scalar
        return x.get("val") if isinstance(x, dict) else x

    cfg = (cred or {}).get("config", {}) if isinstance(cred, dict) else {}
    cp = cfg.get("currentPeriod", {}) or {}
    if cfg.get("creditUsagePercent") is not None:
        wk = "WEEKLY" in str(cp.get("type", ""))
        out["metrics"].append(M("Weekly" if wk else "Usage", cfg["creditUsagePercent"], cp.get("end")))
    for pu in (cfg.get("productUsage") or []):
        if pu.get("usagePercent") is not None and abs(pu["usagePercent"] - (cfg.get("creditUsagePercent") or -1)) > 0.5:
            out["metrics"].append(M(pu.get("product", "product"), pu["usagePercent"], cp.get("end")))

    # plain billing -> monthly credit pool used/limit
    _, _, bill = _jget("https://cli-chat-proxy.grok.com/v1/billing", tok, h)
    b = (bill or {}).get("config", {}) if isinstance(bill, dict) else {}
    lim, used = _v(b.get("monthlyLimit")), _v(b.get("used"))
    if lim:
        out["metrics"].append(M("Monthly", (used or 0) / lim * 100, b.get("billingPeriodEnd"),
                                f"{used or 0}/{lim} cr"))
    cap = _v(cfg.get("onDemandCap")) or _v(b.get("onDemandCap")) or 0
    out["metrics"].append(M("Pay-go", None, None, str(cap) if cap else "off"))
    _save_last("grok", out)
    return out


def _refresh_grok_writeback(auth, ekey, entry):
    """Refresh the xAI token and persist it back to ~/.grok/auth.json so the grok CLI
    keeps working (same approach as the Claude path)."""
    st, hd, bd = _http("POST", "https://auth.x.ai/oauth2/token", None,
                       {"grant_type": "refresh_token", "client_id": entry.get("oidc_client_id", ""),
                        "refresh_token": entry.get("refresh_token", "")}, form=True)
    dbg("grok refresh http", st)
    try:
        j = json.loads(bd)
        at = j["access_token"]
    except Exception:
        return None
    e = dict(entry)
    e["key"] = at
    if j.get("refresh_token"):
        e["refresh_token"] = j["refresh_token"]
    if j.get("expires_in"):
        e["expires_at"] = time.time() + j["expires_in"]
    try:
        full = dict(auth)
        full[ekey] = e
        p = HOME / ".grok" / "auth.json"
        tmp = p.with_suffix(".json.fleet-tmp")
        tmp.write_text(json.dumps(full, indent=2))
        os.replace(tmp, p)  # atomic
    except Exception as ex:
        dbg("grok writeback", ex)
    return at


_AGY_BUCKETS = {"gemini-5h": "Session", "gemini-weekly": "Weekly", "3p-5h": "Claude", "3p-weekly": "Claude Wk"}


def _agy_parse_summary(j):
    groups = ((j.get("response") or j).get("groups")) if isinstance(j, dict) else None
    mets = []
    for g in (groups or []):
        for b in g.get("buckets", []):
            rf = b.get("remainingFraction")
            if rf is not None:
                mets.append(M(_AGY_BUCKETS.get(b.get("bucketId"), b.get("bucketId", "pool")),
                              (1 - float(rf)) * 100, b.get("resetTime")))
    return mets


def _agy_ls_endpoint():
    """Find running Antigravity language_server: its csrf token + listening TCP ports.
    Port isn't a flag (--https_server_port 0 = auto); read the actual LISTEN sockets by PID."""
    import subprocess
    ps = r"""
$out=@()
Get-CimInstance Win32_Process -Filter "Name LIKE 'language_server%'" | ForEach-Object {
  $cl=$_.CommandLine
  if ($cl -match 'antigravity' -and $cl -match '--csrf_token[= ]+([0-9a-fA-F\-]+)') {
    $csrf=$Matches[1]
    $ports=Get-NetTCPConnection -OwningProcess $_.ProcessId -State Listen -EA SilentlyContinue |
           Where-Object {$_.LocalAddress -eq '127.0.0.1'} | Select-Object -Expand LocalPort -Unique
    $out += [pscustomobject]@{csrf=$csrf;ports=@($ports)}
  }
}
$out | ConvertTo-Json -Compress
"""
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=8,
                             creationflags=0x08000000).stdout.strip()
        j = json.loads(raw) if raw else None
    except Exception as e:
        dbg("agy ls scan", e)
        return None
    if isinstance(j, list):
        j = j[0] if j else None
    if not j or not j.get("csrf"):
        return None
    ports = j.get("ports")
    ports = [ports] if isinstance(ports, int) else list(ports or [])
    return (ports, j["csrf"]) if ports else None


_AGY_SSL = None


def _agy_ls_call(ports, csrf, method):
    """POST the connect-RPC; try each listening port over http then https (self-signed, unverified)."""
    global _AGY_SSL
    if _AGY_SSL is None:
        import ssl
        _AGY_SSL = ssl.create_default_context()
        _AGY_SSL.check_hostname = False
        _AGY_SSL.verify_mode = ssl.CERT_NONE
    body = json.dumps({"metadata": {"ideName": "antigravity", "extensionName": "antigravity",
                                    "ideVersion": "unknown", "locale": "en"}}).encode()
    hdr = {"Content-Type": "application/json", "Connect-Protocol-Version": "1",
           "x-codeium-csrf-token": csrf}
    svc = "exa.language_server_pb.LanguageServerService"
    for port in ports:
        for scheme in ("http", "https"):
            url = f"{scheme}://127.0.0.1:{port}/{svc}/{method}"
            req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
            try:
                ctx = _AGY_SSL if scheme == "https" else None
                with urllib.request.urlopen(req, timeout=6, context=ctx) as r:
                    return 200, json.loads(r.read()), (port, scheme)
            except Exception as e:
                dbg("agy ls", scheme, port, method, type(e).__name__)
    return 0, {}, None


def api_antigravity():
    """Live quota via Antigravity's local language server, then short-lived local access token."""
    out = dict(agent="Antigrav", plan="gemini", metrics=[], err="")

    ep = _agy_ls_endpoint()
    if ep:
        ports, csrf = ep
        st, j, where = _agy_ls_call(ports, csrf, "RetrieveUserQuotaSummary")
        mets = _agy_parse_summary(j)
        if mets:
            out["metrics"] = mets
            hit = [where[0]] if where else ports  # reuse the port/scheme that worked
            _, us, _ = _agy_ls_call(hit, csrf, "GetUserStatus")
            plan = ((((us or {}).get("userStatus") or {}).get("planStatus") or {}).get("planInfo") or {}).get("planName")
            if plan:
                out["plan"] = str(plan)
            _save_last("antigravity", out)
            return out
        dbg("agy ls http", st)

    tok = _agy_token()  # never refresh using another application's OAuth client
    if tok:
        for host in ("https://daily-cloudcode-pa.googleapis.com", "https://cloudcode-pa.googleapis.com"):
            st, hd, bd = _http("POST", f"{host}/v1internal:retrieveUserQuotaSummary",
                               {"Authorization": f"Bearer {tok}", "Accept": "application/json",
                                "User-Agent": "antigravity"}, {})
            try:
                j = json.loads(bd)
            except Exception:
                j = {}
            mets = _agy_parse_summary(j) if st == 200 else []
            if mets:
                out["metrics"] = mets
                _save_last("antigravity", out)
                return out

    last = _load_last("antigravity")  # app closed -> show last live quota with age
    if last:
        mets, plan, age, cached_at = last
        out["metrics"] = mets
        out["plan"] = plan
        out["err"] = f"cached {age} ago (AGY closed)"
        out["cached_at"] = cached_at
        return out
    out["plan"] = ""
    out["metrics"].append(M("closed", None, None, f"open AGY · bal {_agy_credit_balance()}"))
    out["err"] = "Antigravity not running"
    return out


def _agy_extract_tokens(raw):
    """Extract access + refresh tokens from Antigravity's nested base64 protobuf value."""
    if isinstance(raw, str):
        raw = raw.encode()
    queue = [bytes(raw)]
    seen = set()
    access = refresh = None
    while queue and len(seen) < 24:
        blob = queue.pop(0)
        if blob in seen:
            continue
        seen.add(blob)
        if not access:
            hit = re.search(rb"ya29\.[A-Za-z0-9._-]+", blob)
            access = hit.group(0).decode() if hit else None
        if not refresh:
            hit = re.search(rb"1//[A-Za-z0-9._-]+", blob)
            refresh = hit.group(0).decode() if hit else None
        if access and refresh:
            return access, refresh
        for encoded in re.findall(rb"[A-Za-z0-9+/=_-]{32,}", blob):
            # A printable protobuf length byte can touch the base64 payload.
            for trim in range(min(4, len(encoded))):
                candidate = encoded[trim:]
                try:
                    decoded = base64.b64decode(
                        candidate + b"=" * (-len(candidate) % 4),
                        altchars=b"-_",
                        validate=True,
                    )
                except Exception:
                    continue
                if decoded and decoded != candidate:
                    queue.append(decoded)
    return access, refresh


def _agy_stored_tokens():
    for db in glob.glob(str(APPDATA / "Antigravity" / "User" / "**" / "state.vscdb"), recursive=True):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            r = c.execute("SELECT value FROM ItemTable WHERE key='antigravityUnifiedStateSync.oauthToken'").fetchone()
            c.close()
        except sqlite3.Error:
            continue
        if not r:
            continue
        tokens = _agy_extract_tokens(r[0])
        if any(tokens):
            return tokens
    return None, None


def _agy_token():
    cached = cache_get("antigravity")
    if cached:
        return cached["access_token"]
    access, _ = _agy_stored_tokens()
    return access


def _agy_credit_balance():
    for db in glob.glob(str(APPDATA / "Antigravity" / "User" / "**" / "state.vscdb"), recursive=True):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            r = c.execute("SELECT value FROM ItemTable WHERE key='antigravityUnifiedStateSync.modelCredits'").fetchone()
            c.close()
        except sqlite3.Error:
            continue
        if not r:
            continue
        try:
            blob = base64.b64decode(r[0].decode() if isinstance(r[0], (bytes, bytearray)) else r[0])
            for m in re.finditer(rb"availableCredits\w*Key.{0,4}\n\x04([A-Za-z0-9+/=]{4})", blob):
                inner = base64.b64decode(m.group(1))
                if len(inner) >= 2 and inner[0] == 0x10:
                    return inner[1]
        except Exception:
            pass
    return "?"


API_PROVIDERS = [api_claude, api_codex, api_grok, api_antigravity]


# ================= PUBLIC API KEYS + CUSTOM ENTRIES =================
def _api_key_specs():
    config, err = apikeys.load_config(_ROOT)
    if err:
        dbg("providers.json", err)
    return apikeys.enabled_specs(config), err


def _collect_one_spec(spec):
    agent = f"api_{spec['id']}"
    try:
        out = apikeys.collect(spec, _http, _header, M)
    except Exception as e:
        dbg("apikey", spec["id"], repr(e))
        out = dict(agent=spec["label"], plan="", metrics=[], err=f"crash: {e.__class__.__name__}",
                   api_id=spec["id"], compact=True)
    if out.get("metrics"):
        _save_last(agent, out)
        return out
    cached = _cached_or(out, agent)
    cached["compact"] = True
    cached["api_id"] = spec["id"]
    return cached


def collect_api_keys(specs=None):
    """Read every configured API-key provider at once; they are independent."""
    specs = _api_key_specs()[0] if specs is None else specs
    if not specs:
        return []
    workers = min(8, len(specs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_collect_one_spec, specs))


def collect_api(force_claude=False, include_api_keys=True):
    out = []
    specs = _api_key_specs()[0] if include_api_keys else []
    pool = pending = None
    if specs:
        # Started first: they are pure HTTP and finish while the local providers
        # are still reading credential files and shelling out to the CLIs.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(specs)))
        pending = [pool.submit(_collect_one_spec, spec) for spec in specs]
    try:
        for p in API_PROVIDERS:
            try:
                out.append(p(force=force_claude) if p is api_claude else p())
            except Exception as e:
                dbg(p.__name__, repr(e))
                out.append(dict(agent=p.__name__.replace("api_", "").title()[:8], plan="",
                                metrics=[], err=f"crash: {e.__class__.__name__}"))
        for future in pending or []:
            try:
                out.append(future.result(timeout=30))
            except Exception as e:
                dbg("apikey future", repr(e))
    finally:
        if pool is not None:
            pool.shutdown(wait=False)
    return out


# ================= LOG MODE (fallback, from v1) =================
def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _k(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n/1000:.1f}k"
    return f"{n/1_000_000:.2f}M"


def _age(ts):
    if not ts:
        return "-"
    d = max(0, time.time() - ts)
    return f"{int(d)}s" if d < 90 else f"{int(d/60)}m" if d < 5400 else f"{int(d/3600)}h" if d < 172800 else f"{int(d/86400)}d"


def collect_logs(limit):
    rows = []

    def add(agent, session, model, tin, tout, ctx, extra, ts):
        rows.append(dict(agent=agent, session=session, model=model, tin=tin, tout=tout, ctx=ctx, extra=extra, ts=ts))

    cf = sorted(glob.glob(str(HOME / ".claude" / "projects" / "*" / "*.jsonl")), key=_mtime, reverse=True)
    for f in cf[:limit]:
        tin = tout = ctx = 0
        model = "-"
        try:
            for line in open(f, encoding="utf-8", errors="ignore"):
                if '"usage"' not in line:
                    continue
                o = json.loads(line)
                u = (o.get("message") or {}).get("usage") or o.get("usage") or {}
                if not u:
                    continue
                model = (o.get("message") or {}).get("model") or model
                tin += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
                tout += u.get("output_tokens") or 0
                ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        except Exception:
            continue
        add("Claude", Path(f).stem[:8], model, tin, tout, ctx, "", _mtime(f))

    xf = sorted(glob.glob(str(HOME / ".codex" / "sessions" / "*" / "*" / "*" / "*.jsonl")), key=_mtime, reverse=True)
    for f in xf[:limit]:
        last, model = None, "-"
        try:
            for line in open(f, encoding="utf-8", errors="ignore"):
                if '"model"' in line:
                    mm = re.search(r'"model":"([^"]+)"', line)
                    model = mm.group(1) if mm else model
                if '"total_token_usage"' in line:
                    mm = re.search(r'"total_token_usage":(\{[^}]*\})', line)
                    if mm:
                        last = json.loads(mm.group(1))
        except Exception:
            continue
        u = last or {}
        sid = re.sub(r"^rollout-\d{4}-\d\d-\d\dT[\d-]+-", "", Path(f).stem)[:8]
        add("Codex", sid, model, u.get("input_tokens", 0), u.get("output_tokens", 0),
            u.get("total_tokens", 0), f"cache {_k(u.get('cached_input_tokens',0))}", _mtime(f))

    for f in sorted(glob.glob(str(HOME / ".grok" / "sessions" / "*" / "*" / "signals.json")), key=_mtime, reverse=True)[:limit]:
        try:
            s = json.load(open(f, encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        mu = s.get("modelsUsed") or [s.get("primaryModelId")]
        add("Grok", Path(f).parent.name[:8], (mu[0] if mu else "-") or "-",
            s.get("contextTokensUsed", 0), 0, s.get("contextTokensUsed", 0),
            f"{s.get('turnCount',0)}t ctx{s.get('contextWindowUsage',0)}%", _mtime(f))

    add("Antigrav", "credits", "gemini", 0, 0, 0, f"bal {_agy_credit_balance()}",
        _mtime(next(iter(glob.glob(str(APPDATA / 'Antigravity' / 'User' / 'globalStorage' / 'state.vscdb'))), "")))
    return rows


# ================= RENDER =================
COLOR = {"Claude": "orange1", "Codex": "cyan", "Grok": "magenta", "Antigrav": "green"}


def table_api(data):
    from rich.table import Table
    from rich import box
    t = Table(box=box.SIMPLE_HEAVY, expand=True, title_style="bold white",
              title="FLEET — live quota  ·  " + datetime.now().strftime("%H:%M:%S"))
    for c, j in [("Agent", "left"), ("Plan", "left"), ("Metric", "left"), ("Usage", "left"),
                 ("Resets", "right"), ("Extra", "left")]:
        t.add_column(c, justify=j)
    for d in data:
        col = COLOR.get(d["agent"], "white")
        name = f"[{col}]{d['agent']}[/]"
        if not d["metrics"]:
            t.add_row(name, d.get("plan", ""), "[dim]—[/]", "", "", f"[red]{d['err']}[/]")
        else:
            for i, m in enumerate(d["metrics"]):
                usage = _pct_bar(m["pct"]) if m["pct"] is not None else "[dim]—[/]"
                t.add_row(name if i == 0 else "", d.get("plan", "") if i == 0 else "",
                          m["label"], usage, _until(m["resets"]) if m["resets"] else "", m["extra"])
            if d["err"]:
                t.add_row("", "", "", "", "", f"[yellow dim]{d['err']}[/]")
        t.add_section()
    return t


def print_providers():
    """Show which API-key providers are wired up, so setup needs no guesswork."""
    config, err = apikeys.load_config(_ROOT)
    if err:
        print(f"! {err}")
    active = {spec["id"] for spec in apikeys.enabled_specs(config)}
    print(f"config: {apikeys.config_path(_ROOT)}"
          f"{'' if apikeys.config_path(_ROOT).exists() else '  (not created yet)'}\n")
    for spec in apikeys.BUILTIN:
        kind = "probe (opt-in, costs a token)" if spec.get("probe") else "usage endpoint (free)"
        state = "ON " if spec["id"] in active else "   "
        variables = " or ".join(spec.get("env") or ["-"])
        print(f"{state} {spec['id']:<16} {variables:<44} {kind}")
    for spec in (apikeys.normalize_custom(entry, i)
                 for i, entry in enumerate(config.get("custom") or [])):
        if spec:
            print(f"ON  {spec['id']:<16} {' or '.join(spec['env']) or '-':<44} custom entry")
    print("\nSet the variable, then restart Fleet. Probes need \"probe\": true in the config.")


def table_logs(rows):
    from rich.table import Table
    from rich import box
    t = Table(box=box.SIMPLE_HEAVY, expand=True, title_style="bold white",
              title="FLEET — token logs  ·  " + datetime.now().strftime("%H:%M:%S"))
    for c in ["Agent", "Session", "Model", "In", "Out", "Ctx", "Extra", "Age"]:
        t.add_column(c, justify="right" if c in ("In", "Out", "Ctx", "Age") else "left",
                     overflow="ellipsis", max_width=18 if c == "Model" else None)
    for a in ["Claude", "Codex", "Grok", "Antigrav"]:
        grp = sorted([r for r in rows if r["agent"] == a], key=lambda r: r["ts"], reverse=True)
        col = COLOR.get(a, "white")
        for r in grp:
            t.add_row(f"[{col}]{a}[/]", r["session"], r["model"], _k(r["tin"]), _k(r["tout"]),
                      _k(r["ctx"]), r["extra"], _age(r["ts"]))
        if grp:
            t.add_row(f"[{col} dim]{a} Σ[/]", f"[dim]{len(grp)}[/]", "", f"[bold]{_k(sum(r['tin'] for r in grp))}[/]",
                      f"[bold]{_k(sum(r['tout'] for r in grp))}[/]", "", "", "")
        t.add_section()
    return t


def main():
    global DEBUG, REFRESH
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--logs", action="store_true", help="log-parse mode instead of live API")
    ap.add_argument("--limit", type=int, default=6, help="rows/agent in --logs mode")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--refresh", action="store_true",
                    help="opt-in: refresh expired tokens (may rotate the CLI's own token)")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--providers", action="store_true",
                    help="list API-key providers, their variables, and what is enabled")
    a = ap.parse_args()
    DEBUG = a.debug
    REFRESH = a.refresh

    if a.providers:
        print_providers()
        return

    from rich.console import Console
    from rich.live import Live
    console = Console(legacy_windows=False)
    render = (lambda: table_logs(collect_logs(a.limit))) if a.logs else (lambda: table_api(collect_api()))

    if a.once:
        console.print(render())
        return
    try:
        with Live(render(), console=console, refresh_per_second=4, screen=True) as live:
            while True:
                time.sleep(a.interval)
                live.update(render())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
