"""Windows notifications for limits: once per limit per window, never a stream.

Each limit is tracked by provider, label and the window it belongs to (its
reset time). Inside one window an alert fires at most once per kind: the
highest threshold crossed, a forecast that it runs out before the reset, and,
once the window rolls over, that it has reset (only if it had got high).
State persists in alerts.json, so a restart does not repeat anything.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# Windows move their reset time by a second or so between readings.
SAME_WINDOW = 900


def _until(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"in {max(1, seconds // 60)} min"
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return f"in {hours} h {minutes} min" if minutes else f"in {hours} h"
    days, hours = divmod(seconds // 3600, 24)
    return f"in {days} d {hours} h" if hours else f"in {days} d"


def _clock(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


def _subject(name, group, label):
    """("Claude", None, "Weekly limit") -> "Claude weekly limit";
    ("Antigravity", "Gemini", "5-hour limit") -> "Antigravity Gemini 5-hour limit"."""
    words = str(label or "Limit")
    if words.split()[0] in ("Weekly", "Daily", "Monthly"):
        words = words[:1].lower() + words[1:]
    return " ".join(part for part in (name, group, words) if part)


def evaluate(model, current, state, now=None):
    """Return (alerts, new_state); alerts are (title, body) pairs to show."""
    now = time.time() if now is None else now
    levels = sorted(current.get("alert_levels") or [80, 95])
    windows = dict((state or {}).get("windows") or {})
    out = []
    seen = set()
    for provider in (model or {}).get("providers") or []:
        if provider.get("freshness"):
            continue  # an old reading is not news
        for group in provider.get("groups") or []:
            for row in group.get("rows") or []:
                pct, reset = row.get("pct"), row.get("reset_at")
                if pct is None or not reset:
                    continue
                key = f"{provider.get('id')}|{group.get('title') or ''}|{row.get('label')}"
                seen.add(key)
                subject = _subject(provider.get("name"), group.get("title"), row.get("label"))
                entry = windows.get(key)
                if entry is None or abs(float(entry.get("reset_at", 0)) - reset) > SAME_WINDOW:
                    if (entry and current.get("alert_reset") and float(entry.get("peak", 0)) >= levels[0]
                            and now >= float(entry.get("reset_at", 0)) - 60):
                        out.append((f"{subject} has reset", f"Now at {pct:.0f}%."))
                    entry = {"reset_at": reset, "sent": [], "peak": pct}
                entry = dict(entry, sent=list(entry.get("sent") or []))
                entry["peak"] = max(float(entry.get("peak", 0)), pct)
                crossed = [level for level in levels if pct >= level and f"{level:g}" not in entry["sent"]]
                if crossed:
                    level = crossed[-1]
                    entry["sent"].extend(f"{value:g}" for value in levels if value <= level)
                    if pct >= 100:
                        out.append((f"{subject} reached", f"It resets {_until(reset - now)}."))
                    else:
                        out.append((f"{subject} at {level:g}%",
                                    f"{pct:.0f}% used. It resets {_until(reset - now)}, at {_clock(reset)}."))
                runout = row.get("runout_at")
                if runout and current.get("alert_runout") and "runout" not in entry["sent"] and pct < 100:
                    entry["sent"].append("runout")
                    when = f"around {_clock(runout)}" if runout - now < 12 * 3600 else _until(runout - now)
                    out.append((f"{subject} runs out {when}",
                                f"{pct:.0f}% used at this pace. It resets {_until(reset - now)}."))
                windows[key] = entry
    # Forget limits that disappeared long ago, so the file stays small.
    windows = {k: v for k, v in windows.items() if k in seen or float(v.get("reset_at", 0)) > now - 7 * 86400}
    if not current.get("alerts", True):
        out = []
    elif float(current.get("mute_until") or 0) > now:
        out = []
    return out, {"windows": windows}


def load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    path = Path(path)
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(state), encoding="utf-8")
    staging.replace(path)
