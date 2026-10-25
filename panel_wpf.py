"""Native WPF flyout renderer for Fleet.

The warm host keeps one WPF window ready between clicks. Win11 system acrylic
(DWMWA_SYSTEMBACKDROP_TYPE) is the material: a thin tint lets the real blur of
the desktop through, and each provider floats on its own lit glass platter.
Motion is a content TranslateTransform + opacity fade, never a Window.Top move.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import re
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape


WPF = (
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / r"Microsoft.NET\Framework64\v4.0.30319\WPF"
)
WM_NCACTIVATE = 0x0086
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_ROUND = 2
# Transient-window acrylic is the official Win11 flyout material (menus, popups).
DWMSBT_TRANSIENTWINDOW = 3
WCA_ACCENT_POLICY = 19
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
MONITOR_DEFAULTTONEAREST = 2
# Content-space motion only. Animating Window.Top forces a full DWM window move
# every frame and is what made the old slide feel capped around 30 fps.
SLIDE_DISTANCE = 10.0
OPEN_MS = 160
CLOSE_MS = 110
PANEL_EDGE_GAP = 12.0
PANEL_ANCHOR_GAP = 10.0
# Must match what DWM actually rounds the window to. DWMWCP_ROUND is the Windows 11
# standard 8 px for a flyout of this size. Drawing the rim at a different radius put
# three curves on screen at once, which is what made the corners look mislayered.
PANEL_CORNER_RADIUS = 10.0
# Glass platters inset by WINDOW_PADDING. A slightly tighter curve than the
# window keeps them nested instead of reading as a bubble inside a bubble.
SURFACE_CORNER_RADIUS = 8.0
# Capsules (status, plan tags, buttons) are fully round at their own height.
CAPSULE_RADIUS = 10.0
PANEL_WIDTH = 320.0
# Real chrome: GlassClip padding, then one lit platter per provider.
WINDOW_PADDING = 10.0
CARD_PADDING = 12.0
CARD_BORDER = 1.0
PANEL_PADDING = WINDOW_PADDING
PANEL_BORDER = 1.0
PLATTER_GAP = 8.0
# One quota is one line: name, meter, value, reset. The old stacked form spent a
# 30 px row plus a full-width slab on every window and pushed a four-provider
# reading past the height of the flyout.
ROW_HEIGHT = 22.0
AMOUNT_ROW_HEIGHT = 20.0
PROVIDER_HEADER_HEIGHT = 22.0
BAR_HEIGHT = 6.0
CONTENT_WIDTH = (
    PANEL_WIDTH
    - 2 * PANEL_BORDER
    - 2 * WINDOW_PADDING
    - 2 * CARD_BORDER
    - 2 * CARD_PADDING
)
LABEL_COLUMN = 52.0
METER_COLUMN = 146.0
VALUE_COLUMN = 40.0
RESET_COLUMN = CONTENT_WIDTH - LABEL_COLUMN - METER_COLUMN - VALUE_COLUMN
METER_INSET_LEFT = 4.0
METER_INSET_RIGHT = 10.0
BAR_TRACK = METER_COLUMN - METER_INSET_LEFT - METER_INSET_RIGHT
NAME_MAX_WIDTH = CONTENT_WIDTH - 30.0


def _bar_fill(pct):
    """Bar width in DIPs. Zero stays empty rather than showing a stub."""
    if pct <= 0.0:
        return 0.0
    return max(3.0, BAR_TRACK * pct / 100.0)
PROVIDER_MARKS = {"Claude": "C", "Codex": "X", "Grok": "G", "Antigrav": "A"}
# Monograms sit on the same white tile in both themes.
ICON_INK = "#FF3F3F48"
GRAD = {"g": "#FF6CCB7B", "y": "#FFFFC145", "r": "#FFFF6B6B", "n": "#FF8A8A99"}
# Quotas use one calm base colour; amber and coral are reserved for pressure.
# Freshness remains green/amber/red through GRAD, so state and consumption never
# compete for the same visual meaning.
METER_PRESSURE = {"y": "#FFFFB224", "r": "#FFFF5C5C", "n": "#FF8A8A99"}
# Used when the shell has no accent to offer, or when the accent is itself warm
# enough to be mistaken for the pressure colours below.
METER_FALLBACK = {"dark": "#FF60CDFF", "light": "#FF0067C0"}
_ACCENT = {}


def _is_warm(rgb):
    """True for an accent that would be mistaken for the pressure colours.

    An amber or red accent on a low quota reads as a warning, so those users
    keep the neutral base and only real pressure gets a warm meter.
    """
    import colorsys

    hue, saturation, _ = colorsys.rgb_to_hsv(*[channel / 255 for channel in rgb])
    return saturation > 0.25 and (hue * 360 < 45 or hue * 360 > 330)


def _accent_pair():
    """The user's Windows accent as (dark_surface_hex, light_surface_hex).

    Windows stores a ramp of accent shades. The light end of the ramp is what
    the shell puts on dark surfaces and the dark end is what it puts on light
    ones, so reading both is what makes Fleet's meters look like the rest of
    the system instead of a colour someone picked.
    """
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM"
        ) as key:
            abgr = int(winreg.QueryValueEx(key, "AccentColor")[0])
    except Exception:
        return None
    base = (abgr & 0xFF, (abgr >> 8) & 0xFF, (abgr >> 16) & 0xFF)
    if _is_warm(base):
        return None
    dark_surface = "#FF%02X%02X%02X" % base
    light_surface = dark_surface
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent",
        ) as key:
            palette = winreg.QueryValueEx(key, "AccentPalette")[0]
        if len(palette) >= 20:
            light_surface = "#FF%02X%02X%02X" % tuple(palette[16:19])
    except Exception:
        pass
    return dark_surface, light_surface


def _accent(light):
    if "pair" not in _ACCENT:
        try:
            _ACCENT["pair"] = _accent_pair()
        except Exception:
            _ACCENT["pair"] = None
    pair = _ACCENT["pair"]
    if not pair:
        return METER_FALLBACK["light" if light else "dark"]
    return pair[1] if light else pair[0]


def _meter(light):
    """Consumption ramp: the system accent until a quota is actually under pressure."""
    ramp = dict(METER_PRESSURE, g=_accent(light))
    return {f"meter_{key}": value for key, value in ramp.items()}
ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "providers"


def _load_wpf():
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
    import clr

    for name in ("WindowsBase", "PresentationCore", "PresentationFramework"):
        clr.AddReference(str(WPF / f"{name}.dll"))
    # The flyout uses stock WPF/DWM primitives only.


def _sevkey(pct):
    if pct is None:
        return "n"
    return "g" if pct < 60 else "y" if pct < 85 else "r"


def _x(value):
    return escape(str(value), {'"': "&quot;"})


def _point_outside(rect, point):
    left, top, right, bottom = rect
    x, y = point
    return not (left <= x < right and top <= y < bottom)


def _should_dismiss(panel_rect, point, anchor_rect=None):
    return _point_outside(panel_rect, point) and (
        anchor_rect is None or _point_outside(anchor_rect, point)
    )


def _panel_origin(
    work_rect_px,
    cursor_px,
    window_size_dip,
    dpi_scale,
    edge_gap=PANEL_EDGE_GAP,
):
    """Place a flyout inside its monitor work area using WPF device-independent units."""
    scale_x, scale_y = dpi_scale
    work_left, work_top, work_right, work_bottom = work_rect_px
    work_left /= scale_x
    work_right /= scale_x
    work_top /= scale_y
    work_bottom /= scale_y
    cursor_x = cursor_px[0] / scale_x
    width, height = window_size_dip

    left = max(
        work_left + edge_gap,
        min(cursor_x - width + 34.0, work_right - width - edge_gap),
    )
    top = max(work_top + edge_gap, work_bottom - height - edge_gap)
    return left, top


def _anchored_panel_origin(
    work_rect_px,
    anchor_rect_px,
    window_size_dip,
    dpi_scale,
    edge_gap=PANEL_ANCHOR_GAP,
):
    """Position a flyout against the taskbar edge containing its notification icon."""
    scale_x, scale_y = dpi_scale
    wl, wt, wr, wb = work_rect_px
    al, at, ar, ab = anchor_rect_px
    wl, wr, al, ar = (value / scale_x for value in (wl, wr, al, ar))
    wt, wb, at, ab = (value / scale_y for value in (wt, wb, at, ab))
    width, height = window_size_dip
    anchor_x = (al + ar) / 2.0
    anchor_y = (at + ab) / 2.0

    if at >= wb - 1.0:
        edge = "bottom"
    elif ab <= wt + 1.0:
        edge = "top"
    elif al >= wr - 1.0:
        edge = "right"
    elif ar <= wl + 1.0:
        edge = "left"
    else:
        distances = {
            "bottom": abs(anchor_y - wb),
            "top": abs(anchor_y - wt),
            "right": abs(anchor_x - wr),
            "left": abs(anchor_x - wl),
        }
        edge = min(distances, key=distances.get)

    if edge in ("bottom", "top"):
        left = max(wl + edge_gap, min(anchor_x - width / 2.0, wr - width - edge_gap))
        top = wb - height - edge_gap if edge == "bottom" else wt + edge_gap
    else:
        left = wr - width - edge_gap if edge == "right" else wl + edge_gap
        top = max(wt + edge_gap, min(anchor_y - height / 2.0, wb - height - edge_gap))
    return left, top


def _defer_to_dispatcher(dispatcher, callback):
    """Queue `callback` on its own dispatcher frame, which is never input-sync."""
    from System import Action
    from System.Windows.Threading import DispatcherPriority

    dispatcher.BeginInvoke(DispatcherPriority.Input, Action(callback))


def _present_prepared_window(window, anchor_rect, position, prepare_open, begin_slide):
    """Show the warm flyout at its final rect, then play content-space motion."""
    position(anchor_rect)
    window.UpdateLayout()
    prepare_open()
    window.Show()
    begin_slide()

    def take_foreground():
        try:
            window.Activate()
        except Exception:
            # Losing the foreground race is cosmetic -- the flyout is already on
            # screen and the mouse watcher still light-dismisses it.
            pass

    # Taking the foreground reaches COM. Run from inside an input-synchronous
    # dispatch -- which is exactly what an activation handoff from the tray click
    # is -- that call raises RPC_E_CANTCALLOUT_ININPUTSYNCCALL (0x8001010d), a
    # native fault that killed the whole panel host with no Python traceback.
    dispatcher = getattr(window, "Dispatcher", None)
    if dispatcher is None:
        take_foreground()
        return
    _defer_to_dispatcher(dispatcher, take_foreground)


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


def _monitor_work_area(point):
    monitor = ctypes.windll.user32.MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST)
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    if not monitor or not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    return (
        info.rcWork.left,
        info.rcWork.top,
        info.rcWork.right,
        info.rcWork.bottom,
    )


def _set_dwm_int(hwnd, attribute, value):
    native_value = ctypes.c_int(value)
    return ctypes.windll.dwmapi.DwmSetWindowAttribute(
        hwnd,
        attribute,
        ctypes.byref(native_value),
        ctypes.sizeof(native_value),
    )


class _MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def _light_backdrop(hwnd, send=None):
    """Draw the acrylic in its active state whether or not the flyout has focus.

    DWM paints an inactive window's system backdrop as a flat fill: no blur and
    no colour from behind. The flyout lives in the panel host, which never sees
    the tray click, so its Activate() can lose the foreground race -- and every
    such open showed a grey card instead of glass. WM_NCACTIVATE(TRUE) changes
    only the frame's active look; focus is left alone.
    """
    if not hwnd:
        return
    send = send or ctypes.windll.user32.SendMessageW
    send(ctypes.wintypes.HWND(hwnd), WM_NCACTIVATE, 1, 0)


def _force_opaque():
    """Local escape hatch: FLEET_OPAQUE=1 keeps the solid panel."""
    return os.environ.get("FLEET_OPAQUE", "").strip().lower() in {"1", "true", "yes"}


class _AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_int),
    ]


class _CompositionData(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


def _material():
    """Which backdrop draws the glass: "accent" (default), "system" or "opaque".

    FLEET_MATERIAL picks one explicitly; FLEET_OPAQUE=1 still means opaque.
    """
    if _force_opaque():
        return "opaque"
    choice = os.environ.get("FLEET_MATERIAL", "").strip().lower()
    return choice if choice in {"accent", "system", "opaque"} else "accent"


def _accent_tint(light):
    """ACCENT_POLICY colour, 0xAABBGGRR: a thin neutral veil under Fleet's own tint."""
    return 0x59F3F3F3 if light else 0x8C202020


def _apply_accent_acrylic(hwnd, light):
    policy = _AccentPolicy(ACCENT_ENABLE_ACRYLICBLURBEHIND, 2, _accent_tint(light), 0)
    data = _CompositionData(
        WCA_ACCENT_POLICY, ctypes.cast(ctypes.byref(policy), ctypes.c_void_p),
        ctypes.sizeof(policy),
    )
    try:
        return bool(ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))
    except AttributeError:
        return False


def _apply_window_chrome(hwnd, light):
    """Round corners, match theme, and put live acrylic behind the client.

    The default is the accent acrylic (SetWindowCompositionAttribute). On build
    29648 the official DWMSBT_TRANSIENTWINDOW backdrop drew its flat inactive fill
    even for a focused flyout, so the owner saw no glass at all, while accent
    acrylic kept blurring. It used to composite black under WPF only because the
    client was never extended into the frame; with the frame extended and the
    HwndSource cleared, it renders correctly. FLEET_MATERIAL=system returns to
    the official backdrop. Either way the window stays non-layered.
    """
    _set_dwm_int(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 0 if light else 1)
    _set_dwm_int(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    material = _material()
    if material == "opaque":
        return False
    margins = _MARGINS(-1, -1, -1, -1)
    if ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)) != 0:
        return False
    if material == "accent" and _apply_accent_acrylic(hwnd, light):
        return True
    return _set_dwm_int(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW) == 0


def _mix(fore, back, amount):
    """Solid blend of two #AARRGGBB colours, for fallbacks that must stay opaque."""
    a = [int(fore[i:i + 2], 16) for i in (3, 5, 7)]
    b = [int(back[i:i + 2], 16) for i in (3, 5, 7)]
    return "#FF" + "".join(
        "%02X" % round(x * amount + y * (1 - amount)) for x, y in zip(a, b)
    )


def _theme(light, acrylic=True):
    """Layered-glass tokens.

    The window tint stays thin so the system acrylic -- a real blur of whatever
    is behind the flyout -- carries the material, the way a macOS popover lets
    its vibrancy show. Each provider floats on a brighter platter whose top rim
    catches the light and whose bottom rim falls away. Foregrounds stay opaque
    so contrast never rides whatever is behind the glass.
    """
    meter = _meter(light)
    accent = meter["meter_g"]
    if light:
        base = {
            "fg": "#FF16161A",
            "muted": "#FF4D4D56",
            "faint": "#FF6C6C77",
            **meter,
        }
        if acrylic:
            return dict(base, **{
                # A sheen: the top of the window is lit, the rest lets glass through.
                "surface_hi": "#9EFFFFFF",
                "surface": "#80F2F2F5",
                "border_top": "#F2FFFFFF",
                "border_bot": "#40FFFFFF",
                "card_bg": "#9EFFFFFF",
                "card_hi": "#BDFFFFFF",
                "rim_top": "#FFFFFFFF",
                "rim_bot": "#1C000000",
                "card_border": "#14000000",
                "icon_bg": "#FFFFFFFF",
                "icon_rim": "#14000000",
                "track": "#17000000",
                "pill_bg": "#0F000000",
                "chip": "#FFFFFFFF",
                "control_bg": "#B8FFFFFF",
                "control_border": "#24000000",
                "control_hover": "#F0FFFFFF",
                "accent_soft": "#1F" + accent[3:],
                "accent_soft_hover": "#33" + accent[3:],
            })
        return dict(base, **{
            "surface_hi": "#FFF7F7FA",
            "surface": "#FFF2F2F5",
            "border_top": "#FFDDDDE3",
            "border_bot": "#FFDDDDE3",
            "card_bg": "#FFFFFFFF",
            "card_hi": "#FFFFFFFF",
            "rim_top": "#FFE6E6EB",
            "rim_bot": "#FFD5D5DC",
            "card_border": "#FFE6E6EC",
            "icon_bg": "#FFFFFFFF",
            "icon_rim": "#FFE3E3E8",
            "track": "#FFE4E4EA",
            "pill_bg": "#FFEDEDF2",
            "chip": "#FFFFFFFF",
            "control_bg": "#FFFFFFFF",
            "control_border": "#FFDDDDE3",
            "control_hover": "#FFF5F5F8",
            "accent_soft": _mix(accent, "#FFFFFFFF", 0.12),
            "accent_soft_hover": _mix(accent, "#FFFFFFFF", 0.2),
        })
    base = {
        "fg": "#FFFFFFFF",
        "muted": "#FFD3D3DB",
        "faint": "#FFA2A2AE",
        **meter,
    }
    if acrylic:
        return dict(base, **{
            "surface_hi": "#66303036",
            "surface": "#801A1A1E",
            # Glass lights its top edge and lets the bottom fall away; one flat
            # rim is what made the old panel read as a rectangle pasted on the
            # desktop.
            "border_top": "#4DFFFFFF",
            "border_bot": "#0FFFFFFF",
            "card_bg": "#12FFFFFF",
            "card_hi": "#21FFFFFF",
            "rim_top": "#3DFFFFFF",
            "rim_bot": "#0AFFFFFF",
            "card_border": "#1AFFFFFF",
            # Provider artwork ships on white, so the tile stays white in dark
            # mode too -- an app icon, not a white square floating in a grey one.
            "icon_bg": "#FFFFFFFF",
            "icon_rim": "#33FFFFFF",
            "track": "#2EFFFFFF",
            "pill_bg": "#1AFFFFFF",
            "chip": "#FF232329",
            "control_bg": "#1FFFFFFF",
            "control_border": "#33FFFFFF",
            "control_hover": "#33FFFFFF",
            "accent_soft": "#26" + accent[3:],
            "accent_soft_hover": "#40" + accent[3:],
        })
    return dict(base, **{
        "surface_hi": "#FF26262B",
        "surface": "#FF202024",
        "border_top": "#FF3A3A42",
        "border_bot": "#FF2C2C33",
        "card_bg": "#FF2A2A30",
        "card_hi": "#FF2E2E35",
        "rim_top": "#FF3C3C44",
        "rim_bot": "#FF2E2E35",
        "card_border": "#FF35353D",
        "icon_bg": "#FFFFFFFF",
        "icon_rim": "#FF3A3A42",
        "muted": "#FFC8C8D0",
        "faint": "#FF8E8E9A",
        "track": "#FF3A3A42",
        "pill_bg": "#FF34343C",
        "chip": "#FF24242A",
        "control_bg": "#FF2E2E36",
        "control_border": "#FF3E3E48",
        "control_hover": "#FF383842",
        "accent_soft": _mix(accent, "#FF2A2A30", 0.18),
        "accent_soft_hover": _mix(accent, "#FF2A2A30", 0.28),
    })


def _plan_label(value):
    raw = str(value or "").strip()
    known = {
        "pro": "Pro",
        "prolite": "Pro",
        "plus": "Plus",
        "gemini": "Gemini",
        "x premium": "X Premium",
        "supergrok": "SuperGrok",
        "max": "Max",
        "team": "Team",
        "enterprise": "Enterprise",
    }
    return known.get(raw.casefold(), raw)


SHORT_LABELS = {
    "Claude Wk": "week",
    "Extra $": "extra",
    "Session": "session",
    "Weekly": "week",
    "Daily": "day",
    "Monthly": "month",
    "Balance": "balance",
    "Credits": "credits",
    "Pay-go": "pay-go",
}


def _short_label(value):
    raw = str(value or "").strip()
    return SHORT_LABELS.get(raw, raw.lower())


def _looks_like_money(extra):
    text = str(extra or "").strip()
    if not text:
        return False
    if text[0] in "$¥€£":
        return True
    upper = text.upper()
    return upper.endswith((" USD", " CNY", " EUR", " GBP"))


def _is_zero_money_metric(metric):
    """Hide zero-dollar facts, but never hide an actual zero-percent quota."""
    if metric.get("pct") is not None:
        return False
    extra = str(metric.get("extra") or "").strip()
    if not _looks_like_money(extra):
        return False
    numeric = re.sub(r"[^0-9.+-]", "", extra.replace(",", ""))
    try:
        return float(numeric) == 0
    except ValueError:
        return False


def _is_inert_metric(metric):
    """Drop facts that only say a disabled optional feature is off."""
    if metric.get("pct") is not None:
        return False
    return str(metric.get("extra") or "").strip().casefold() in {
        "off",
        "disabled",
        "inactive",
    }


def _partition_agents(data):
    """Keep usable readings visible; cached values carry their original age."""
    live = []
    inactive = []
    for agent in data:
        copied = dict(agent)
        inert = [
            metric for metric in (agent.get("metrics") or [])
            if _is_inert_metric(metric)
        ]
        copied["metrics"] = [
            metric for metric in (agent.get("metrics") or [])
            if not _is_zero_money_metric(metric) and not _is_inert_metric(metric)
        ]
        if (
            copied["metrics"]
            and copied.get("configured") is not False
        ):
            live.append(copied)
        elif (copied.get("err") or inert) and copied.get("known", True):
            if not copied.get("err"):
                copied["_inactive_reason"] = str(inert[0].get("extra") or "inactive")
            inactive.append(copied)
    return live, inactive


def _visible_agents(data):
    """Return providers with a meaningful reading, including labelled cache."""
    return _partition_agents(data)[0]



def _provider_icon_markup(raw_name, theme, mark):
    name = raw_name.casefold()
    mapped = {
        "claude": "claude.png",
        "codex": "codex.png",
        "grok": "grok.ico",
        "antigrav": "antigravity.png",
        "antigravity": "antigravity.png",
    }.get(name, f"{name}.png")

    # An app-icon tile: a solid squircle with a hairline, so artwork with its own
    # transparency still sits on something instead of floating on the glass.
    tile = (
        f"""<Border CornerRadius="6" Background="{theme['icon_bg']}"
                      BorderBrush="{theme['icon_rim']}" BorderThickness="1"/>"""
    )
    icon_file = ASSETS_DIR / mapped
    if icon_file.exists():
        uri = _x(icon_file.absolute().as_uri())
        return f"""
            <Grid Width="22" Height="22" Margin="0,0,9,0" VerticalAlignment="Center">
              {tile}
              <Image Source="{uri}" Width="14" Height="14" HorizontalAlignment="Center" VerticalAlignment="Center" RenderOptions.BitmapScalingMode="HighQuality"/>
            </Grid>"""

    return f"""
            <Grid Width="22" Height="22" Margin="0,0,9,0" VerticalAlignment="Center">
              {tile}
              <TextBlock Text="{_x(mark or '?')}" Foreground="{ICON_INK}"
                         FontSize="{'8' if len(mark or '') > 1 else '10'}"
                         FontWeight="SemiBold" HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Grid>"""


def _metric_row(
    metric,
    theme,
    last=False,
):
    """One quota on one line: name, meter, value, and when it turns over."""
    label_raw = _short_label(metric.get("label")) or "status"
    label = _x(label_raw)
    pct = metric.get("pct")
    extra = (metric.get("extra") or "").strip()
    reset = _x(metric.get("_reset_text") or "")
    margin = "0" if last else "0,0,0,1"
    tooltip = _x(metric.get("_tooltip") or "")
    tooltip_attr = f' ToolTip="{tooltip}"' if tooltip else ""
    columns = f"""
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="{LABEL_COLUMN:.0f}"/>
          <ColumnDefinition Width="{METER_COLUMN:.0f}"/>
          <ColumnDefinition Width="{VALUE_COLUMN:.0f}"/>
          <ColumnDefinition Width="{RESET_COLUMN:.0f}"/>
        </Grid.ColumnDefinitions>"""

    # Pure amount rows (balance/credits/extra): no meter, so the value takes the
    # width the meter would have used instead of being squeezed into a column
    # sized for "100%".
    if pct is None:
        value = _x(extra or "—")
        return f"""
        <Grid Tag="MetricAmount" Height="{AMOUNT_ROW_HEIGHT:.0f}" Margin="{margin}"{tooltip_attr}>{columns}
          <TextBlock Grid.ColumnSpan="2" Text="{label}" Foreground="{theme['muted']}"
                     FontSize="10.5" FontWeight="Medium" VerticalAlignment="Center"
                     TextTrimming="CharacterEllipsis"/>
          <TextBlock Grid.Column="2" Grid.ColumnSpan="2" Text="{value}"
                     Foreground="{theme['fg']}" FontSize="12" FontWeight="SemiBold"
                     HorizontalAlignment="Right" Margin="8,0,0,0"
                     Typography.NumeralAlignment="Tabular" VerticalAlignment="Center"/>
        </Grid>"""

    pct = max(0.0, min(100.0, float(pct)))
    fill_color = theme[f"meter_{_sevkey(pct)}"]
    # Only currency extras replace the percentage. Count extras like 310/4000 cr
    # remain percentage-first so the usage visual stays comparable.
    money = bool(extra and _looks_like_money(extra))
    value = _x(extra) if money else f"{pct:.0f}%"
    value_color = fill_color if _sevkey(pct) == "r" else theme["fg"]
    # A currency amount needs the reset column's width; a percentage never does.
    value_span = ' Grid.ColumnSpan="2"' if money else ""
    reset_block = (
        f"""<TextBlock Grid.Column="3" Text="{reset}" Foreground="{theme['faint']}"
                     FontSize="9.5" HorizontalAlignment="Right" Margin="6,0,0,0"
                     Typography.NumeralAlignment="Tabular" VerticalAlignment="Center"/>"""
        if reset and not money
        else ""
    )
    return f"""
      <Grid Tag="MetricUsage" Height="{ROW_HEIGHT:.0f}" Margin="{margin}"{tooltip_attr}>{columns}
        <TextBlock Text="{label}" Foreground="{theme['muted']}" FontSize="11"
                   FontWeight="Medium" VerticalAlignment="Center"
                   TextTrimming="CharacterEllipsis"/>
        <Grid Grid.Column="1" Height="{BAR_HEIGHT:.0f}" VerticalAlignment="Center"
              Margin="{METER_INSET_LEFT:.0f},0,{METER_INSET_RIGHT:.0f},0">
          <Border Background="{theme['track']}" CornerRadius="{BAR_HEIGHT / 2:.0f}"/>
          <Border Width="{_bar_fill(pct):.1f}" HorizontalAlignment="Left"
                  CornerRadius="{BAR_HEIGHT / 2:.0f}" Background="{fill_color}"/>
        </Grid>
        <TextBlock Grid.Column="2"{value_span} Text="{value}" Foreground="{value_color}"
                   FontSize="12" FontWeight="SemiBold" HorizontalAlignment="Right"
                   Typography.NumeralAlignment="Tabular" VerticalAlignment="Center"/>
        {reset_block}
      </Grid>"""


def _compact_quota(metric, theme):
    """One always-visible Antigravity value in the four-column quota strip."""
    label = _x(_short_label(metric.get("label")) or "quota")
    window = _x(label.rsplit(" ", 1)[-1])
    pct = max(0.0, min(100.0, float(metric.get("pct") or 0)))
    reset = str(metric.get("_reset_text") or "").strip()
    tooltip = _x(f"{label} · resets {reset}" if reset else label)
    severity = _sevkey(pct)
    value_color = theme[f"meter_{severity}"] if severity == "r" else theme["fg"]
    return f"""
      <Grid Tag="MetricCompact" Height="28" Margin="2,0" ToolTip="{tooltip}"
            AutomationProperties.Name="{label} at {pct:.0f}%">
        <Grid.RowDefinitions>
          <RowDefinition Height="*"/>
          <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>
        <TextBlock Text="{pct:.0f}%" Foreground="{value_color}" FontSize="12"
                   FontWeight="SemiBold" HorizontalAlignment="Center"
                   Typography.NumeralAlignment="Tabular" VerticalAlignment="Center"/>
        <TextBlock Tag="QuotaWindow" Grid.Row="1" Text="{window}"
                   Foreground="{theme['faint']}" FontSize="9" FontWeight="Medium"
                   HorizontalAlignment="Center" Margin="0,-1,0,0"/>
      </Grid>"""


def _quota_grid(metrics, theme):
    order = {
        "gemini week": 0,
        "gemini session": 1,
        "claude week": 2,
        "claude session": 3,
    }
    metrics = sorted(metrics, key=lambda metric: order.get(str(metric.get("label")), 99))
    return f"""
      <Grid Tag="QuotaGrid" Height="40" Margin="0,-2,0,-2">
        <Grid.RowDefinitions>
          <RowDefinition Height="12"/>
          <RowDefinition Height="28"/>
        </Grid.RowDefinitions>
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <TextBlock Tag="QuotaGroup" Text="gemini" Foreground="{theme['muted']}"
                     FontSize="9" FontWeight="Medium" HorizontalAlignment="Center"/>
          <TextBlock Tag="QuotaGroup" Grid.Column="1" Text="claude"
                     Foreground="{theme['muted']}" FontSize="9" FontWeight="Medium"
                     HorizontalAlignment="Center"/>
        </Grid>
        <UniformGrid Tag="QuotaValues" Grid.Row="1" Columns="4">
          {''.join(_compact_quota(metric, theme) for metric in metrics)}
        </UniformGrid>
        <Border Grid.RowSpan="2" Width="1" Margin="0,2,0,0"
                HorizontalAlignment="Center" Background="{theme['card_border']}"/>
      </Grid>"""


def _mark_for(name):
    """API-key providers are user-configurable, so their mark comes from the name."""
    raw = str(name or "")
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|\d+", raw)
    if len(words) > 1:
        return "".join(word[0] for word in words[:2]).upper()
    letters = [char for char in raw if char.isalnum()]
    return "".join(letters[:2]).upper() if letters else "?"


def _headline(metrics):
    """The one row a compact provider gets: its worst window, else its first fact."""
    scored = [metric for metric in metrics if metric.get("pct") is not None]
    if scored:
        return max(scored, key=lambda metric: metric["pct"])
    return metrics[0]


ANTIGRAVITY_QUOTA_LABELS = {
    "Session": "gemini session",
    "Weekly": "gemini week",
    "Claude": "claude session",
    "Claude Wk": "claude week",
}


def _display_metric(metric, provider_name):
    """Make Antigravity's four independent quota buckets unambiguous in the UI."""
    if provider_name != "Antigravity":
        return metric
    label = ANTIGRAVITY_QUOTA_LABELS.get(str(metric.get("label") or ""))
    if not label:
        return metric
    displayed = dict(metric)
    displayed["label"] = label
    return displayed


def _card(agent, theme, last=False):
    """Render one live provider inside the shared usage surface."""
    raw_name = agent.get("agent") or "Unknown"
    name = "Antigravity" if raw_name == "Antigrav" else raw_name
    mark = PROVIDER_MARKS.get(raw_name) or _mark_for(name)

    metrics = [
        _display_metric(metric, name)
        for metric in (agent.get("metrics") or [])
        if not _is_zero_money_metric(metric) and not _is_inert_metric(metric)
    ]
    # Antigravity owns four independent quota windows.  "Compact" may reduce
    # other API providers to a headline, but it must never hide these four facts.
    if agent.get("compact") and name != "Antigravity" and metrics:
        metrics = [_headline(metrics)]
    icon = _provider_icon_markup(name, theme, mark)
    if name == "Antigravity" and len(metrics) > 1 and all(
        metric.get("pct") is not None for metric in metrics
    ):
        rows = _quota_grid(metrics, theme)
    elif metrics:
        rows = "".join(
            _metric_row(metric, theme, last=index == len(metrics) - 1)
            for index, metric in enumerate(metrics)
        )
    else:
        rows = _metric_row(
            {"label": "", "pct": None, "extra": agent.get("err") or "no data"},
            theme,
            last=True,
        )

    is_antigravity_strip = name == "Antigravity" and 'Tag="QuotaGrid"' in rows
    header_margin = "0" if not rows else "0,0,0,5" if is_antigravity_strip else "0,0,0,7"
    card_padding = (
        f"{CARD_PADDING:.0f},10,{CARD_PADDING:.0f},9"
        if is_antigravity_strip
        else f"{CARD_PADDING:.0f},10,{CARD_PADDING:.0f},10"
    )
    plan = _plan_label(agent.get("plan"))
    if agent.get("_freshness"):
        plan = agent["_freshness"]
    plan_markup = (
        f"""<Border Grid.Column="1" Background="{theme['pill_bg']}" CornerRadius="{CAPSULE_RADIUS:.0f}"
                      Padding="7,1,7,2" Margin="8,0,0,0" VerticalAlignment="Center">
            <TextBlock Text="{_x(plan)}" Foreground="{theme['muted']}" FontSize="9.5"
                       FontWeight="SemiBold" TextTrimming="CharacterEllipsis"/>
          </Border>"""
        if plan
        else ""
    )
    body = f"""
      <StackPanel>
        <Grid Tag="ProviderHeader" Height="{PROVIDER_HEADER_HEIGHT:.0f}" Margin="{header_margin}">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <StackPanel Grid.Column="0" Orientation="Horizontal" VerticalAlignment="Center" ClipToBounds="True">
            {icon}
            <TextBlock Text="{_x(name)}" Foreground="{theme['fg']}" FontSize="13"
                       FontWeight="SemiBold" VerticalAlignment="Center"
                       MaxWidth="{NAME_MAX_WIDTH:.0f}" TextTrimming="CharacterEllipsis"/>
          </StackPanel>
          {plan_markup}
        </Grid>
        {rows}
      </StackPanel>"""
    return _platter(
        "ProviderRow",
        theme,
        body,
        padding=card_padding,
        margin="0" if last else f"0,0,0,{PLATTER_GAP:.0f}",
        tooltip=agent.get("_freshness_detail") or "",
    )


def _rim(theme, top="rim_top", bottom="rim_bot"):
    """Glass edge: lit along the top, falling away towards the bottom."""
    return f"""<LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
          <GradientStop Color="{theme[top]}" Offset="0"/>
          <GradientStop Color="{theme[bottom]}" Offset="1"/>
        </LinearGradientBrush>"""


def _platter(tag, theme, body, padding, margin="0", tooltip=""):
    """One raised pane of glass: a sheen that is brightest along the top, under
    a rim that catches the light there and falls away towards the bottom."""
    tooltip_attr = f' ToolTip="{_x(tooltip)}"' if tooltip else ""
    return f"""
    <Border Tag="{tag}" BorderThickness="{CARD_BORDER:.0f}"
            CornerRadius="{SURFACE_CORNER_RADIUS:.0f}" Padding="{padding}"
            Margin="{margin}"{tooltip_attr}>
      <Border.Background>
        {_rim(theme, "card_hi", "card_bg")}
      </Border.Background>
      <Border.BorderBrush>
        {_rim(theme)}
      </Border.BorderBrush>{body}
    </Border>"""


def _needs_claude_sign_in(agent):
    """Claude Code itself is signed out or its session lapsed: one click fixes it."""
    if agent.get("agent") != "Claude" or agent.get("cached_at"):
        return False
    reason = str(agent.get("err") or "").casefold()
    return any(term in reason for term in ("signed out", "sign-in expired", "access token"))


SIGN_IN_TOOLTIP = (
    "Claude Code is signed out, so Fleet has no Claude usage to read. "
    "Sign in opens a console running claude auth login."
)


def _inactive_state(agent, fleet):
    reason = str(agent.get("err") or agent.get("_inactive_reason") or "not active").strip()
    tooltip = reason
    if agent.get("cached_at"):
        age = fleet._age(agent["cached_at"])
        tooltip = f"{reason} · {age} old"
        label = "cached"
    elif _needs_claude_sign_in(agent):
        label, tooltip = "signed out", SIGN_IN_TOOLTIP
    else:
        lowered = reason.casefold()
        if "access token" in lowered:
            label = "no access"
        elif "credentials" in lowered:
            label = "not connected"
        elif "expired" in lowered:
            label = "expired"
        elif "not running" in lowered or "closed" in lowered:
            label = "not running"
        else:
            label = reason.split(" · ", 1)[0]
    return label, tooltip


def _capsule_button_template(background, hover, rim=None):
    """A pill-shaped glass button; `rim` is (top, bottom) token colours or None."""
    if rim:
        border = f"""BorderThickness="1">
            <Border.BorderBrush>
              <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                <GradientStop Color="{rim[0]}" Offset="0"/>
                <GradientStop Color="{rim[1]}" Offset="1"/>
              </LinearGradientBrush>
            </Border.BorderBrush>"""
    else:
        border = 'BorderThickness="0">'
    return f"""
      <Button.Template>
        <ControlTemplate TargetType="Button">
          <Border x:Name="Chrome" Background="{background}" CornerRadius="{CAPSULE_RADIUS + 2:.0f}"
                  Padding="{{TemplateBinding Padding}}" {border}
            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsMouseOver" Value="True">
              <Setter TargetName="Chrome" Property="Background" Value="{hover}"/>
            </Trigger>
            <Trigger Property="IsPressed" Value="True">
              <Setter TargetName="Chrome" Property="Opacity" Value="0.8"/>
            </Trigger>
            <Trigger Property="IsEnabled" Value="False">
              <Setter TargetName="Chrome" Property="Opacity" Value="0.45"/>
            </Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Button.Template>"""


def _sign_in_button(theme):
    return f"""
        <Button Grid.Column="1" Tag="ClaudeSignIn" Cursor="Hand" Padding="10,2,10,3"
                Margin="10,0,0,0" VerticalAlignment="Center"
                AutomationProperties.Name="Sign in to Claude Code">{_capsule_button_template(theme['accent_soft'], theme['accent_soft_hover'])}
          <TextBlock Text="Sign in" Foreground="{theme['meter_g']}" FontSize="10"
                     FontWeight="SemiBold"/>
        </Button>"""


def _inactive_row(agent, theme, fleet):
    raw_name = agent.get("agent") or "Unknown"
    name = "Antigravity" if raw_name == "Antigrav" else raw_name
    mark = PROVIDER_MARKS.get(raw_name) or _mark_for(name)
    label, tooltip = _inactive_state(agent, fleet)
    icon = _provider_icon_markup(name, theme, mark)
    status = (
        _sign_in_button(theme)
        if _needs_claude_sign_in(agent)
        else f"""
        <TextBlock Grid.Column="1" Text="{_x(label)}" Foreground="{theme['faint']}"
                   FontSize="10" Margin="10,0,0,0" VerticalAlignment="Center"
                   TextTrimming="CharacterEllipsis"/>"""
    )
    return f"""
      <Grid Tag="InactiveProvider" Height="34" Margin="10,0,8,0" ToolTip="{_x(tooltip)}">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <StackPanel Orientation="Horizontal" VerticalAlignment="Center" ClipToBounds="True"
                    Opacity="0.82">
          {icon}
          <TextBlock Text="{_x(name)}" Foreground="{theme['muted']}" FontSize="11.5"
                     FontWeight="Medium" VerticalAlignment="Center"
                     TextTrimming="CharacterEllipsis"/>
        </StackPanel>{status}
      </Grid>"""


# Segoe Fluent Icons ships with Windows 11; MDL2 is the Windows 10 fallback.
ICON_FONT = "Segoe Fluent Icons, Segoe MDL2 Assets"
GLYPH_CHEVRON_DOWN = "&#xE70D;"
GLYPH_CHEVRON_UP = "&#xE70E;"
GLYPH_REFRESH = "&#xE72C;"

# The stock Expander draws a circular chevron button that floats outside the
# surface. Templating it away is the only way to get a flat disclosure row.
EXPANDER_TEMPLATE = """
    <Expander.Template>
      <ControlTemplate TargetType="Expander">
        <StackPanel>
          <ToggleButton Cursor="Hand" Focusable="True"
                        HorizontalContentAlignment="Stretch"
                        IsChecked="{Binding IsExpanded, RelativeSource={RelativeSource TemplatedParent}, Mode=TwoWay}">
            <ToggleButton.Template>
              <ControlTemplate TargetType="ToggleButton">
                <Border Background="Transparent" Padding="2,3" CornerRadius="5">
                  <ContentPresenter HorizontalAlignment="Stretch"/>
                </Border>
              </ControlTemplate>
            </ToggleButton.Template>
            <ContentPresenter ContentSource="Header"/>
          </ToggleButton>
          <ContentPresenter x:Name="ExpandSite" Visibility="Collapsed"/>
        </StackPanel>
        <ControlTemplate.Triggers>
          <Trigger Property="IsExpanded" Value="True">
            <Setter TargetName="ExpandSite" Property="Visibility" Value="Visible"/>
          </Trigger>
        </ControlTemplate.Triggers>
      </ControlTemplate>
    </Expander.Template>"""

CHEVRON_STYLE = """
        <TextBlock.Style>
          <Style TargetType="TextBlock">
            <Setter Property="Text" Value="%s"/>
            <Style.Triggers>
              <DataTrigger Binding="{Binding IsExpanded, RelativeSource={RelativeSource AncestorType=Expander}}" Value="True">
                <Setter Property="Text" Value="%s"/>
              </DataTrigger>
            </Style.Triggers>
          </Style>
        </TextBlock.Style>""" % (GLYPH_CHEVRON_DOWN, GLYPH_CHEVRON_UP)

def _refresh_button_template(theme):
    return _capsule_button_template(
        theme["control_bg"],
        theme["control_hover"],
        rim=(theme["rim_top"], theme["control_border"]),
    )


def _inactive_section(agents, theme, fleet):
    if not agents:
        return ""
    rows = []
    for index, agent in enumerate(agents):
        rows.append(_inactive_row(agent, theme, fleet))
        if index < len(agents) - 1:
            rows.append(
                f'<Border Height="1" Margin="41,0,10,0" Background="{theme["card_border"]}"/>'
            )
    count = len(agents)
    surface = _platter(
        "InactiveSurface", theme, f"<StackPanel>{''.join(rows)}</StackPanel>",
        padding="0", margin="0,5,0,0",
    )
    return f"""
  <Expander Tag="InactiveProviders" IsExpanded="False" Margin="0,8,0,0"
            HorizontalContentAlignment="Stretch" Foreground="{theme['faint']}">{EXPANDER_TEMPLATE}
    <Expander.Header>
      <Grid HorizontalAlignment="Stretch" Margin="2,0,4,0">
        <TextBlock Text="{count} inactive" Foreground="{theme['muted']}" FontSize="10.5"
                   FontWeight="Medium" VerticalAlignment="Center"/>
        <TextBlock HorizontalAlignment="Right" VerticalAlignment="Center"
                   FontFamily="{ICON_FONT}" FontSize="8"
                   Foreground="{theme['muted']}">{CHEVRON_STYLE}
        </TextBlock>
      </Grid>
    </Expander.Header>{surface}
  </Expander>"""


def _content_xaml(data, ts, light, fleet, acrylic=True):
    """The panel's inner content, parseable on its own.

    Kept separate from the window shell so refreshed data can be swapped into a
    live window. Rebuilding the whole Window instead costs a fresh HWND every
    time, which is the bulk of the open/close cost.
    """
    import time

    live, inactive = _partition_agents(data)
    theme = _theme(light, acrylic=acrylic)
    rows = []
    for index, agent in enumerate(live):
        copied = dict(agent)
        source = agent.get("source")
        if source and not agent.get("err"):
            # A named local source is a real reading on its own sampling clock,
            # not a Fleet cache, and saying "cached" about it would be a lie.
            age = fleet._age(agent.get("cached_at") or ts)
            copied["_freshness"] = f"{source} · {age} old"
            copied["_freshness_detail"] = f"read from the {source} app · {age} old"
        elif agent.get("err") or agent.get("cached_at"):
            age = fleet._age(agent.get("cached_at") or ts)
            copied["_freshness"] = f"cached · {age} old"
            copied["_freshness_detail"] = f"{agent.get('err') or 'cached reading'} · {age} old"
        copied["metrics"] = []
        for metric in agent.get("metrics") or []:
            item = dict(metric)
            item["_reset_text"] = fleet._until(item["resets"]) if item.get("resets") else ""
            copied["metrics"].append(item)
        rows.append(_card(copied, theme, last=index == len(live) - 1))

    if rows:
        # No shared slab any more: each provider is its own pane of glass, and the
        # gaps between them are where the desktop blur shows through.
        live_surface = f"""
  <StackPanel Tag="LiveSurface">{''.join(rows)}</StackPanel>"""
    else:
        live_surface = _platter(
            "EmptySurface",
            theme,
            f"""
      <TextBlock Text="no live usage" Foreground="{theme['muted']}" FontSize="11"
                 HorizontalAlignment="Center"/>""",
            padding="10,14",
        )

    status_text, status_color = _freshness_status(live, ts)
    inactive_section = _inactive_section(inactive, theme, fleet)
    return f"""
<StackPanel xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
            xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
            x:Name="ContentRoot" Opacity="1">
  <Grid Height="24" Margin="4,0,0,8">
    <TextBlock Text="fleet" Foreground="{theme['fg']}" FontSize="14"
               FontWeight="SemiBold" VerticalAlignment="Center"/>
    <Border Tag="StatusCapsule" HorizontalAlignment="Right" VerticalAlignment="Center"
            Background="{theme['pill_bg']}" CornerRadius="{CAPSULE_RADIUS:.0f}" Padding="8,2,9,3">
      <StackPanel Orientation="Horizontal">
        <Ellipse Width="6" Height="6" Fill="{status_color}" Margin="0,1,6,0"
                 VerticalAlignment="Center"/>
        <TextBlock Text="{_x(status_text)}" Foreground="{theme['muted']}"
                   FontSize="10" FontWeight="SemiBold" Typography.NumeralAlignment="Tabular"
                   VerticalAlignment="Center"/>
      </StackPanel>
    </Border>
  </Grid>
  {live_surface}
  {inactive_section}
  <Grid Margin="4,10,0,0" Height="26">
    <TextBlock Text="updates every 2 min" Foreground="{theme['faint']}" FontSize="10"
               VerticalAlignment="Center"/>
    <Button x:Name="RefreshButton" HorizontalAlignment="Right" Cursor="Hand"
            Foreground="{theme['fg']}" FontSize="10.5" Padding="11,3,13,4"
            AutomationProperties.Name="Refresh usage now">{_refresh_button_template(theme)}
      <StackPanel Orientation="Horizontal">
        <TextBlock Text="{GLYPH_REFRESH}" FontFamily="{ICON_FONT}" FontSize="10"
                   Margin="0,1,6,0" VerticalAlignment="Center"/>
        <TextBlock Text="Refresh" FontWeight="SemiBold" VerticalAlignment="Center"/>
      </StackPanel>
    </Button>
  </Grid>
</StackPanel>"""


def _freshness_status(providers, ts, now=None):
    now = time.time() if now is None else now
    age = max(0, now - ts)
    if not providers:
        return "no readings", GRAD["n"]
    if age > 300:
        label = f"{int(age / 3600)}h" if age >= 3600 else f"{int(age / 60)}m"
        return f"updated {label} ago", GRAD["y"]
    fresh = sum(not p.get("err") and not p.get("cached_at") for p in providers)
    clock = time.strftime("%H:%M", time.localtime(ts))
    if fresh != len(providers):
        return f"{fresh} live · {len(providers) - fresh} cached", GRAD["y"]
    return f"{fresh} live · {clock}", GRAD["g"]


def _xaml(data, ts, light, fleet, acrylic=True):
    """The window shell with its first content already inside."""
    theme = _theme(light, acrylic=acrylic)
    content = _content_xaml(data, ts, light, fleet, acrylic=acrylic)
    # The fragment carries its own namespace declarations; strip them so it nests
    # cleanly inside the window.
    content = content.replace(
        '<StackPanel xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"\n'
        '            xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"\n'
        '            x:Name="ContentRoot"',
        '<StackPanel x:Name="ContentRoot"',
    )
    # Transparent window brush + AllowsTransparency=False is intentional: DWM
    # system acrylic only attaches to non-layered windows. HwndSource clears the
    # composition target after SourceInitialized.
    window_bg = "Transparent" if acrylic else theme["surface"]
    return f"""
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Width="{PANEL_WIDTH:.0f}" SizeToContent="Height" WindowStyle="None"
        ResizeMode="NoResize" AllowsTransparency="False"
        Background="{window_bg}" ShowInTaskbar="False"
        Topmost="True" Opacity="0" FontFamily="Segoe UI Variable Text"
        TextOptions.TextFormattingMode="Display" SnapsToDevicePixels="True"
        UseLayoutRounding="True">
  <Border x:Name="GlassClip" CornerRadius="{PANEL_CORNER_RADIUS:.0f}" BorderThickness="1"
          Padding="{WINDOW_PADDING:.0f}" Opacity="1" RenderTransformOrigin="0.5,1">
    <Border.BorderBrush>
      <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
        <GradientStop Color="{theme['border_top']}" Offset="0.0"/>
        <GradientStop Color="{theme['border_bot']}" Offset="1.0"/>
      </LinearGradientBrush>
    </Border.BorderBrush>
    <Border.Background>
      <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
        <GradientStop Color="{theme['surface_hi']}" Offset="0.0"/>
        <GradientStop Color="{theme['surface']}" Offset="0.35"/>
        <GradientStop Color="{theme['surface']}" Offset="1.0"/>
      </LinearGradientBrush>
    </Border.Background>
    <Border.RenderTransform>
      <TranslateTransform x:Name="SlideTransform" Y="0"/>
    </Border.RenderTransform>
    {content}
  </Border>
</Window>"""


def show_panel(data, ts, light, fleet):
    _load_wpf()
    from System.Threading import ApartmentState, Thread, ThreadStart

    failure = []

    def run():
        try:
            _show_panel_sta(data, ts, light, fleet)
        except Exception as exc:
            failure.append(exc)

    thread = Thread(ThreadStart(run))
    thread.SetApartmentState(ApartmentState.STA)
    thread.Start()
    thread.Join()
    if failure:
        raise failure[0]


def _show_panel_sta(data, ts, light, fleet):
    from System.Windows import Application

    app = Application()
    window, _, _ = _create_window(data, ts, light, fleet)
    app.Run(window)


def run_panel_host(state_loader, light_loader, fleet, signal_path, quit_path):
    """Keep WPF warm and create a fresh flyout whenever the tray signals."""
    _load_wpf()
    from System.Threading import ApartmentState, Thread, ThreadStart

    failure = []

    def run():
        try:
            _run_panel_host_sta(state_loader, light_loader, fleet, signal_path, quit_path)
        except Exception as exc:
            failure.append(exc)

    thread = Thread(ThreadStart(run))
    thread.SetApartmentState(ApartmentState.STA)
    thread.Start()
    thread.Join()
    if failure:
        raise failure[0]


def _run_panel_host_sta(state_loader, light_loader, fleet, signal_path, quit_path):
    from System import TimeSpan
    from System.Windows import Application, ShutdownMode
    from System.Windows.Threading import DispatcherTimer

    app = Application()
    app.ShutdownMode = ShutdownMode.OnExplicitShutdown
    # One window for the host's lifetime. It is hidden between opens, never
    # closed, because rebuilding the HWND costs ~37 ms against ~6 ms to re-show.
    current = {"window": None, "present": None, "swap": None, "ts": None, "light": None}
    last_signal = {"mtime": 0}
    refresh_check = {"at": 0.0}
    heartbeat_at = {"at": 0.0}
    stopping = {"value": False}

    def build():
        data, ts = state_loader()
        light = light_loader()
        window, present, swap = _create_window(data, ts, light, fleet, auto_present=False)
        current.update(
            {"window": window, "present": present, "swap": swap, "ts": ts, "light": light}
        )
        # Realise the native HWND and composition target at zero opacity up front,
        # so the first click pays neither.
        window.ShowActivated = False
        window.Show()
        window.Hide()
        window.ShowActivated = True
        return window

    def sync(data, ts, light):
        """Bring the existing window up to date without recreating it."""
        if light != current["light"]:
            # A theme flip changes the window's own brushes, so it has to be rebuilt.
            current["window"].Close()
            build()
            return
        current["swap"](data, ts)
        current["ts"] = ts

    def present(anchor_rect=None):
        window = current["window"]
        if window is None:
            window = build()
        if window.IsVisible:
            current["present"](anchor_rect)
            return
        data, ts = state_loader()
        # Even unchanged readings age while the panel is hidden.
        sync(data, ts, light_loader())
        current["present"](anchor_rect)

    # Pay XAML parsing/JIT cost invisibly so the first user click is fast.
    build()

    timer = DispatcherTimer()
    # This is the click-to-open latency floor: the tray writes a signal file and
    # the host only notices on the next tick.
    timer.Interval = TimeSpan.FromMilliseconds(12)

    def tick(sender, event):
        if quit_path.exists():
            try:
                quit_path.unlink()
            except OSError:
                pass
            stopping["value"] = True
            timer.Stop()
            app.Shutdown()
            return
        try:
            changed = signal_path.stat().st_mtime_ns
        except OSError:
            changed = 0
        if changed > last_signal["mtime"]:
            last_signal["mtime"] = changed
            anchor_rect = None
            try:
                payload = json.loads(signal_path.read_text())
                anchor = payload.get("anchor")
                if isinstance(anchor, list) and len(anchor) == 4:
                    anchor_rect = tuple(float(value) for value in anchor)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            present(anchor_rect)
        now = time.monotonic()
        if now - heartbeat_at["at"] >= 10:
            heartbeat = signal_path.with_name("panel-heartbeat.json")
            heartbeat.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
            heartbeat_at["at"] = now
        window = current["window"]
        if window is None:
            if not stopping["value"]:
                build()
            return
        # State swaps are local and cheap; keep an open panel current too.
        if now - refresh_check["at"] >= 15:
            refresh_check["at"] = now
            data, ts = state_loader()
            light = light_loader()
            if window.IsVisible or current["ts"] != ts or current["light"] != light:
                sync(data, ts, light)

    timer.Tick += tick
    timer.Start()
    app.Run()


def _create_window(data, ts, light, fleet, auto_present=True):
    from System import TimeSpan
    from System.Windows import (
        Duration,
        LogicalTreeHelper,
        RoutedEventHandler,
        SystemParameters,
        UIElement,
    )
    from System.Windows.Controls import Button, Expander
    from System.Windows.Interop import HwndSource, WindowInteropHelper
    from System.Windows.Markup import XamlReader
    from System.Windows.Media import Colors, SolidColorBrush, VisualTreeHelper
    from System.Windows.Media.Animation import (
        CubicEase,
        DoubleAnimation,
        EasingMode,
        FillBehavior,
    )
    from System.Windows.Media import TranslateTransform
    from System.Windows.Threading import DispatcherTimer

    want_acrylic = _material() != "opaque"
    theme = _theme(light, acrylic=want_acrylic)
    window = XamlReader.Parse(_xaml(data, ts, light, fleet, acrylic=want_acrylic))
    glass_clip = window.FindName("GlassClip")
    slide_transform = window.FindName("SlideTransform")
    if slide_transform is None:
        slide_transform = TranslateTransform()
        glass_clip.RenderTransform = slide_transform
    chrome = {"acrylic": False}

    def content_controls(root):
        controls = []
        stack = [root]
        while stack:
            node = stack.pop()
            if isinstance(node, (Button, Expander)):
                controls.append(node)
            if isinstance(node, UIElement):
                stack.extend(child for child in LogicalTreeHelper.GetChildren(node)
                             if isinstance(child, UIElement))
        return controls

    def request_refresh(sender, event):
        (fleet.CACHE / "refresh.request").write_text(str(time.time_ns()))
        sender.Content = "refreshing…"
        sender.IsEnabled = False

    def sign_in(sender, event):
        # The console owns the sign-in; once it closes, ask the tray for a fresh
        # poll so Claude moves into the live list without waiting two minutes.
        launch = getattr(fleet, "claude_sign_in", None)
        started = launch is not None and launch(
            on_exit=lambda: (fleet.CACHE / "refresh.request").write_text(str(time.time_ns()))
        )
        if started:
            sender.IsEnabled = False
            close_animated()
        else:
            sender.ToolTip = "Claude Code is not installed"

    def route_click(sender, event):
        button = event.Source
        if isinstance(button, Button) and button.Name == "RefreshButton":
            request_refresh(button, event)
        elif isinstance(button, Button) and str(button.Tag) == "ClaudeSignIn":
            sign_in(button, event)

    # One routed handler on the long-lived GlassClip serves every swapped-in
    # fragment. pythonnet's `button.Click += handler` keeps the button, and
    # through its parents the whole fragment, alive until a matching `-=`, so
    # wiring each fragment's buttons pinned every one a swap replaced: about
    # 175 KB per state change, ~130 MB a day for a host that runs for days.
    glass_clip.AddHandler(Button.ClickEvent, RoutedEventHandler(route_click))

    def swap_content(new_data, new_ts):
        """Replace the panel's contents without touching the window or its HWND.

        Creating a window costs ~37 ms of HWND and composition-target setup, and
        the old code paid that on every dismiss because it closed the window
        instead of hiding it. Parsing this fragment costs a few ms.
        """
        expanded = {str(control.Tag): bool(control.IsExpanded)
                    for control in content_controls(glass_clip.Child) if isinstance(control, Expander)}
        fragment = XamlReader.Parse(
            _content_xaml(new_data, new_ts, light, fleet, acrylic=chrome["acrylic"])
        )
        glass_clip.Child = fragment
        for control in content_controls(fragment):
            if isinstance(control, Expander) and str(control.Tag) in expanded:
                control.IsExpanded = expanded[str(control.Tag)]
        window.UpdateLayout()
        if window.IsVisible:
            position(anchor["rect"])

    # No explicit rounded clip: DWM already clips the HWND to its rounded shape.
    # Per-pixel corner clips on a non-layered window paint black in the discarded
    # samples, which is why the earlier clip attempts looked wrong.
    native = {"hwnd": 0}

    # WS_EX_TOOLWINDOW keeps this flyout out of Alt+Tab as well as the taskbar.
    def source_ready(sender, event):
        helper = WindowInteropHelper(window)
        hwnd = int(helper.Handle.ToInt64())
        get_style = ctypes.windll.user32.GetWindowLongW
        set_style = ctypes.windll.user32.SetWindowLongW
        exstyle = get_style(hwnd, -20)
        set_style(hwnd, -20, exstyle | 0x00000080)
        native["hwnd"] = hwnd
        acrylic = _apply_window_chrome(hwnd, light)
        chrome["acrylic"] = acrylic
        source = HwndSource.FromHwnd(helper.Handle)
        if acrylic and source is not None and source.CompositionTarget is not None:
            # Non-layered transparent client: DWM acrylic shows through wherever
            # WPF leaves alpha, without WS_EX_LAYERED (which system backdrops refuse).
            source.CompositionTarget.BackgroundColor = Colors.Transparent
            window.Background = SolidColorBrush(Colors.Transparent)
        elif source is not None and source.CompositionTarget is not None:
            # Acrylic refused: fall back to a solid panel so we never flash a
            # see-through unreadable surface.
            opaque = _theme(light, acrylic=False)
            from System.Windows.Media import Color as WpfColor

            def brush(hex_color):
                value = hex_color.lstrip("#")
                a = int(value[0:2], 16)
                r = int(value[2:4], 16)
                g = int(value[4:6], 16)
                b = int(value[6:8], 16)
                return SolidColorBrush(WpfColor.FromArgb(a, r, g, b))

            window.Background = brush(opaque["surface"])
            glass_clip.Background = brush(opaque["surface"])
            glass_clip.BorderBrush = brush(opaque["border_top"])
            source.CompositionTarget.BackgroundColor = WpfColor.FromArgb(
                255,
                int(opaque["surface"][3:5], 16),
                int(opaque["surface"][5:7], 16),
                int(opaque["surface"][7:9], 16),
            )

    window.SourceInitialized += source_ready

    def light_backdrop():
        if chrome["acrylic"]:
            _light_backdrop(native["hwnd"])

    def position(anchor_rect=None):
        point = ctypes.wintypes.POINT()
        if anchor_rect:
            point.x = int((anchor_rect[0] + anchor_rect[2]) / 2)
            point.y = int((anchor_rect[1] + anchor_rect[3]) / 2)
        else:
            ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        work_rect = _monitor_work_area(point)
        if work_rect is None:
            work = SystemParameters.WorkArea
            work_rect = (work.Left, work.Top, work.Right, work.Bottom)
            dpi_scale = (1.0, 1.0)
            cursor = (point.x, point.y)
        else:
            dpi = VisualTreeHelper.GetDpi(window)
            dpi_scale = (dpi.DpiScaleX, dpi.DpiScaleY)
            cursor = (point.x, point.y)
        if anchor_rect:
            window.Left, window.Top = _anchored_panel_origin(
                work_rect,
                anchor_rect,
                (window.ActualWidth, window.ActualHeight),
                dpi_scale,
            )
        else:
            window.Left, window.Top = _panel_origin(
                work_rect,
                cursor,
                (window.ActualWidth, window.ActualHeight),
                dpi_scale,
            )

    anchor = {"rect": None}
    closing = {"value": False}
    animations = {"open_y": None, "open_o": None, "close_y": None, "close_o": None}

    def keep_anchor_on_resize(sender, event):
        # SizeToContent grows upward from the tray when a quota disclosure opens.
        # A missing tray rect makes position() fall back to the cursor. During an
        # expander click that would move the flyout underneath the pointer.
        if window.IsVisible and anchor["rect"] is not None:
            position(anchor["rect"])

    window.SizeChanged += keep_anchor_on_resize

    def ease(mode):
        curve = CubicEase()
        curve.EasingMode = mode
        return curve

    def clear_motion():
        slide_transform.BeginAnimation(TranslateTransform.YProperty, None)
        glass_clip.BeginAnimation(UIElement.OpacityProperty, None)
        slide_transform.Y = 0.0
        glass_clip.Opacity = 1.0

    def prepare_open():
        """Park content below + faded while the HWND is still hidden."""
        closing["value"] = False
        clear_motion()
        if SystemParameters.ClientAreaAnimation:
            slide_transform.Y = SLIDE_DISTANCE
            glass_clip.Opacity = 0.0
        window.Opacity = 1.0

    def begin_open_slide():
        """GPU-friendly content motion. Window stays put — no Top animation."""
        closing["value"] = False
        light_backdrop()
        if not SystemParameters.ClientAreaAnimation:
            clear_motion()
            window.Opacity = 1.0
            return
        start_y = float(slide_transform.Y)
        start_o = float(glass_clip.Opacity)
        if start_y <= 0.01 and start_o >= 0.99:
            clear_motion()
            window.Opacity = 1.0
            return
        fraction = max(0.1, min(1.0, start_y / SLIDE_DISTANCE)) if SLIDE_DISTANCE > 0 else 1.0
        duration_ms = max(30, int(OPEN_MS * fraction))
        y_anim = DoubleAnimation(
            start_y,
            0.0,
            Duration(TimeSpan.FromMilliseconds(duration_ms)),
        )
        y_anim.EasingFunction = ease(EasingMode.EaseOut)
        y_anim.FillBehavior = FillBehavior.HoldEnd
        o_anim = DoubleAnimation(
            start_o,
            1.0,
            Duration(TimeSpan.FromMilliseconds(duration_ms)),
        )
        o_anim.EasingFunction = ease(EasingMode.EaseOut)
        o_anim.FillBehavior = FillBehavior.HoldEnd
        animations["open_y"] = y_anim
        animations["open_o"] = o_anim
        slide_transform.BeginAnimation(TranslateTransform.YProperty, y_anim)
        glass_clip.BeginAnimation(UIElement.OpacityProperty, o_anim)

    if auto_present:
        def auto_open(sender, event):
            position()
            window.UpdateLayout()
            prepare_open()
            window.Show()
            begin_open_slide()
            window.Activate()

        window.ContentRendered += auto_open

    def dismiss():
        """Hide, never close.

        Closing destroys the HWND and composition target, so the next open has to
        rebuild both -- about 37 ms versus 6 ms to re-show the same window. That
        was most of what made repeated opens feel sluggish.
        """
        clear_motion()
        window.Hide()
        window.Opacity = 0.0
        closing["value"] = False

    def close_animated(*_):
        if not window.IsVisible or closing["value"]:
            return
        closing["value"] = True
        if not SystemParameters.ClientAreaAnimation:
            dismiss()
            return
        start_y = float(slide_transform.Y)
        start_o = float(glass_clip.Opacity)
        fraction = max(0.1, min(1.0, (SLIDE_DISTANCE - start_y) / SLIDE_DISTANCE)) if SLIDE_DISTANCE > 0 else 1.0
        duration_ms = max(30, int(CLOSE_MS * fraction))
        y_anim = DoubleAnimation(
            start_y,
            SLIDE_DISTANCE,
            Duration(TimeSpan.FromMilliseconds(duration_ms)),
        )
        y_anim.EasingFunction = ease(EasingMode.EaseIn)
        y_anim.FillBehavior = FillBehavior.HoldEnd
        o_anim = DoubleAnimation(
            start_o,
            0.0,
            Duration(TimeSpan.FromMilliseconds(duration_ms)),
        )
        o_anim.EasingFunction = ease(EasingMode.EaseIn)
        o_anim.FillBehavior = FillBehavior.HoldEnd
        o_anim.Completed += lambda sender, event: dismiss()
        animations["close_y"] = y_anim
        animations["close_o"] = o_anim
        slide_transform.BeginAnimation(TranslateTransform.YProperty, y_anim)
        glass_clip.BeginAnimation(UIElement.OpacityProperty, o_anim)

    if not os.environ.get("FLEET_PANEL_STAY"):
        window.KeyDown += lambda sender, event: close_animated() if str(event.Key) == "Escape" else None

        # Native flyouts are light-dismiss surfaces. A short delay avoids the
        # activation handoff flicker that occurs while the tray click completes.
        deactivate = DispatcherTimer()
        deactivate.Interval = TimeSpan.FromMilliseconds(90)

        def finish_deactivation(sender, event):
            deactivate.Stop()
            if not window.IsActive and window.IsVisible:
                close_animated()

        deactivate.Tick += finish_deactivation
        # Losing focus would flatten the glass for the whole fade-out; keep it
        # lit. Deactivation arrives inside a system-sent activation message, so
        # the re-light waits for its own dispatcher frame (see take_foreground).
        window.Deactivated += lambda sender, event: (
            _defer_to_dispatcher(window.Dispatcher, light_backdrop),
            deactivate.Stop(),
            deactivate.Start(),
        )
        window.Activated += lambda sender, event: deactivate.Stop()

        # Also watch every mouse button so clicks on non-activating surfaces
        # dismiss immediately while still passing through to the clicked app.
        mouse = DispatcherTimer()
        mouse.Interval = TimeSpan.FromMilliseconds(16)
        mouse_buttons = (0x01, 0x02, 0x04, 0x05, 0x06)
        button = {
            key: bool(ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000)
            for key in mouse_buttons
        }

        def watch_outside_click(sender, event):
            pressed = False
            for key in mouse_buttons:
                state = ctypes.windll.user32.GetAsyncKeyState(key)
                down = bool(state & 0x8000)
                pressed = pressed or bool(state & 0x0001) or (
                    down and not button[key]
                )
                button[key] = down
            if not pressed or not window.IsVisible:
                return
            hwnd = int(WindowInteropHelper(window).Handle.ToInt64())
            rect = ctypes.wintypes.RECT()
            point = ctypes.wintypes.POINT()
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return
            ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
            if _should_dismiss(
                (rect.left, rect.top, rect.right, rect.bottom),
                (point.x, point.y),
                anchor["rect"],
            ):
                close_animated()

        def stop_mouse_watch(sender, event):
            mouse.Stop()
            deactivate.Stop()

        mouse.Tick += watch_outside_click
        window.Closed += stop_mouse_watch
        # The window now outlives each dismissal, so the watchers run only while
        # it is actually on screen instead of for the host's whole lifetime.
        window.IsVisibleChanged += lambda sender, event: (
            mouse.Start() if window.IsVisible else (mouse.Stop(), deactivate.Stop())
        )
    def present(anchor_rect=None):
        anchor["rect"] = anchor_rect
        if window.IsVisible:
            if closing["value"]:
                begin_open_slide()
            else:
                close_animated()
            return
        return _present_prepared_window(
            window,
            anchor_rect,
            position,
            prepare_open,
            begin_open_slide,
        )

    return window, present, swap_content
