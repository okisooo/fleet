"""The tray icon and tooltip: a live meter, drawn pixel-exact at the taskbar's size.

The notification area shows a 16 px icon at 100% scaling (20, 24, 32 px at
125, 150, 200%). Letting Windows shrink a big bitmap blurs it, so every frame
is drawn at its own size on whole pixels. The glyph follows the taskbar's own
theme (white on a dark taskbar, near-black on a light one) and uses colour only
for state: amber when a limit runs ahead of its window or passes 80%, red near
the cap. It never animates; it changes only when the numbers do.
"""
from __future__ import annotations

import ctypes
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SS = 4  # draw four times larger, then reduce: smooth ends on whole pixels
FRAME_SIZES = (16, 20, 24, 32, 40, 48)
PALETTE = {
    False: {"fg": (255, 255, 255), "warn": (252, 225, 0), "high": (255, 153, 164), "ink": (24, 24, 24)},
    True: {"fg": (26, 26, 26), "warn": (157, 93, 0), "high": (196, 43, 28), "ink": (255, 255, 255)},
}


def taskbar_is_light():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except OSError:
        return False


def small_icon_size():
    """The taskbar's icon size in pixels; needs a DPI-aware process to be true."""
    user32 = ctypes.windll.user32
    try:
        return int(user32.GetSystemMetricsForDpi(49, user32.GetDpiForSystem()))  # SM_CXSMICON
    except AttributeError:
        return int(user32.GetSystemMetrics(49)) or 16


def _tone(ring):
    if ring is None:
        return "calm"
    if ring.get("state") in ("full", "high") or (ring.get("pct") or 0) >= 95:
        return "high"
    if ring.get("state") == "ahead" or (ring.get("pct") or 0) >= 80:
        return "warn"
    return "calm"


def _pressure(provider):
    score = 0.0
    for group in provider.get("groups") or []:
        for row in group.get("rows") or []:
            if row.get("pct") is None:
                continue
            bonus = {"full": 100, "high": 20, "ahead": 40}.get(row.get("state"), 0)
            score = max(score, row["pct"] + bonus)
    return score


def spec(model, current):
    """What the icon shows: the provider whose limits press hardest right now."""
    rings = [p for p in (model or {}).get("providers") or [] if p.get("kind") == "rings" and p.get("rings")]
    style = current.get("tray_icon", "bars")
    if not rings:
        return {"style": style, "empty": True}
    provider = max(rings, key=_pressure)
    outer = provider["rings"][0]
    inner = provider["rings"][1] if len(provider["rings"]) > 1 else None
    left = current.get("units") == "left"
    shown = lambda ring: None if ring is None else (100 - ring["pct"] if left else ring["pct"])
    fullest = max(provider["rings"], key=lambda r: r["pct"])
    worst = "high" if "high" in (_tone(outer), _tone(inner)) else "warn" if "warn" in (_tone(outer), _tone(inner)) else "calm"
    return {
        "style": style, "empty": False, "provider": provider.get("name"),
        "top": shown(inner), "top_tone": _tone(inner),
        "bottom": shown(outer), "bottom_tone": _tone(outer),
        "value": shown(fullest), "tone": worst,
        "stale": bool((provider.get("freshness") or {}).get("stale")),
    }


def draw(glyph, size, light=False):
    """One frame of the icon, `size` pixels square, RGBA."""
    colors = PALETTE[bool(light)]
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fg = colors["fg"]
    track = fg + (92,)
    unit = size / 16

    def tone(name):
        return colors.get(name, fg) if name in ("warn", "high") else fg

    if glyph.get("empty"):
        r = round(3 * unit) * SS
        d.rounded_rectangle((SS, round(3 * unit) * SS, big - SS - 1, big - round(3 * unit) * SS - 1),
                            radius=r, outline=track, width=max(SS, round(1.2 * unit * SS)))
    elif glyph["style"] == "ring":
        stroke = max(2, round(2.5 * unit)) * SS
        inset = SS // 2 + stroke // 2
        box = (inset, inset, big - inset - 1, big - inset - 1)
        d.ellipse(box, outline=track, width=stroke)
        value = max(0.0, min(100.0, glyph.get("value") or 0))
        if value > 0:
            d.arc(box, -90, -90 + 360 * value / 100, fill=tone(glyph["tone"]) + (255,), width=stroke)
    elif glyph["style"] == "number":
        top = round(1.5 * unit) * SS
        fill = tone(glyph["tone"]) if glyph["tone"] != "calm" else fg
        d.rounded_rectangle((0, top, big - 1, big - top - 1), radius=round(3.5 * unit) * SS, fill=fill + (255,))
        value = glyph.get("value") or 0
        text = "!" if value >= 100 else f"{round(value):d}"
        ink = colors["ink"] if fill == fg else (24, 24, 24)
        font = _font(round((9 if len(text) > 1 else 10) * unit * SS))
        d.text((big / 2, big / 2 + SS * unit * 0.4), text, fill=ink + (255,), font=font, anchor="mm")
    else:
        margin = max(1, round(unit))
        height = max(2, round(4 * unit))
        gap = max(1, round(2 * unit))
        width = size - 2 * margin
        bars = [(glyph.get("top"), glyph.get("top_tone")), (glyph.get("bottom"), glyph.get("bottom_tone"))]
        bars = [bar for bar in bars if bar[0] is not None]
        if len(bars) == 1:
            height = max(3, round(6 * unit))
        total = len(bars) * height + (len(bars) - 1) * gap
        y = (size - total) // 2
        for value, name in bars:
            x0, y0, x1, y1 = margin * SS, y * SS, (margin + width) * SS - 1, (y + height) * SS - 1
            radius = height * SS // 2
            d.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=track)
            value = max(0.0, min(100.0, value))
            if value > 0:
                fill = max(height, round(width * value / 100)) * SS
                d.rounded_rectangle((x0, y0, x0 + fill - 1, y1), radius=min(radius, fill // 2),
                                    fill=tone(name) + (255,))
            y += height + gap
    frame = img.resize((size, size), Image.LANCZOS)
    if glyph.get("stale"):
        alpha = frame.getchannel("A").point(lambda a: int(a * 0.55))
        frame.putalpha(alpha)
    return frame


def write_ico(glyph, path, light=False, sizes=FRAME_SIZES):
    """An .ico with every frame drawn at its own size; returns the path."""
    frames = [draw(glyph, size, light) for size in sorted(set(sizes))]
    path = Path(path)
    largest = frames[-1]
    largest.save(path, format="ICO", sizes=[f.size for f in frames], append_images=frames[:-1])
    return path


_FONTS = {}


def _font(px):
    if px not in _FONTS:
        for name in (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf"):
            try:
                _FONTS[px] = ImageFont.truetype(name, px)
                break
            except OSError:
                continue
        else:
            _FONTS[px] = ImageFont.load_default()
    return _FONTS[px]


def tooltip(model, current):
    """One line per provider, inside the 127 characters Windows allows."""
    left = current.get("units") == "left"
    lines = []
    for provider in (model or {}).get("providers") or []:
        if provider.get("kind") == "rings" and provider.get("rings"):
            parts = [f"{(100 - r['pct']) if left else r['pct']:.0f}% {r['label'].lower()}" for r in reversed(provider["rings"])]
            lines.append(f"{provider['name']}  " + " · ".join(parts) + (" left" if left else ""))
        elif provider.get("balance"):
            lines.append(f"{provider['name']}  {provider['balance']['value']}")
    for item in (model or {}).get("inactive") or []:
        lines.append(f"{item['name']}  {item['label'].lower()}")
    text = "\n".join(lines) or "Fleet"
    return text if len(text) <= 127 else text[:126] + "…"
