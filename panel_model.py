"""The flyout's view model: everything the native panel draws, decided here.

The tray writes this as panel.json after every poll. The native panel only lays
it out and ticks the clocks (reset countdowns, "updated 5s ago"), so every rule
about what is shown -- which providers are live, how a quota is labelled, when a
zero-dollar fact is noise, which limits a closed section shows -- lives in tested
Python next to the collectors.

Schema 3 adds what the tokens in local agent logs would cost at API list prices
(ledger.py's snapshot, passed in as `usage`) and how fast each limit has been
filling lately (`rates`, from Fleet's own readings). Both are optional: without
them the model is schema 2's plus the closed-section meters.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import fleet

SCHEMA = 4


def _assets_dir():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets" / "providers"


ICONS = {
    "claude": "claude.png",
    "claude api": "claude.png",
    "codex": "codex.png",
    "grok": "grok.ico",
    "antigrav": "antigravity.png",
    "antigravity": "antigravity.png",
}
NAMES = {"Antigrav": "Antigravity"}
PLANS = {
    "pro": "Pro",
    "prolite": "Pro",
    "plus": "Plus",
    "max": "Max",
    "team": "Team",
    "enterprise": "Enterprise",
    "free": "Free",
    "x premium": "X Premium",
    "supergrok": "SuperGrok",
}
# Plain words, as the provider would say them: (label, window length in seconds).
WINDOWS = {
    "session": ("5-hour limit", 5 * 3600),
    "5 hours": ("5-hour limit", 5 * 3600),
    "weekly": ("Weekly limit", 7 * 86400),
    "week": ("Weekly limit", 7 * 86400),
    "claude wk": ("Weekly limit", 7 * 86400),
    "daily": ("Daily limit", 86400),
    "monthly": ("Monthly limit", 30 * 86400),
    "balance": ("Balance", None),
    "credits": ("Credits", None),
    "extra $": ("Extra usage", None),
    "messages": ("Messages", None),
    "pay-go": ("Pay as you go", None),
}
# Antigravity names each bucket's window; older readings carry only these labels.
ANTIGRAVITY_WINDOWS = {"5h": ("5-hour limit", 5 * 3600), "weekly": ("Weekly limit", 7 * 86400),
                       "daily": ("Daily limit", 86400)}
# Readings from before Fleet kept Antigravity's own groups: two per model family.
ANTIGRAVITY_GROUPS = (
    ("Gemini", (("Weekly", "Weekly limit"), ("Session", "5-hour limit"))),
    ("Claude", (("Claude Wk", "Weekly limit"), ("Claude", "5-hour limit"))),
)
INERT = {"off", "disabled", "inactive"}


def _window(raw, scoped=False):
    """(label, window seconds) for a metric label; unknown quotas keep their name."""
    text = str(raw or "").strip()
    if text.casefold() in WINDOWS:
        return WINDOWS[text.casefold()]
    if scoped:
        # Claude's per-model weekly limits arrive named after the model ("Fable").
        return f"{text} weekly limit", 7 * 86400
    return (text[:1].upper() + text[1:]) if text else "Usage", None


def _plan(raw):
    text = str(raw or "").strip()
    return PLANS.get(text.casefold(), text)


def _money(extra):
    text = str(extra or "").strip()
    return bool(text) and (text[0] in "$¥€£" or text.upper().endswith((" USD", " EUR", " GBP", " CNY")))


def _zero_money(metric):
    if metric.get("keep_zero"):
        return False
    if metric.get("pct") is not None or not _money(metric.get("extra")):
        return False
    digits = re.sub(r"[^0-9.+-]", "", str(metric.get("extra")).replace(",", ""))
    try:
        return float(digits) == 0
    except ValueError:
        return False


def _inert(metric):
    return metric.get("pct") is None and str(metric.get("extra") or "").strip().casefold() in INERT


def severity(pct):
    if pct is None:
        return "none"
    return "calm" if pct < 60 else "warn" if pct < 85 else "high"


def _row(metric, label=None, window=None, scoped=False):
    pct = metric.get("pct")
    extra = str(metric.get("extra") or "").strip()
    if pct is not None:
        pct = max(0.0, min(100.0, float(pct)))
        value = extra if _money(extra) else f"{pct:.0f}%"
    else:
        value = extra or "—"
    named, seconds = _window(metric.get("label"), scoped and pct is not None)
    return {
        "label": label or named,
        "pct": pct,
        "value": value,
        "reset_at": fleet._epoch(metric.get("resets")),
        "window": window or seconds,
        "severity": severity(pct),
    }


def _icon(raw_name):
    path = _assets_dir() / ICONS.get(str(raw_name).casefold(), f"{str(raw_name).casefold()}.png")
    return str(path) if path.exists() else None


def _mark(name):
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|\d+", str(name))
    if len(words) > 1:
        return "".join(word[0] for word in words[:2]).upper()
    letters = [char for char in str(name) if char.isalnum()]
    return "".join(letters[:2]).upper() or "?"


def _groups(name, metrics):
    if name == "Antigravity" and any(metric.get("group") for metric in metrics):
        groups, by_title = [], {}
        for metric in metrics:
            title = str(metric.get("group") or "Other")
            window = ANTIGRAVITY_WINDOWS.get(str(metric.get("window") or "").casefold()) or _window(metric.get("label"))
            if title not in by_title:
                by_title[title] = {"title": title, "rows": []}
                groups.append(by_title[title])
            by_title[title]["rows"].append(_row(metric, window[0], window[1]))
        return groups
    if name == "Antigravity":
        by_label = {str(metric.get("label")): metric for metric in metrics}
        groups = []
        for title, windows in ANTIGRAVITY_GROUPS:
            rows = [_row(by_label[key], label, _window(key)[1]) for key, label in windows if key in by_label]
            if rows:
                groups.append({"title": title, "rows": rows})
        if groups:
            return groups
    scoped = name == "Claude"
    return [{"title": None, "rows": [_row(metric, scoped=scoped) for metric in metrics]}]


SOFT_CACHE = "soft cache"
# A forecast this early in a window is noise; so is one for a limit barely used.
PACE_MIN_ELAPSED = 0.05
PACE_MIN_PCT = 10
RUNOUT_MARGIN = 600
# A forecast only warns when the run-out comes a tenth of the window early: a
# weekly limit "running out" 12 hours before it resets is noise, a 5-hour one
# running dry 40 minutes early is not.
RUNOUT_SHARE = 0.1
# A cached reading is only dimmed once it is this old; a few minutes is still true.
STALE_AFTER = 900


def _short(label):
    """ "5-hour limit" -> "5-hour", "Fable weekly limit" -> "Fable weekly". """
    text = str(label or "").strip()
    return text[:-len(" limit")] if text.endswith(" limit") else text


def _pace(row, now):
    """How a limit is tracking against an even burn rate, as JSON-ready fields.

    elapsed: share of the window gone (0-1). reserve: percentage points under
    an even pace (negative when ahead of it). runout_at: when it hits 100% at
    the current rate, set only if that comes before the reset. state: full,
    high (90%+), ahead (runs out before the reset) or calm.
    """
    pct, reset, window = row.get("pct"), row.get("reset_at"), row.get("window")
    out = {"elapsed": None, "reserve": None, "runout_at": None,
           "state": "calm" if pct is None or pct < 90 else "full" if pct >= 100 else "high"}
    if pct is None or not reset or not window:
        return out
    elapsed = max(0.0, min(1.0, 1 - (reset - now) / window))
    out["elapsed"] = round(elapsed, 4)
    if elapsed < PACE_MIN_ELAPSED:
        return out
    out["reserve"] = round(elapsed * 100 - pct, 1)
    if PACE_MIN_PCT <= pct < 100:
        rate = pct / (elapsed * window)
        full_at = now + (100 - pct) / rate
        if full_at < reset - max(RUNOUT_MARGIN, RUNOUT_SHARE * window):
            out["runout_at"] = round(full_at)
            if out["state"] == "calm":
                out["state"] = "ahead"
    return out


def _rings(name, groups):
    """(outer, inner) rows for the ring gauge: longest window outside, shortest
    inside. Antigravity shows its busiest model family; the rest is in rows."""
    usable = [g for g in groups if any(r.get("pct") is not None for r in g["rows"])]
    if not usable:
        return []
    group = max(usable, key=lambda g: max(r["pct"] for r in g["rows"] if r.get("pct") is not None))
    rows = [r for r in group["rows"] if r.get("pct") is not None and not str(r.get("value", "")).startswith("$")]
    if not rows:
        return []
    timed = [r for r in rows if r.get("window")]
    # A model-scoped weekly (Claude "Fable weekly") is a detail, not a ring, when
    # the all-model weekly exists.
    plain = [r for r in timed if r["label"] in ("Weekly limit", "5-hour limit", "Daily limit", "Monthly limit")]
    pool = plain or timed or rows
    outer = max(pool, key=lambda r: (r.get("window") or 0, r["pct"]))
    inner = min(pool, key=lambda r: (r.get("window") or 0, -r["pct"]))
    return [outer] if inner is outer else [outer, inner]


# Providers whose tokens can be priced from local logs: panel id -> ledger provider.
LEDGER = {"claude": "claude", "codex": "codex"}
PLAIN = ("5-hour limit", "Weekly limit", "Daily limit", "Monthly limit")


def _meter_label(label):
    """ "5-hour limit" -> "5-hour", "Fable weekly limit" -> "Fable wk". """
    text = _short(label)
    return text[: -len(" weekly")] + " wk" if text.endswith(" weekly") else text


def _meters(groups):
    """The limits a closed section shows, shortest window first.

    A model family (Antigravity's Gemini, Claude) shows its busiest window under
    the family's name. Otherwise the all-model limits, at most two; a scoped one
    (Claude's "Fable weekly") joins only while it is the fullest of all.
    """
    titled = [g for g in groups if g.get("title")]
    if titled:
        out = []
        for group in titled:
            timed = [r for r in group["rows"] if r.get("pct") is not None]
            if timed:
                row = max(timed, key=lambda r: (r["pct"], -(r.get("window") or 0)))
                out.append(dict(row, short=group["title"]))
        return out
    rows = [r for g in groups for r in g["rows"]
            if r.get("pct") is not None and not str(r.get("value", "")).startswith("$")]
    plain = [r for r in rows if r["label"] in PLAIN]
    chosen = sorted(plain or rows, key=lambda r: r.get("window") or float("inf"))[:2]
    rest = [r for r in rows if r not in chosen]
    if rest and chosen:
        fullest = max(rest, key=lambda r: r["pct"])
        if fullest["pct"] > max(r["pct"] for r in chosen):
            chosen.append(fullest)
    return [dict(r, short=_meter_label(r["label"])) for r in chosen]


def _span_row(metrics):
    """The shortest all-model window among a provider's raw metrics: (label, start, seconds)."""
    best = None
    for metric in metrics or []:
        if metric.get("pct") is None:
            continue
        label, seconds = _window(metric.get("label"))
        reset = fleet._epoch(metric.get("resets"))
        if seconds and reset and label in PLAIN and (best is None or seconds < best[2]):
            best = (_short(label), reset - seconds, seconds)
    return best


def window_spans(data):
    """{ledger provider: {"5-hour": window start}}: the spans the ledger totals for
    each provider's current shortest window, so its spend can sit beside its meter."""
    spans = {}
    for agent in data or []:
        pid = str(agent.get("api_id") or agent.get("agent") or "").casefold()
        source = LEDGER.get(pid)
        best = _span_row(agent.get("metrics")) if source else None
        if best:
            spans.setdefault(source, {})[best[0]] = best[1]
    return spans


# Codex desktop chats run in folders named after their ChatGPT project's id.
_CHATGPT_PROJECT = re.compile(r"^g-p-[0-9a-f]{16,}$")


def _projects(projects):
    """Readable project names, merged where several share one (a ChatGPT project id)."""
    merged = {}
    for project in projects or []:
        name = str(project.get("name") or "").strip() or "Other"
        if _CHATGPT_PROJECT.match(name):
            name = "ChatGPT project"
        entry = merged.setdefault(name, {"name": name, "cost": 0.0})
        entry["cost"] += float(project.get("cost") or 0.0)
    return sorted(merged.values(), key=lambda p: -p["cost"])


def _spend(pid, metrics, usage):
    """What this provider's logged tokens came to at API prices, or None."""
    source = LEDGER.get(pid)
    snap = (usage or {}).get(source) if source else None
    if not snap or not usage.get("complete"):
        return None
    best = _span_row(metrics)
    label = best[0] if best else ""
    return {
        "today": snap.get("today"),
        "window": (snap.get("spans") or {}).get(label) if label else None,
        "window_label": label,
        "week": snap.get("week"),
        "month": snap.get("month"),
        "rate": snap.get("rate"),
        "hourly": list(snap.get("hourly") or []),
        "daily": list(snap.get("daily") or [])[-14:],
        "models": list(snap.get("models") or [])[:20],
        "projects": _projects(snap.get("projects"))[:20],
        "estimated": bool(snap.get("estimated")),
        "unpriced": snap.get("unpriced") or 0,
    }


def _summary(usage, settings):
    """The totals band: today, 7 and 30 days at API prices, across providers."""
    if settings.get("cost") is False:
        return {"state": "off"}
    if not usage:
        return None
    if not usage.get("complete"):
        return {"state": "indexing"}
    providers = [usage[source] for source in sorted(set(LEDGER.values())) if usage.get(source)]
    if not providers:
        return None
    total = usage.get("total") or {}

    def cost(span):
        return round(float((total.get(span) or {}).get("cost") or 0.0), 2)

    return {
        "state": "ready",
        "today": cost("today"),
        "week": cost("week"),
        "month": cost("month"),
        "tokens_today": (total.get("today") or {}).get("tokens") or 0,
        "estimated": any(p.get("estimated") for p in providers),
    }


def _caption(rows, rings, groups):
    """The one line under a tile: the most urgent thing, else when it resets."""
    timed = [r for r in rows if r.get("pct") is not None]
    full = [r for r in timed if r["state"] == "full"]
    if full:
        r = min(full, key=lambda r: r.get("reset_at") or 0)
        return {"kind": "full", "label": _short(r["label"]), "at": r.get("reset_at"), "tone": "high"}
    soon = [r for r in timed if r.get("runout_at")]
    if soon:
        r = min(soon, key=lambda r: r["runout_at"])
        return {"kind": "runout", "label": _short(r["label"]), "at": r["runout_at"], "tone": "warn"}
    high = [r for r in timed if r["state"] == "high"]
    if high:
        r = max(high, key=lambda r: r["pct"])
        return {"kind": "reset", "label": _short(r["label"]), "at": r.get("reset_at"), "tone": "high"}
    if timed and all(r["pct"] == 0 for r in timed):
        return {"kind": "text", "text": "All limits unused", "tone": "calm"}
    if rings:
        r = max(rings, key=lambda r: r["pct"])
        quiet = [g["title"] for g in groups if g.get("title") and g["rows"]
                 and all((x.get("pct") or 0) == 0 for x in g["rows"]) and r not in g["rows"]]
        if quiet:
            return {"kind": "text", "text": " · ".join(quiet) + " unused", "tone": "calm"}
        return {"kind": "reset", "label": _short(r["label"]), "at": r.get("reset_at"), "tone": "calm"}
    return None


def _balance(rows):
    money = [r for r in rows if r.get("pct") is None and r.get("value") not in (None, "", "—")]
    if not money:
        return None
    r = money[0]
    label = {"Balance": "Credit balance"}.get(r["label"], r["label"])
    return {"value": r["value"], "label": label}


def _stale_reason(err):
    """Why a reading is old, in a word or two; the full error stays in the tooltip."""
    text = str(err or "").casefold()
    if any(term in text for term in ("expired", "signed out", "run grok", "run codex")):
        return "Sign-in expired"
    if "auth retry" in text or "auth temporarily" in text:
        return "Reconnecting"
    if "429" in text or "retry in" in text or "rate limit" in text:
        return "Rate limited"
    if "http 0" in text or "offline" in text or "timed out" in text:
        return "Offline"
    return ""


def _freshness(agent, ts, now=None):
    now = ts if now is None else now
    fresh = _freshness_kind(agent, ts)
    if fresh is not None:
        fresh["stale"] = now - float(fresh["at"] or now) > STALE_AFTER
    return fresh


def _freshness_kind(agent, ts):
    err = str(agent.get("err") or "")
    if agent.get("source") and not err:
        return {"kind": agent["source"], "at": agent.get("cached_at") or ts}
    # Grok's three-minute soft cache spares its login; the reading is healthy.
    if err == SOFT_CACHE:
        return None
    if err or agent.get("cached_at"):
        fresh = {"kind": "cached", "at": agent.get("cached_at") or ts}
        reason = _stale_reason(err)
        if reason:
            fresh["reason"] = reason
        return fresh
    return None


def _needs_sign_in(agent):
    reason = str(agent.get("err") or "").casefold()
    return agent.get("agent") == "Claude" and not agent.get("cached_at") and any(
        term in reason for term in ("signed out", "sign-in expired", "access token")
    )


def _inactive(agent):
    raw = agent.get("agent") or "Unknown"
    name = NAMES.get(raw, raw)
    reason = str(agent.get("err") or agent.get("_inert") or "not active").strip()
    if agent.get("api_id") in ("anthropic", "anthropic-admin"):
        label, tooltip, action = reason[:1].upper() + reason[1:], reason, "api-settings"
    elif _needs_sign_in(agent):
        label, tooltip, action = (
            "Signed out",
            "Claude Code is signed out, so there is no Claude usage to read. "
            "Sign in opens a console running claude auth login.",
            "claude-sign-in",
        )
    elif raw == "Grok" and any(t in reason.casefold() for t in ("run grok", "signed out", "expired")):
        label, tooltip, action = (
            "Sign-in expired" if "expired" in reason.casefold() else "Signed out",
            "Grok's own sign-in has lapsed; Fleet only reads it. "
            "Renew opens a console running grok, which signs in again.",
            "grok-renew",
        )
    else:
        lowered = reason.casefold()
        first = reason.split(" · ", 1)[0]
        label = (
            "Signed out" if "signed out" in lowered
            else "Not connected" if "credentials" in lowered
            else "Sign-in expired" if "expired" in lowered
            else "Not running" if "not running" in lowered or "closed" in lowered
            else "Off" if lowered in INERT
            else _stale_reason(reason)
            or first[:1].upper() + first[1:]
        )
        tooltip, action = reason, None
    return {"name": name, "icon": _icon(raw), "mark": _mark(name), "label": label,
            "tooltip": tooltip, "action": action}


def build(data, ts, now=None, settings=None, usage=None, rates=None, accounts=None):
    """Turn collector output into the panel model (plain JSON-serialisable dict).

    usage: ledger.snapshot() for the API-equivalent figures; rates: {panel id:
    {limit label: percentage points per hour}} from Fleet's own readings.
    """
    now = time.time() if now is None else now
    settings = settings or {}
    rates = rates or {}
    cost_on = settings.get("cost") is not False
    hidden = {str(item).casefold() for item in settings.get("hidden") or []}
    providers, inactive, known = [], [], []
    for agent in data or []:
        raw = agent.get("agent") or "Unknown"
        name = NAMES.get(raw, raw)
        pid = str(agent.get("api_id") or raw).casefold()
        if agent.get("known", True) and (agent.get("metrics") or agent.get("err")):
            known.append({"id": pid, "name": name, "hidden": pid in hidden})
        if pid in hidden:
            continue
        metrics = agent.get("metrics") or []
        inert = [metric for metric in metrics if _inert(metric)]
        shown = [metric for metric in metrics if not _inert(metric) and not _zero_money(metric)]
        if shown and agent.get("configured") is not False:
            all_shown = shown
            if agent.get("compact") and name != "Antigravity":
                scored = [metric for metric in shown if metric.get("pct") is not None]
                shown = [max(scored, key=lambda m: m["pct"])] if scored else shown[:1]
            fresh = _freshness(agent, ts, now)
            if fresh and fresh.get("reason") == "Sign-in expired":
                # Old numbers behind a dead login help nobody: offer the fix instead.
                inactive.append(_inactive(dict(agent, cached_at=None)))
                continue
            groups = _groups(name, shown)
            for group in groups:
                for row in group["rows"]:
                    row.update(_pace(row, now))
                    # Unique within the provider: Antigravity's families share labels.
                    row["key"] = (group["title"] + " " if group.get("title") else "") + row["label"]
                    # A rate from readings that stopped is history, not a pace.
                    rate = None if fresh else (rates.get(pid) or {}).get(row["key"])
                    row["rate"] = round(rate, 1) if isinstance(rate, (int, float)) and rate > 0 else None
            rows = [row for group in groups for row in group["rows"]]
            rings = _rings(name, groups)
            balance = None if rings else _balance(rows)
            all_groups = groups if all_shown is shown else _groups(name, all_shown)
            if all_groups is not groups:
                for group in all_groups:
                    for row in group["rows"]:
                        row.update(_pace(row, now))
            providers.append({
                "id": pid,
                "name": name,
                "icon": _icon(raw),
                "mark": _mark(name),
                "plan": _plan(agent.get("plan")),
                "freshness": fresh,
                "detail": "" if agent.get("err") == SOFT_CACHE else str(agent.get("err") or ""),
                "groups": groups,
                "all_groups": all_groups,
                "kind": "rings" if rings else "balance" if balance else "rows",
                "rings": [{"label": _short(r["label"]), "pct": r["pct"], "reset_at": r["reset_at"],
                           "window": r["window"], "elapsed": r["elapsed"], "state": r["state"]} for r in rings],
                "headline": max((r["pct"] for r in rings), default=None),
                "caption": _caption(rows, rings, groups),
                "balance": balance,
                "meters": _meters(groups),
                "spend": _spend(pid, shown, usage) if cost_on else None,
                "account": {"mode": agent["account"]["mode"]}
                if pid in ("anthropic", "anthropic-admin") and isinstance(agent.get("account"), dict)
                and agent["account"].get("mode") in ("spend", "connection") else None,
            })
        elif (agent.get("err") or inert) and agent.get("known", True):
            entry = dict(agent)
            if not entry.get("err") and inert:
                entry["_inert"] = str(inert[0].get("extra") or "off")
            inactive.append(_inactive(entry))
    fresh = sum(1 for provider in providers if provider["freshness"] is None)
    return {
        "schema": SCHEMA,
        "ts": ts,
        "built": now,
        "live": fresh,
        "cached": len(providers) - fresh,
        "providers": providers,
        "inactive": inactive,
        "known": known,
        # Local history remains useful when a client is signed out. Never put
        # credentials or raw account payloads into this read-only dashboard data.
        "history": [{"id": pid, "name": "Claude Code" if pid == "claude" else "Codex",
                     "icon": _icon(pid), "mark": _mark(pid), "spend": spend}
                    for pid in LEDGER if pid not in hidden
                    for spend in [_spend(pid, [], usage) if cost_on else None] if spend],
        "coverage": {key: (usage.get("coverage") or {}).get(key)
                     for key in ("files", "pending_files", "skipped_zst", "updated", "since")}
                    if cost_on and usage else None,
        "api_accounts": [{"id": item["id"], "source": item.get("source", ""), "enabled": item.get("enabled") is not False}
                         for item in (accounts or []) if item.get("id") in ("anthropic", "anthropic-admin")
                         and item.get("source", "") in ("", "windows", "environment")],
        "summary": _summary(usage, settings),
        "settings": {key: settings[key] for key in ("units", "tray_icon", "alerts", "cost") if key in settings},
    }
