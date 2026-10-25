"""User choices, shared by the tray and the native flyout.

settings.json sits beside the cache (%LOCALAPPDATA%\\Fleet\\settings.json), so
clearing the cache never resets them. The flyout writes it from its settings
page; the tray reads it on every icon update and poll, so a change shows within
a couple of seconds without any message between the two processes.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import fleet

PATH = fleet.CACHE.parent / "settings.json"
UNITS = ("used", "left")
TRAY_ICONS = ("bars", "ring", "number")
DEFAULTS = {
    "units": "used",
    "tray_icon": "bars",
    "alerts": True,
    "cost": True,
    "alert_levels": [80, 95],
    "alert_runout": True,
    "alert_reset": True,
    "hidden": [],
    "mute_until": 0,
}


def load(path=None):
    """Settings merged over the defaults; anything malformed falls back quietly."""
    path = Path(path or PATH)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out = dict(DEFAULTS)
    if raw.get("units") in UNITS:
        out["units"] = raw["units"]
    if raw.get("tray_icon") in TRAY_ICONS:
        out["tray_icon"] = raw["tray_icon"]
    for key in ("alerts", "alert_runout", "alert_reset", "cost"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    levels = raw.get("alert_levels")
    if isinstance(levels, list) and all(isinstance(v, (int, float)) and 0 < v <= 100 for v in levels):
        out["alert_levels"] = sorted({float(v) for v in levels})
    if isinstance(raw.get("hidden"), list):
        out["hidden"] = [str(v) for v in raw["hidden"] if isinstance(v, str)]
    if isinstance(raw.get("mute_until"), (int, float)):
        out["mute_until"] = float(raw["mute_until"])
    return out


def save(changes, path=None):
    """Merge `changes` into the stored settings, atomically."""
    path = Path(path or PATH)
    current = load(path)
    current.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(current, indent=2), encoding="utf-8")
    os.replace(staging, path)
    return current


def muted(current, now=None):
    return float(current.get("mute_until") or 0) > (time.time() if now is None else now)


def end_of_today(now=None):
    """Local midnight after `now`: "mute alerts for today"."""
    local = time.localtime(time.time() if now is None else now)
    midnight = time.mktime((local.tm_year, local.tm_mon, local.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return midnight
