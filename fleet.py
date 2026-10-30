#!/usr/bin/env python
"""
fleet.py - unified live usage view for Claude Code, Codex, Grok, Antigravity (Windows).

Two data modes per agent:
  * API  - real subscription limits (session %, weekly %, resets, credits, $)
           via each vendor's usage endpoint, same ones openusage uses.
  * logs - local token tallies scraped from session JSONL (fallback / --logs).

Token handling:
  * Reads credentials from local CLI/app state.
  * Claude refreshes through Claude Code first. Grok credentials are strictly
    read-only: only the Grok CLI may refresh or rewrite its own auth state.
  * Runtime cache stays in ./.cache during development and LocalAppData\\Fleet
    when packaged. Tokens never enter logs.

Run:  uv run --with rich python fleet.py            # live API view, 30s refresh
      uv run --with rich python fleet.py --once
      uv run --with rich python fleet.py --logs      # force log-parse mode
      uv run --with rich python fleet.py --debug     # print provider errors
Endpoints/field shapes cribbed from openusage (MIT, robinebers/openusage).
"""
from __future__ import annotations
import argparse, base64, concurrent.futures, ctypes, email.utils, functools, glob, hashlib, json, logging, os, re, shutil, sqlite3, subprocess, sys, threading, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone
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


def _token_id(token):
    """A short one-way fingerprint: enough to recognise a token, useless to replay."""
    return hashlib.sha256(str(token).encode()).hexdigest()[:16]


def _token_dead(agent, token):
    """True while this exact token is known to be rejected. The owning CLI
    writing a new token clears it, so a dead login is never re-sent every poll."""
    try:
        return (CACHE / f"{agent}_dead_token").read_text().strip() == _token_id(token)
    except OSError:
        return False


def _mark_token_dead(agent, token):
    try:
        (CACHE / f"{agent}_dead_token").write_text(_token_id(token))
    except OSError:
        pass


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


def _epoch(iso_or_ms):
    """Epoch seconds from an ISO8601 string or epoch ms/sec, else None."""
    if iso_or_ms in (None, "", 0):
        return None
    try:
        if isinstance(iso_or_ms, (int, float)):
            return float(iso_or_ms) / (1000 if iso_or_ms > 1e12 else 1)
        return datetime.fromisoformat(str(iso_or_ms).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


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
        metadata = {key: out[key] for key in ("credential_id", "account") if key in out}
        (CACHE / f"{agent}_last.json").write_text(json.dumps(
            {"ts": time.time(), "plan": out.get("plan", ""), "metrics": out["metrics"], **metadata}))
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

# The panel offers a one-click sign-in for exactly these two states.
CLAUDE_SIGNED_OUT = "Claude Code is signed out"
CLAUDE_EXPIRED = "Claude Code sign-in expired"


def _claude_cli():
    """Path to the Claude Code CLI, or None. The native installer's default
    location is checked too: a tray started at logon may predate a PATH edit."""
    found = shutil.which("claude")
    if found:
        return found
    native = HOME / ".local" / "bin" / "claude.exe"
    return str(native) if native.exists() else None


def claude_sign_in(on_exit=None):
    """Open a console running `claude auth login`; the CLI owns the whole flow.

    Fleet never sees the browser handoff or the tokens it produces. `on_exit`
    runs once the console closes, so a finished sign-in can be picked up without
    waiting for the next scheduled poll. False when Claude Code is not installed.
    """
    exe = _claude_cli()
    if not exe:
        return False
    process = subprocess.Popen(
        ["cmd.exe", "/c", "title", "Claude", "sign-in", "&", exe, "auth", "login", "&", "pause"],
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )
    if on_exit is not None:
        def wait():
            process.wait()
            on_exit()

        threading.Thread(target=wait, name="claude-sign-in", daemon=True).start()
    return True


FLEET_UA = "Fleet (+https://github.com/okisooo/fleet)"


def _claude_desktop_history():
    return APPDATA / "Claude" / "plan-usage-history.json"


def _claude_desktop_metrics(path=None, max_age=5400):
    """Read the Claude desktop app's own 5-hour and 7-day utilisation samples.

    Claude Code's CLI credentials and the desktop app's session are separate
    logins. Signing in through the app alone leaves claudeAiOauth empty, and
    Fleet used to show a dead provider even though live percentages were sitting
    on disk. Read-only, like every other local client file Fleet touches.

    Since desktop 2.19675 (2026-10) the app samples every fifteen minutes only
    while its own tray menu has been opened recently ("background poll paused:
    tray not opened recently"); otherwise it writes one sample per launch. So
    this is a bonus source, never the reason Claude shows up -- the Claude Code
    sign-in is.
    """
    path = path or _claude_desktop_history()
    try:
        samples = json.loads(Path(path).read_text()).get("samples") or []
    except Exception as e:
        dbg("claude desktop history", type(e).__name__)
        return None
    for sample in reversed(samples):
        usage = sample.get("u") if isinstance(sample, dict) else None
        if not isinstance(usage, dict) or not usage:
            continue
        ts = (sample.get("t") or 0) / 1000.0
        if max_age and time.time() - ts > max_age:
            return None
        mets = []
        if usage.get("fh") is not None:
            mets.append(M("Session", usage["fh"]))
        if usage.get("sd") is not None:
            mets.append(M("Weekly", usage["sd"]))
        return (mets, ts) if mets else None
    return None


def _claude_from_desktop(out):
    """Promote a desktop-app reading into `out`, or leave it untouched."""
    desktop = _claude_desktop_metrics()
    if not desktop:
        return None
    out["metrics"], out["cached_at"] = desktop
    out["source"] = "desktop"
    out["err"] = ""
    out.pop("configured", None)
    return out


def api_claude(force=False):
    out = dict(agent="Claude", plan="", metrics=[], err="", known=False)
    cred = HOME / ".claude" / ".credentials.json"
    try:
        full = _read_claude(cred)
        out["known"] = True
        o = full.get("claudeAiOauth", {})
        out["plan"] = str(o.get("subscriptionType") or o.get("rateLimitTier") or "")
    except FileNotFoundError:
        out["configured"] = False
        out["err"] = "no Claude Code credentials"
        return out
    except Exception as e:
        out["known"] = True
        out["err"] = f"no creds ({e.__class__.__name__})"
        return out
    rt = o.get("refreshToken")
    tok = o.get("accessToken")
    exp = _claude_expiry(o)
    if not tok:
        # The desktop app keeps its own session; prefer its samples over nothing.
        # Its Code tab never fills this file, so using Claude only through the
        # app still leaves Claude Code itself signed out.
        return _claude_from_desktop(out) or dict(
            out, configured=False, err=CLAUDE_SIGNED_OUT
        )
    # Auth and usage have separate throttles: a busy usage endpoint must never
    # stop an expired token from being refreshed.
    if rt and exp and exp < time.time() + CLAUDE_REFRESH_MARGIN:
        status, printed = _renew_claude_via_cli(cred, tok, force=force)
        try:
            o = _read_claude(cred).get("claudeAiOauth") or {}
        except (OSError, ValueError, AttributeError):
            o = {}
        if not o.get("accessToken"):
            return _claude_from_desktop(out) or dict(out, configured=False, err=CLAUDE_SIGNED_OUT)
        tok, exp = o["accessToken"], _claude_expiry(o)
        if printed:
            # Claude Code just read usage itself; a second request adds nothing.
            out["metrics"] = printed
            _save_last("claude", out)
            return out
        if exp and exp < time.time():
            wait = _refresh_cooldown_left("claude_refresh")
            if status == "missing":
                out["err"] = f"{CLAUDE_EXPIRED} · Claude Code is not installed"
            elif status == "busy":
                out["err"] = f"{CLAUDE_EXPIRED} · renewing again in {max(1, int(wait / 60))}m"
            else:
                out["err"] = f"{CLAUDE_EXPIRED} · Claude Code could not renew it"
            return _claude_from_desktop(out) or _cached_or(out, "claude")
    if _token_dead("claude", tok):
        out["err"] = CLAUDE_EXPIRED
        return _claude_from_desktop(out) or _cached_or(out, "claude")
    usage_cool = 0 if force else _refresh_cooldown_left("claude_usage")
    if usage_cool:
        out["err"] = f"usage retry in {max(1, int(usage_cool/60))}m"
        return _cached_or(out, "claude")
    hdrs = {"anthropic-beta": "oauth-2025-04-20", "User-Agent": FLEET_UA,
            "Content-Type": "application/json"}
    st, hd, j = _jget("https://api.anthropic.com/api/oauth/usage", tok, hdrs)
    if st == 401:
        # Revoked or expired early: not re-sent until Claude Code writes a new one.
        _mark_token_dead("claude", tok)
        out["err"] = CLAUDE_EXPIRED
        return _claude_from_desktop(out) or _cached_or(out, "claude")
    if st != 200 or not isinstance(j, dict):
        # The usage endpoint throttles hard. Back off for as long as it asks
        # (at least five minutes), and ask Claude Code itself instead: its own
        # /usage reads the same numbers without a second Fleet request.
        _set_cooldown("claude_usage", max(300, _retry_after(hd)))
        if not _refresh_cooldown_left("claude_cli_usage"):
            _set_cooldown("claude_cli_usage", 600)
            run = _run_claude_usage(cred)
            out["metrics"] = _parse_claude_usage(run[1]) if run and run[0] == 0 else []
        if not out["metrics"]:
            out["err"] = "rate limited" if st == 429 else f"http {st}"
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


def _refresh_cooldown_left(agent):
    """Seconds left in a backoff. The file holds its expiry; reading a past expiry
    as an old-style start time used to add five minutes to every backoff."""
    try:
        return max(0, float((CACHE / f"{agent}_backoff").read_text()) - time.time())
    except (OSError, ValueError):
        return 0


def _set_cooldown(agent, secs=300):
    try:
        (CACHE / f"{agent}_backoff").write_text(str(time.time() + max(1, secs)))
    except Exception:
        pass


# Claude Code renews an access token in the last five minutes before it expires;
# asking any earlier would only read usage with the old token.
CLAUDE_REFRESH_MARGIN = 240
CLAUDE_CLI_TIMEOUT = 90
# Lands in tray.log: renewal outcomes were invisible when Claude went stale.
_RUNTIME = logging.getLogger("fleet.runtime")
_CLI_USAGE_LINE = re.compile(
    r"^Current (session|week \(([^)]+)\)):\s*(\d+(?:\.\d+)?)% used(?:\s*\S\s*resets (.+?))?\s*$")
# English month names whatever the Windows locale: Claude Code prints them so.
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_CLI_RESET = re.compile(r"^(?:([A-Za-z]{3})[a-z]*\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)$", re.I)


def _claude_expiry(oauth):
    exp = oauth.get("expiresAt") or 0
    try:
        exp = float(exp)
    except (TypeError, ValueError):
        return 0
    return exp / 1000 if exp > 1e12 else exp


def _read_claude(cred_path):
    return json.loads(cred_path.read_text(encoding="utf-8-sig"))


def _renew_strikes(agent="claude"):
    try:
        return int((CACHE / f"{agent}_renew_strikes").read_text())
    except (OSError, ValueError):
        return 0


def _renew_failed(why, agent="claude"):
    """Wait 5 min after a failed renewal, doubling to 6 h: the CLI renews on its
    own the next time it runs, so there is nothing to gain by insisting."""
    strikes = _renew_strikes(agent) + 1
    try:
        (CACHE / f"{agent}_renew_strikes").write_text(str(strikes))
    except OSError:
        pass
    wait = min(6 * 3600, 300 * 2 ** (strikes - 1))
    _set_cooldown(f"{agent}_refresh", wait)
    _RUNTIME.warning("%s sign-in renewal failed (%s); next try in %d min (attempt %d)",
                     agent, why, max(1, wait // 60), strikes)


def _renewed(agent):
    for name in (f"{agent}_refresh_backoff", f"{agent}_renew_strikes"):
        try:
            (CACHE / name).unlink()
        except OSError:
            pass


def _run_claude_usage(cred_path):
    """Run Claude Code's own `/usage`, hidden. Returns (exit code, output) or None.

    `/usage` is a local command: no model call, no quota spent. Claude Code
    fetches plan usage through its own client, which first renews an expiring
    login and saves it under Claude Code's own locks. Hooks and MCP servers are
    off and no transcript is written, so the run leaves nothing behind.
    """
    exe = _claude_cli()
    if not exe:
        return None
    probe = CACHE / "claude-probe"
    try:
        probe.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [exe, "-p", "/usage", "--no-session-persistence", "--strict-mcp-config",
             "--settings", '{"disableAllHooks":true}'],
            cwd=str(probe), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        dbg("claude cli", e.__class__.__name__)
        return None
    try:
        out, _ = proc.communicate(timeout=CLAUDE_CLI_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Never stop Claude Code while it holds its refresh lock: the server may
        # already have rotated the login it is about to save.
        lock = cred_path.parent / ".oauth_refresh.lock"
        deadline = time.time() + 60
        while lock.exists() and time.time() < deadline:
            time.sleep(0.5)
        proc.kill()
        out, _ = proc.communicate()
        return -1, (out or b"").decode("utf-8", "replace")
    return proc.returncode, (out or b"").decode("utf-8", "replace")


def _parse_claude_usage(text, now=None):
    """Metrics from Claude Code's `/usage` text, e.g.
    "Current week (all models): 75% used · resets Oct 16, 8am (Asia/Tokyo)"."""
    now = datetime.now().astimezone() if now is None else now
    metrics = []
    for line in str(text or "").splitlines():
        found = _CLI_USAGE_LINE.match(line.strip())
        if not found:
            continue
        kind, scope, pct, when = found.groups()
        label = "Session" if kind == "session" else (
            "Weekly" if (scope or "").casefold() == "all models" else scope)
        metrics.append(M(label, float(pct), _cli_reset(when, now)))
    return metrics


def _cli_reset(when, now):
    """"Oct 16, 8am (Asia/Tokyo)" -> ISO time in the local zone (which is the zone
    Claude Code names); None when the text is not a date it recognises."""
    found = _CLI_RESET.match(re.sub(r"\s*\([^)]*\)\s*$", "", str(when or "")).strip())
    if not found:
        return None
    month, day, hour, minute, half = found.groups()
    hour = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    try:
        stamp = now.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
        if month:
            stamp = stamp.replace(month=_MONTHS.index(month.lower()) + 1, day=int(day))
            if stamp < now - timedelta(days=1):
                stamp = stamp.replace(year=now.year + 1)
        elif stamp < now:
            stamp += timedelta(days=1)
    except ValueError:
        return None
    return stamp.isoformat()


def _renew_claude_via_cli(cred_path, seen_token, force=False):
    """Have Claude Code renew its own login. Fleet never calls the token endpoint.

    Anthropic answers refresh requests that do not come from Claude Code itself
    with 429 before reading the token: it refused Fleet's for six days straight.
    Returns (status, metrics): status is "renewed", "valid", "busy", "missing" or
    "failed"; metrics are what Claude Code's `/usage` printed, when it did.
    """
    if not force and _refresh_cooldown_left("claude_refresh"):
        return "busy", []
    run = _run_claude_usage(cred_path)
    if run is None:
        return "missing", []
    code, text = run
    metrics = _parse_claude_usage(text) if code == 0 else []
    try:
        oauth = _read_claude(cred_path).get("claudeAiOauth") or {}
    except (OSError, ValueError, AttributeError):
        oauth = {}
    token = oauth.get("accessToken")
    if token and token != seen_token:
        _renewed("claude")
        _RUNTIME.info("claude sign-in renewed by Claude Code")
        return "renewed", metrics
    if token and _claude_expiry(oauth) > time.time():
        return "valid", metrics
    _renew_failed(f"exit {code}" if code else "login unchanged")
    return "failed", metrics


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


def _codex_additional_limit_metric(limit):
    """Return a useful optional Codex limit, omitting placeholders with no usage."""
    used_percent = limit.get("used_percent")
    if used_percent is None:
        return None
    raw_label = str(limit.get("name") or limit.get("limit_name") or "extra")
    label = "Spark" if "codex-spark" in raw_label.casefold() else raw_label
    reset_at = limit.get("reset_at") or (
        time.time() + (limit.get("reset_after_seconds", 0) or 0)
    )
    return M(label, used_percent, reset_at)


def _codex_credit_metric(balance):
    """Show Codex credits only when there is a positive balance to report."""
    try:
        amount = float(balance or 0)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return M("Credits", None, None, f"${balance}")


def api_codex():
    out = dict(agent="Codex", plan="", metrics=[], err="", known=False)
    tok = acct = None
    for p in [os.environ.get("CODEX_HOME"), HOME / ".codex", HOME / ".config" / "codex"]:
        if not p:
            continue
        f = Path(p) / "auth.json"
        try:
            a = json.loads(f.read_text())
        except FileNotFoundError:
            continue
        except Exception:
            out["known"] = True
            continue
        out["known"] = True
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
        out["configured"] = False
        out["err"] = "no Codex credentials"
        return out
    if _token_dead("codex", tok):
        out["err"] = "token expired (run codex once)"
        return _cached_or(out, "codex")
    hdr = {"User-Agent": "OpenUsage"}
    if acct:
        hdr["ChatGPT-Account-Id"] = acct
    st, hd, j = _jget("https://chatgpt.com/backend-api/wham/usage", tok, hdr)
    # Opt-in only (--refresh). OpenAI rotates refresh tokens: refreshing here and
    # keeping the result in memory retires the token Codex CLI has saved, which
    # signs the CLI out. By default Fleet waits for Codex to refresh itself.
    if st == 401 and rt and REFRESH:
        r = _refresh_codex(rt)
        if r:
            tok = r
            st, hd, j = _jget("https://chatgpt.com/backend-api/wham/usage", tok, hdr)
    if st == 401:
        _mark_token_dead("codex", tok)
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
    for limit in (j.get("additional_rate_limits") or []):
        metric = _codex_additional_limit_metric(limit)
        if metric:
            out["metrics"].append(metric)
    cr = j.get("credits") or {}
    if cr:
        metric = _codex_credit_metric(cr.get("balance"))
        if metric:
            out["metrics"].append(metric)
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


def _unwrap_number(value):
    """Accept a scalar or {"val": n} billing amount; never throw on junk."""
    if isinstance(value, dict):
        value = value.get("val", value.get("value"))
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quota_metrics(metrics):
    """Percent windows are the only Grok facts the flyout treats as usage."""
    return [m for m in (metrics or []) if m.get("pct") is not None]


def _grok_metrics(cred, bill):
    """Map cli-chat-proxy billing payloads onto Fleet's metric rows."""
    cfg = (cred or {}).get("config", {}) if isinstance(cred, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}
    cp = cfg.get("currentPeriod") or {}
    if not isinstance(cp, dict):
        cp = {}
    metrics = []
    weekly = _unwrap_number(cfg.get("creditUsagePercent"))
    if weekly is None and cp.get("type"):
        # The proxy serialises protobuf JSON, which omits zero-valued fields: a
        # live weekly period with no usage yet arrives without the percentage.
        # Reading that as "no data" filed an unused Grok under inactive as "off".
        weekly = 0.0
    if weekly is not None:
        wk = "WEEKLY" in str(cp.get("type", "")).upper()
        metrics.append(M("Weekly" if wk else "Usage", weekly, cp.get("end")))
    for pu in (cfg.get("productUsage") or []):
        if not isinstance(pu, dict):
            continue
        pct = _unwrap_number(pu.get("usagePercent"))
        if pct is None:
            continue
        if weekly is not None and abs(pct - weekly) <= 0.5:
            continue
        metrics.append(M(str(pu.get("product") or "product"), pct, cp.get("end")))
    b = (bill or {}).get("config", {}) if isinstance(bill, dict) else {}
    if not isinstance(b, dict):
        b = {}
    lim = _unwrap_number(b.get("monthlyLimit"))
    used = _unwrap_number(b.get("used")) or 0
    if lim:
        metrics.append(M(
            "Monthly",
            used / lim * 100,
            b.get("billingPeriodEnd"),
            f"{used:g}/{lim:g} cr",
        ))
    cap = _unwrap_number(cfg.get("onDemandCap")) or _unwrap_number(b.get("onDemandCap")) or 0
    metrics.append(M("Pay-go", None, None, f"{cap:g}" if cap else "off"))
    return metrics


# The Grok CLI's access token lives six hours and is renewed only while the CLI
# runs; its refresh token rotates on every use and dies after about a week unused.
GROK_CLI_TIMEOUT = 90
# The CLI refreshes five minutes ahead while it runs: a running session renews
# first, and Fleet only steps in once nobody has.
GROK_RENEW_MARGIN = 60


def _grok_cli():
    """The CLI installed beside the login it owns (~/.grok/bin), or None."""
    exe = HOME / ".grok" / "bin" / "grok.exe"
    return str(exe) if exe.is_file() else None


def _grok_entry():
    """The Grok CLI's stored login (its auth.json entry with a key), or None."""
    auth = json.loads((HOME / ".grok" / "auth.json").read_text())
    if isinstance(auth, dict):
        for value in auth.values():
            if isinstance(value, dict) and value.get("key"):
                return value
    return None


def _renew_grok_via_cli(seen_expiry):
    """Have the Grok CLI renew its own login. Fleet never calls the token endpoint.

    `grok models` lists models and exits: it authenticates first, so the CLI
    refreshes silently through its identity provider and saves the new login
    under its own lock. No browser, no model call. Without this a login lapsed six
    hours after the CLI last ran, and after a week unused it needed the browser.
    Returns "renewed", "busy", "missing" or "failed".
    """
    if _refresh_cooldown_left("grok_refresh"):
        return "busy"
    exe = _grok_cli()
    if not exe:
        return "missing"
    try:
        proc = subprocess.Popen(
            [exe, "models"], cwd=str(HOME), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        dbg("grok cli", e.__class__.__name__)
        return "missing"
    try:
        code = proc.wait(timeout=GROK_CLI_TIMEOUT)
    except subprocess.TimeoutExpired:
        # A refresh takes well under a second; give a hung one a little longer
        # rather than stop it between the server rotating the login and saving it.
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = -1
    try:
        expiry = _epoch((_grok_entry() or {}).get("expires_at"))
    except (OSError, ValueError):
        expiry = None
    if expiry and expiry > max(time.time() + GROK_RENEW_MARGIN, seen_expiry or 0):
        _renewed("grok")
        _RUNTIME.info("grok sign-in renewed by the Grok CLI")
        return "renewed"
    _renew_failed(f"exit {code}" if code else "login unchanged", agent="grok")
    return "failed"


def api_grok():
    out = dict(agent="Grok", plan="", metrics=[], err="", known=False)
    # Soft-cache successful readings so tray polls do not re-hit cli-chat-proxy every cycle.
    # This reduces auth pressure on the same Grok account used by CLIProxyAPI/Codex.
    # Pay-go "off" is not a usage reading: caching it hid Grok in the inactive list.
    last = _load_last("grok", max_age=180)
    if last:
        mets, plan, _age_text, _ts = last
        if _quota_metrics(mets):
            out["metrics"] = mets
            out["plan"] = plan
            out["err"] = "soft cache"
            out["cached_at"] = _ts
            return out
    try:
        auth = json.loads((HOME / ".grok" / "auth.json").read_text())
    except FileNotFoundError:
        out["configured"] = False
        # An installed Grok CLI with no auth file has been signed out (the CLI
        # deletes it); say so instead of hiding Grok as if it never existed.
        if (HOME / ".grok").is_dir():
            out["known"] = True
            out["err"] = "Grok is signed out (run grok)"
        else:
            out["err"] = "no Grok credentials"
        return out
    except Exception as e:
        out["known"] = True
        out["err"] = f"no creds ({e.__class__.__name__})"
        return out
    out["known"] = True
    entry = None
    if isinstance(auth, dict):
        for v in auth.values():
            if isinstance(v, dict) and v.get("key"):
                entry = v
                break
    if entry:
        expires = _epoch(entry.get("expires_at"))
        tok = (entry.get("key") or "").strip()
        if (expires and expires <= time.time() + GROK_RENEW_MARGIN) or _token_dead("grok", tok):
            if _renew_grok_via_cli(expires) == "renewed":
                try:
                    entry = _grok_entry() or entry
                except (OSError, ValueError):
                    pass
    if not entry:
        out["configured"] = False
        out["err"] = "no Grok credentials"
        return out
    tok = (entry.get("key") or "").strip()
    # Grok owns both the schema and refresh-token rotation for this file. A
    # dashboard refresh must never race the CLI or serialize provider state in a
    # shape the CLI cannot read. An expiring token is renewed above by running the
    # CLI itself, which refreshes under its own lock; Fleet only reads the result.
    # The file states its own expiry, so a dead token needs no doomed request --
    # and saying how long it has been dead is the difference between a user
    # noticing a stale login and staring at a provider that never updates.
    expires = _epoch(entry.get("expires_at"))
    if expires and expires <= time.time():
        out["err"] = f"login expired {_age(expires)} ago (run grok)"
        return _cached_or(out, "grok")
    if _token_dead("grok", tok):
        out["err"] = "login expired (run grok)"
        return _cached_or(out, "grok")
    h = {"X-XAI-Token-Auth": "xai-grok-cli", "User-Agent": "OpenUsage"}
    st, hd, cred = _jget("https://cli-chat-proxy.grok.com/v1/billing?format=credits", tok, h)
    if st != 200:
        if st == 401:
            _mark_token_dead("grok", tok)
        out["err"] = ("login expired (run grok)" if st == 401 else f"http {st}")
        return _cached_or(out, "grok")
    _, _, settings = _jget("https://cli-chat-proxy.grok.com/v1/settings", tok, h)
    out["plan"] = (settings or {}).get("subscription_tier_display", "") if isinstance(settings, dict) else ""
    _, _, bill = _jget("https://cli-chat-proxy.grok.com/v1/billing", tok, h)
    out["metrics"] = _grok_metrics(cred, bill)
    if _quota_metrics(out["metrics"]):
        _save_last("grok", out)
    return out


_AGY_BUCKETS = {"gemini-5h": "Session", "gemini-weekly": "Weekly", "3p-5h": "Claude", "3p-weekly": "Claude Wk"}


def _agy_group_title(group):
    """'Gemini Models' -> 'Gemini', 'Claude and GPT models' -> 'Claude & GPT'."""
    title = re.sub(r"\s+models?$", "", str(group.get("displayName") or "").strip(), flags=re.I)
    return title.replace(" and ", " & ") or "Quota"


def _agy_parse_summary(j):
    """One metric per quota bucket, keeping the group and window Antigravity names.

    Groups come and go (a "Gemini 4 Argon" weekly bucket appeared in October), so
    they are read from the response rather than a fixed list. A bucket's
    remaining share is a protobuf oneof (fraction or amount): one without a
    fraction is not reported in a form Fleet can show, and is skipped, as is a
    disabled one.
    """
    groups = ((j.get("response") or j).get("groups")) if isinstance(j, dict) else None
    mets = []
    for g in (groups or []):
        title = _agy_group_title(g)
        for b in g.get("buckets", []):
            rf = b.get("remainingFraction")
            if rf is None or b.get("disabled"):
                continue
            bucket = b.get("bucketId", "pool")
            metric = M(_AGY_BUCKETS.get(bucket, bucket), (1 - float(rf)) * 100, b.get("resetTime"))
            metric["group"] = title
            metric["window"] = str(b.get("window") or "")
            mets.append(metric)
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
_AGY_OAUTH_CLIENTS = None


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


# Used when no Antigravity install can be read; the oldest version known to get
# the Gemini quota group back.
AGY_FALLBACK_VERSION = "2.11.0"


def _exe_version(path):
    """A Windows executable's file version ("2.25.0"), or None."""
    try:
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        info, length = ctypes.c_void_p(), ctypes.c_uint()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(info), ctypes.byref(length)) or not info.value:
            return None
        # VS_FIXEDFILEINFO: signature, struct version, then the file version MS/LS words.
        fixed = ctypes.cast(info, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        return f"{fixed[2] >> 16}.{fixed[2] & 0xFFFF}.{fixed[3] >> 16}"
    except (AttributeError, OSError, ValueError):
        return None


@functools.lru_cache(maxsize=1)
def _agy_user_agent():
    """Antigravity's own user agent with the installed version, then Fleet's name.

    Google answers a bare "antigravity" as an old client: its quota summary then
    leaves out the Gemini group, which is how Gemini went missing while
    Antigravity was closed. The installed version gets the full summary back.
    """
    programs = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
    for exe in (programs / "antigravity" / "Antigravity.exe", programs / "Antigravity IDE" / "Antigravity IDE.exe"):
        found = _exe_version(exe) if exe.is_file() else None
        if found and found != "0.0.0":
            return f"antigravity/{found} windows/amd64 {FLEET_UA}"
    return f"antigravity/{AGY_FALLBACK_VERSION} windows/amd64 {FLEET_UA}"


def _agy_cloud_call(token, method, payload):
    """Call a Code Assist method without exposing response data in diagnostics."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": _agy_user_agent(),
    }
    last = (0, {})
    for host in ("https://daily-cloudcode-pa.googleapis.com", "https://cloudcode-pa.googleapis.com"):
        st, _, body = _http("POST", f"{host}/v1internal:{method}", headers, payload)
        try:
            result = json.loads(body) if body else {}
        except Exception:
            result = {}
        last = (st, result)
        if st == 200:
            return last
        if st not in (401, 403, 404):
            break
    return last


def _agy_oauth_client_candidates():
    """Discover locally installed Antigravity OAuth metadata; never persist it."""
    global _AGY_OAUTH_CLIENTS
    if _AGY_OAUTH_CLIENTS is not None:
        return _AGY_OAUTH_CLIENTS

    binaries = []
    configured = os.environ.get("FLEET_ANTIGRAVITY_BIN")
    if configured:
        binaries.append(Path(configured))
    for command in ("agy", "antigravity"):
        found = shutil.which(command)
        if found:
            binaries.append(Path(found))
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "antigravity",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Antigravity IDE",
    ]
    for root in roots:
        if root.exists():
            for pattern in ("language_server*.exe", "agy.exe", "antigravity*.exe"):
                binaries.extend(root.rglob(pattern))

    ids, secrets, seen = [], [], set()
    for path in binaries:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        ids.extend(match.group(0).decode() for match in re.finditer(
            rb"\d+-[a-z0-9]+\.apps\.googleusercontent\.com", blob
        ))
        secrets.extend(match.group(0).decode() for match in re.finditer(
            rb"GOCSPX-[A-Za-z0-9_-]{28}", blob
        ))

    pairs = []
    for client_id in reversed(list(dict.fromkeys(ids))):
        for client_secret in reversed(list(dict.fromkeys(secrets))):
            pair = (client_id, client_secret)
            if pair not in pairs:
                pairs.append(pair)
    _AGY_OAUTH_CLIENTS = pairs
    return pairs


def _agy_refresh_access_token(refresh_token):
    """Refresh via Google's installed-app endpoint using local app metadata."""
    if not refresh_token:
        return None
    for client_id, client_secret in _agy_oauth_client_candidates():
        status, _, body = _http(
            "POST",
            "https://oauth2.googleapis.com/token",
            {"Accept": "application/json"},
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            form=True,
            timeout=10,
        )
        try:
            result = json.loads(body) if body else {}
        except Exception:
            result = {}
        access = result.get("access_token") if status == 200 else None
        if isinstance(access, str) and access:
            try:
                expires = max(60, int(result.get("expires_in", 3600)))
            except (TypeError, ValueError):
                expires = 3600
            cache_put("antigravity", access, time.time() + expires)
            return access
        if status not in (400, 401):
            break
    return None


def api_antigravity():
    """Live quota via local language server or refreshed local OAuth state."""
    out = dict(agent="Antigrav", plan="gemini", metrics=[], err="", known=False)

    ep = _agy_ls_endpoint()
    if ep:
        out["known"] = True
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

    tok = _agy_token()
    if tok:
        out["known"] = True
        for attempt in range(2):
            load_status, loaded = _agy_cloud_call(
                tok, "loadCodeAssist", {"metadata": {"ideType": "ANTIGRAVITY"}}
            )
            project = loaded.get("cloudaicompanionProject") if isinstance(loaded, dict) else None
            quota_status, summary = (
                _agy_cloud_call(tok, "retrieveUserQuotaSummary", {"project": project} if project else {})
                if load_status == 200 else (load_status, {})
            )
            mets = _agy_parse_summary(summary) if quota_status == 200 else []
            if mets:
                out["metrics"] = mets
                tier = (loaded.get("paidTier") or {}).get("name") if isinstance(loaded, dict) else None
                if tier:
                    out["plan"] = str(tier)
                _save_last("antigravity", out)
                return out
            if attempt == 0 and (load_status in (401, 403) or quota_status in (401, 403)):
                _, refresh = _agy_stored_tokens()
                tok = _agy_refresh_access_token(refresh)
                if tok:
                    continue
            break

    last = _load_last("antigravity")  # app closed -> show last live quota with age
    if last:
        out["known"] = True
        mets, plan, age, cached_at = last
        out["metrics"] = mets
        out["plan"] = plan
        out["err"] = f"cached {age} ago (AGY closed)"
        out["cached_at"] = cached_at
        return out
    out["known"] = (APPDATA / "Antigravity").exists() or any(
        path.exists()
        for path in (
            HOME / ".gemini" / "antigravity-cli",
            HOME / ".gemini" / "antigravity",
        )
    )
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
    for path in (
        HOME / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
        HOME / ".gemini" / "antigravity" / "antigravity-oauth-token",
    ):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        token = raw.get("token") if isinstance(raw, dict) and isinstance(raw.get("token"), dict) else raw
        if isinstance(token, dict):
            access = token.get("access_token") or token.get("accessToken")
            refresh = token.get("refresh_token") or token.get("refreshToken")
            if access or refresh:
                return access, refresh
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
    if spec.get("collector") == "anthropic_cost" or spec.get("connection_only"):
        if not out.get("credential_id") or out.get("err") in ("key rejected", "billing reports unavailable for this account"):
            return out
        # Old schema reports used cents as dollars. A replacement key can also
        # belong to another organization: neither cache is a valid fallback.
        try:
            last = json.loads((CACHE / f"{agent}_last.json").read_text())
            age = time.time() - last["ts"]
            same_day = time.gmtime(last["ts"])[:3] == time.gmtime(time.time())[:3]
            if last.get("credential_id") == out["credential_id"] and 0 <= age <= 86400 and same_day and last.get("account"):
                return dict(out, plan=last.get("plan", ""), metrics=last["metrics"], account=last["account"],
                            cached_at=last["ts"], err=f"cached {_age(last['ts'])} ago · {out.get('err', '')}")
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return out
    cached = _cached_or(out, agent)
    cached["compact"] = spec.get("collector") != "anthropic_cost"
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


# Seconds between live calls per provider. The tray polls every 30 s (10 s with
# the panel open); within these windows a provider's last good reading is reused
# with its real timestamp, so frequent panel refreshes never hammer an endpoint.
# Refresh now (force) bypasses the pacing but not a provider's own backoff.
# Anthropic throttles its usage endpoint at about one call a minute; three is safe.
PACE = {"api_claude": 180, "api_codex": 30, "api_grok": 60, "api_antigravity": 15, "api_key": 60,
        **{"api_key:" + pid: 300 for pid in apikeys.api_credentials.TARGETS}}
_LIVE = {}
_LIVE_LOCK = threading.Lock()


def _paced(key, fetch, force=False):
    now = time.monotonic()
    with _LIVE_LOCK:
        last = _LIVE.get(key)
    pace = PACE.get(key, PACE["api_key"])
    if last:
        previous = json.loads(last[1])
        if (previous.get("retry_at") or 0) > time.time() or (not force and now - last[0] < pace):
            return previous
    result = fetch()
    if (key.startswith("api_key:") and key[len("api_key:"):] in apikeys.api_credentials.TARGETS) or (
        result.get("metrics") and not result.get("err") and not result.get("cached_at")
    ):
        with _LIVE_LOCK:
            _LIVE[key] = (now, json.dumps(result))
    return result


def collect_api(force_claude=False, include_api_keys=True):
    out = []
    specs = _api_key_specs()[0] if include_api_keys else []
    pool = pending = None
    if specs:
        # Started first: they are pure HTTP and finish while the local providers
        # are still reading credential files and shelling out to the CLIs.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(specs)))
        pending = [
            pool.submit(_paced, "api_key:" + spec["id"], lambda spec=spec: _collect_one_spec(spec), force_claude)
            for spec in specs
        ]
    def local(p):
        try:
            return _paced(
                p.__name__,
                (lambda: p(force=force_claude)) if p is api_claude else p,
                force=force_claude,
            )
        except Exception as e:
            dbg(p.__name__, repr(e))
            return dict(agent=p.__name__.replace("api_", "").title()[:8], plan="",
                        metrics=[], err=f"crash: {e.__class__.__name__}")

    try:
        # Each local provider is its own credential file and endpoint, so they run
        # side by side; a poll costs the slowest provider, not the sum of them.
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(API_PROVIDERS)) as local_pool:
            out.extend(local_pool.map(local, API_PROVIDERS))
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
    print("\nSet the variable, then refresh Fleet. Probes need \"probe\": true in the config.")


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
