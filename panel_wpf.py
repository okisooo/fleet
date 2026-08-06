"""Native WPF flyout renderer for Fleet.

The warm host keeps one WPF window ready between clicks.\nWin11 system acrylic (DWMWA_SYSTEMBACKDROP_TYPE) is preferred; motion is a\ncontent TranslateTransform + opacity fade, never a Window.Top move.
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
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_ROUND = 2
# Transient-window acrylic is the official Win11 flyout material (menus, popups).
DWMSBT_TRANSIENTWINDOW = 3
MONITOR_DEFAULTTONEAREST = 2
# Content-space motion only. Animating Window.Top forces a full DWM window move
# every frame and is what made the old slide feel capped around 30 fps.
SLIDE_DISTANCE = 12.0
OPEN_MS = 180
CLOSE_MS = 140
PANEL_EDGE_GAP = 12.0
PANEL_ANCHOR_GAP = 10.0
# Must match what DWM actually rounds the window to. DWMWCP_ROUND is the Windows 11
# standard 8 px for a flyout of this size. Drawing the rim at a different radius put
# three curves on screen at once, which is what made the corners look mislayered.
PANEL_CORNER_RADIUS = 10.0
PANEL_WIDTH = 320.0
# Real chrome: GlassClip padding, then a quiet card surface with room for data.
WINDOW_PADDING = 10.0
CARD_PADDING = 10.0
CARD_BORDER = 1.0
PANEL_PADDING = WINDOW_PADDING
PANEL_BORDER = 1.0
ROW_HEIGHT = 27.0
AMOUNT_ROW_HEIGHT = 22.0
PROVIDER_HEADER_HEIGHT = 20.0
BAR_HEIGHT = 6.0
VALUE_COLUMN = 64.0
CONTENT_WIDTH = (
    PANEL_WIDTH
    - 2 * WINDOW_PADDING
    - 2 * CARD_BORDER
    - 2 * CARD_PADDING
)
BAR_TRACK = CONTENT_WIDTH
NAME_MAX_WIDTH = CONTENT_WIDTH - 28.0


def _bar_fill(pct):
    """Bar width in DIPs. Zero stays empty rather than showing a stub."""
    if pct <= 0.0:
        return 0.0
    return max(3.0, BAR_TRACK * pct / 100.0)
PROVIDER_MARKS = {"Claude": "C", "Codex": "X", "Grok": "G", "Antigrav": "A"}
PROVIDER_ACCENTS = {"Claude": "#FFF59E0B", "Codex": "#FF22D3EE", "Grok": "#FFD946EF", "Antigrav": "#FF22C55E"}
# Fallback palette for API-key and custom providers, which Fleet cannot know ahead of time.
API_ACCENTS = [
    "#FF60A5FA", "#FF34D399", "#FFFBBF24", "#FFA78BFA",
    "#FFF472B6", "#FF38BDF8", "#FFFB923C", "#FF94A3B8",
]
GRAD = {"g": "#FF22C55E", "y": "#FFF59E0B", "r": "#FFEF4444", "n": "#FF64748B"}
# Quotas use one calm base colour; amber and coral are reserved for pressure.
# Freshness remains green/amber/red through GRAD, so state and consumption never
# compete for the same visual meaning.
METER = {"g": "#FF4F7CFF", "y": "#FFD89A2B", "r": "#FFE05252", "n": "#FF64748B"}
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


def _present_prepared_window(window, anchor_rect, position, prepare_open, begin_slide):
    """Show the warm flyout at its final rect, then play content-space motion."""
    position(anchor_rect)
    window.UpdateLayout()
    prepare_open()
    window.Show()
    begin_slide()
    window.Activate()


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


def _force_opaque():
    """Local escape hatch: FLEET_OPAQUE=1 keeps the solid panel."""
    return os.environ.get("FLEET_OPAQUE", "").strip().lower() in {"1", "true", "yes"}


def _apply_window_chrome(hwnd, light):
    """Round corners, match theme, and request official Win11 flyout acrylic.

    System backdrops refuse layered windows, so the panel keeps
    AllowsTransparency=False and clears the client through HwndSource instead of
    the old ACCENT_ENABLE_ACRYLICBLURBEHIND path (broken on 26xxx+).
    """
    _set_dwm_int(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 0 if light else 1)
    _set_dwm_int(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    if _force_opaque():
        return False
    hr = _set_dwm_int(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW)
    if hr != 0:
        return False
    margins = _MARGINS(-1, -1, -1, -1)
    extend = ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    return extend == 0


def _theme(light, acrylic=True):
    """macOS Liquid Glass theme tokens.

    Foreground colours stay fully opaque so text contrast never rides the desktop.
    Surface, glass card pods, and specular rims use subtle transparency and
    gradients.
    """
    if light:
        if acrylic:
            return {
                "surface": "#D9F8FAFC",
                "border_top": "#E0FFFFFF",
                "border_bot": "#40D8DEE9",
                "card_top": "#D6FFFFFF",
                "card_bg": "#A8FFFFFF",
                "card_border": "#78FFFFFF",
                "icon_bg": "#15000000",
                "fg": "#FF0F172A",
                "muted": "#FF475569",
                "faint": "#FF64748B",
                "track": "#240F172A",
                "pill_bg": "#FFF1F5F9",
                "chip": "#FFF7F9FC",
            }
        return {
            "surface": "#FFF7F9FC",
            "border_top": "#FFD8DEE9",
            "border_bot": "#FFD8DEE9",
            "card_top": "#FFFFFFFF",
            "card_bg": "#FFFFFFFF",
            "card_border": "#FFE2E8F0",
            "icon_bg": "#FFE2E8F0",
            "fg": "#FF0F172A",
            "muted": "#FF475569",
            "faint": "#FF64748B",
            "track": "#FFE2E8F0",
            "pill_bg": "#FFF1F5F9",
            "chip": "#FFF7F9FC",
        }
    if acrylic:
        return {
            "surface": "#CC12161F",
            "border_top": "#50FFFFFF",
            "border_bot": "#15FFFFFF",
            "card_top": "#24FFFFFF",
            "card_bg": "#14FFFFFF",
            "card_border": "#20FFFFFF",
            "icon_bg": "#20FFFFFF",
            "fg": "#FFF0F4F8",
            "muted": "#FF94A3B8",
            "faint": "#FF64748B",
            "track": "#25FFFFFF",
            "pill_bg": "#FF1E2530",
            "chip": "#FF1A1E25",
        }
    return {
        "surface": "#FF181C24",
        "border_top": "#FF313842",
        "border_bot": "#FF313842",
        "card_top": "#FF282E39",
        "card_bg": "#FF222732",
        "card_border": "#FF2D3442",
        "icon_bg": "#FF2D3442",
        "fg": "#FFF0F4F8",
        "muted": "#FF94A3B8",
        "faint": "#FF64748B",
        "track": "#FF2D3442",
        "pill_bg": "#FF2D3442",
        "chip": "#FF1A1E25",
    }


def _plan_label(value):
    raw = str(value or "").strip()
    known = {
        "pro": "Pro",
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


def _visible_agents(data):
    """Copy provider data while removing empty zero-money-only cards."""
    visible = []
    for agent in data:
        copied = dict(agent)
        copied["metrics"] = [
            metric for metric in (agent.get("metrics") or [])
            if not _is_zero_money_metric(metric)
        ]
        if copied["metrics"] or copied.get("err"):
            visible.append(copied)
    return visible



def _provider_icon_markup(raw_name, theme, mark, accent, status_text="", status_color=None):
    name = raw_name.casefold()
    mapped = {
        "claude": "claude.png",
        "codex": "codex.png",
        "grok": "grok.ico",
        "antigrav": "antigravity.png",
        "antigravity": "antigravity.png",
    }.get(name, f"{name}.png")

    icon_file = ASSETS_DIR / mapped
    status_dot = f"""<Ellipse Width="6" Height="6" Fill="{status_color or GRAD['n']}"
                              Stroke="{theme['chip']}" StrokeThickness="1.2"
                              HorizontalAlignment="Right" VerticalAlignment="Bottom"
                              Margin="0,0,-1,-1" ToolTip="{_x(status_text)}"/>"""

    if icon_file.exists():
        uri = _x(icon_file.absolute().as_uri())
        return f"""
            <Grid Width="18" Height="18" Margin="0,0,8,0" VerticalAlignment="Center">
              <Border CornerRadius="5" Background="{theme['icon_bg']}" ClipToBounds="True">
                <Image Source="{uri}" Width="15" Height="15" HorizontalAlignment="Center" VerticalAlignment="Center" RenderOptions.BitmapScalingMode="HighQuality"/>
              </Border>
              {status_dot}
            </Grid>"""

    return f"""
            <Grid Width="18" Height="18" Margin="0,0,8,0" VerticalAlignment="Center">
              <Border Background="{accent or theme['track']}" CornerRadius="5"/>
              <TextBlock Text="{_x(mark or '?')}" Foreground="{theme['chip']}" FontSize="9.5"
                         FontWeight="Bold" HorizontalAlignment="Center" VerticalAlignment="Center"/>
              {status_dot}
            </Grid>"""


def _metric_row(
    metric,
    theme,
    last=False,
):
    """A quota visual: metadata above a full-width meter, or one amount line."""
    label_raw = _short_label(metric.get("label")) or "status"
    label = _x(label_raw)
    pct = metric.get("pct")
    extra = (metric.get("extra") or "").strip()
    reset = _x(metric.get("_reset_text") or "")
    margin = "0" if last else "0,0,0,5"
    tooltip = _x(metric.get("_tooltip") or "")
    tooltip_attr = f' ToolTip="{tooltip}"' if tooltip else ""

    # Pure amount rows (balance/credits/extra): no meter.
    if pct is None:
        value = _x(extra or "\u2014")
        return f"""
        <Grid Tag="MetricAmount" Height="{AMOUNT_ROW_HEIGHT:.0f}" Margin="{margin}"{tooltip_attr}>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <TextBlock Text="{label}" Foreground="{theme['faint']}"
                     FontSize="10.5" FontWeight="Medium" VerticalAlignment="Center"
                     TextTrimming="CharacterEllipsis"/>
          <TextBlock Grid.Column="1" Text="{value}"
                     Foreground="{theme['fg']}" FontSize="12" FontWeight="SemiBold"
                     Margin="12,0,0,0" TextAlignment="Right"
                     Typography.NumeralAlignment="Tabular" VerticalAlignment="Center"/>
        </Grid>"""

    pct = max(0.0, min(100.0, float(pct)))
    fill_color = METER[_sevkey(pct)]
    # Only currency extras replace the percentage. Count extras like 310/4000 cr
    # remain percentage-first so the usage visual stays comparable.
    if extra and _looks_like_money(extra):
        value = _x(extra)
    else:
        value = f"{pct:.0f}%"
    reset_block = (
        f"""<TextBlock Text="· {reset}" Foreground="{theme['faint']}" FontSize="9.5"
                         Margin="6,0,0,0" VerticalAlignment="Center"
                         Typography.NumeralAlignment="Tabular"/>"""
        if reset
        else ""
    )
    return f"""
      <Grid Tag="MetricUsage" Height="{ROW_HEIGHT:.0f}" Margin="{margin}"{tooltip_attr}>
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="*"/>
        </Grid.RowDefinitions>
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="{VALUE_COLUMN:.0f}"/>
          </Grid.ColumnDefinitions>
          <StackPanel Orientation="Horizontal" ClipToBounds="True">
            <TextBlock Text="{label}" Foreground="{theme['muted']}"
                       FontSize="10.5" FontWeight="Medium" VerticalAlignment="Center"
                       TextTrimming="CharacterEllipsis"/>
            {reset_block}
          </StackPanel>
          <TextBlock Grid.Column="1" Text="{value}" Foreground="{theme['fg']}"
                     FontSize="12" FontWeight="SemiBold" HorizontalAlignment="Right"
                     Typography.NumeralAlignment="Tabular" VerticalAlignment="Center"/>
        </Grid>
        <Grid Grid.Row="1" Height="{BAR_HEIGHT:.0f}" VerticalAlignment="Bottom" Margin="0,4,0,0">
          <Border Background="{theme['track']}" CornerRadius="3"/>
          <Border Width="{_bar_fill(pct):.1f}" HorizontalAlignment="Left"
                  CornerRadius="3" Background="{fill_color}"/>
        </Grid>
      </Grid>"""


def _mark_for(name):
    """API-key providers are user-configurable, so their mark comes from the name."""
    letters = [char for char in str(name) if char.isalnum()]
    return letters[0].upper() if letters else "?"


def _accent_for(name):
    # Deterministic across restarts on purpose: Python's str hash is salted per
    # process, which would repaint every provider a new colour on each launch.
    checksum = sum(ord(char) for char in str(name))
    return API_ACCENTS[checksum % len(API_ACCENTS)]


def _headline(metrics):
    """The one row a compact provider gets: its worst window, else its first fact."""
    scored = [metric for metric in metrics if metric.get("pct") is not None]
    if scored:
        return max(scored, key=lambda metric: metric["pct"])
    return metrics[0]


def _split_zero_metrics(metrics):
    """Return visible metrics plus zero-percent quotas worth disclosing on demand."""
    zeroes = [metric for metric in metrics if metric.get("pct") == 0]
    if len(zeroes) < 2:
        return metrics, []

    return [metric for metric in metrics if metric.get("pct") != 0], zeroes


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


def _zero_quota_expander(zeroes, theme, provider_name):
    """Native keyboard-accessible disclosure for quota periods at exactly 0%."""
    details = []
    for metric in zeroes:
        label = _short_label(metric.get("label")) or "quota"
        reset = metric.get("_reset_text") or ""
        details.append(f"{label} at 0% · resets {reset}" if reset else f"{label} at 0%")

    rows = "".join(
        _metric_row(metric, theme, last=index == len(zeroes) - 1)
        for index, metric in enumerate(zeroes)
    )
    count = len(zeroes)
    label = f"{count} quotas at 0%"
    return f"""
        <Expander Tag="ZeroUsage:{_x(provider_name)}" IsExpanded="False"
                  HorizontalContentAlignment="Stretch" Cursor="Hand"
                  AutomationProperties.Name="{_x(label)}; expand for details"
                  ToolTip="{_x('; '.join(details))}">
          <Expander.Header>
            <Grid Width="{CONTENT_WIDTH - 24:.0f}" Height="{AMOUNT_ROW_HEIGHT:.0f}">
              <TextBlock Text="{_x(label)}" Foreground="{theme['faint']}"
                         FontSize="10.5" FontWeight="Medium" VerticalAlignment="Center"/>
            </Grid>
          </Expander.Header>
          <StackPanel Margin="18,6,0,0">{rows}</StackPanel>
        </Expander>"""


def _card(agent, theme, last=False):
    raw_name = agent.get("agent") or "Unknown"
    name = "Antigravity" if raw_name == "Antigrav" else raw_name
    mark = PROVIDER_MARKS.get(raw_name) or _mark_for(name)
    accent = PROVIDER_ACCENTS.get(raw_name) or _accent_for(name)

    metrics = [
        _display_metric(metric, name)
        for metric in (agent.get("metrics") or [])
        if not _is_zero_money_metric(metric)
    ]
    zeroes = []
    if agent.get("compact") and metrics:
        metrics = [_headline(metrics)]
    else:
        metrics, zeroes = _split_zero_metrics(metrics)
    status_text = agent.get("_status_text") or "unavailable"
    status_color = agent.get("_status_color") or GRAD["n"]
    icon = _provider_icon_markup(name, theme, mark, accent, status_text, status_color)
    if metrics or zeroes:
        row_parts = [
            _metric_row(metric, theme, last=index == len(metrics) - 1 and not zeroes)
            for index, metric in enumerate(metrics)
        ]
        if zeroes:
            row_parts.append(_zero_quota_expander(zeroes, theme, name))
        rows = "".join(row_parts)
    else:
        rows = _metric_row(
            {"label": "", "pct": None, "extra": agent.get("err") or "no data"},
            theme,
            last=True,
        )

    margin = "0" if last else "0,0,0,7"
    return f"""
    <Border BorderBrush="{theme['card_border']}" BorderThickness="1" CornerRadius="10"
            Padding="{CARD_PADDING:.0f},8,{CARD_PADDING:.0f},9" Margin="{margin}">
      <Border.Background>
        <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
          <GradientStop Color="{theme['card_top']}" Offset="0"/>
          <GradientStop Color="{theme['card_bg']}" Offset="1"/>
        </LinearGradientBrush>
      </Border.Background>
      <StackPanel>
        <Grid Tag="ProviderHeader" Height="{PROVIDER_HEADER_HEIGHT:.0f}" Margin="0,0,0,7">
          <StackPanel Orientation="Horizontal" VerticalAlignment="Center" ClipToBounds="True">
            {icon}
            <TextBlock Text="{_x(name)}" Foreground="{theme['fg']}" FontSize="12.5"
                       FontWeight="SemiBold" VerticalAlignment="Center"
                       MaxWidth="{NAME_MAX_WIDTH:.0f}" TextTrimming="CharacterEllipsis"/>
          </StackPanel>
        </Grid>
        {rows}
      </StackPanel>
    </Border>"""


def _content_xaml(data, ts, light, fleet, acrylic=True):
    """The panel's inner content, parseable on its own.

    Kept separate from the window shell so refreshed data can be swapped into a
    live window. Rebuilding the whole Window instead costs a fresh HWND every
    time, which is the bulk of the open/close cost.
    """
    import time

    data = _visible_agents(data)
    theme = _theme(light, acrylic=acrylic)
    cards = []
    for index, agent in enumerate(data):
        copied = dict(agent)
        if not agent.get("err"):
            copied["_status_text"], copied["_status_color"] = "live", GRAD["g"]
        elif agent.get("metrics"):
            age = (
                f" · {fleet._age(agent['cached_at'])} old"
                if agent.get("cached_at")
                else ""
            )
            copied["_status_text"], copied["_status_color"] = f"cached{age}", GRAD["y"]
        else:
            reason = agent.get("err") or "no data"
            copied["_status_text"] = f"unavailable · {reason}"
            copied["_status_color"] = GRAD["r"]
        copied["metrics"] = []
        for metric in agent.get("metrics") or []:
            item = dict(metric)
            item["_reset_text"] = fleet._until(item["resets"]) if item.get("resets") else ""
            copied["metrics"].append(item)
        cards.append(_card(copied, theme, last=index == len(data) - 1))

    clock = time.strftime("%H:%M", time.localtime(ts))
    failures = [agent for agent in data if agent.get("err")]
    cached = [agent for agent in failures if agent.get("metrics")]
    cached_times = [agent.get("cached_at") for agent in cached if agent.get("cached_at")]
    if not data:
        status_text, status_color = "no data", GRAD["n"]
    elif cached:
        live_count = len(data) - len(failures)
        age = f"{fleet._age(min(cached_times))} old" if cached_times else "cache"
        status_text, status_color = f"{live_count}/{len(data)} live · {age}", GRAD["y"]
    elif failures:
        status_text = f"{len(data) - len(failures)}/{len(data)} live · error"
        status_color = GRAD["r"]
    else:
        status_text, status_color = f"live · {clock}", GRAD["g"]
    return f"""
<StackPanel xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
            xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
            x:Name="ContentRoot" Opacity="1">
  <Grid Height="24" Margin="2,0,2,9">
    <Grid.ColumnDefinitions>
      <ColumnDefinition Width="Auto"/>
      <ColumnDefinition Width="*"/>
      <ColumnDefinition Width="Auto"/>
    </Grid.ColumnDefinitions>
    <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
      <Border Width="3" Height="14" Background="{METER['g']}" CornerRadius="1.5"
              Margin="0,0,7,0"/>
      <TextBlock Text="fleet" Foreground="{theme['fg']}" FontSize="13.5"
                 FontWeight="SemiBold" VerticalAlignment="Center"/>
      <TextBlock Text="usage" Foreground="{theme['faint']}" FontSize="10.5"
                 Margin="7,1,0,0" VerticalAlignment="Center"/>
    </StackPanel>
    <StackPanel Grid.Column="2" Orientation="Horizontal" VerticalAlignment="Center">
      <Ellipse Width="6" Height="6" Fill="{status_color}" Margin="0,0,6,0"
               VerticalAlignment="Center"/>
      <TextBlock Text="{_x(status_text)}" Foreground="{theme['muted']}"
                 FontSize="10" FontWeight="Medium" Typography.NumeralAlignment="Tabular"
                 VerticalAlignment="Center"/>
    </StackPanel>
  </Grid>
  {''.join(cards)}
</StackPanel>"""


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
      <SolidColorBrush Color="{theme['surface']}"/>
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
        if current["ts"] != ts or current["light"] != light_loader():
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
            return
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
        window = current["window"]
        if window is None:
            if not stopping["value"]:
                build()
            return
        # State swaps are local and cheap; keep an open panel current too.
        if now - refresh_check["at"] >= 0.75:
            refresh_check["at"] = now
            data, ts = state_loader()
            light = light_loader()
            if current["ts"] != ts or current["light"] != light:
                sync(data, ts, light)

    timer.Tick += tick
    timer.Start()
    app.Run()


def _create_window(data, ts, light, fleet, auto_present=True):
    from System import TimeSpan
    from System.Windows import Duration, LogicalTreeHelper, SystemParameters, UIElement
    from System.Windows.Controls import Expander
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

    want_acrylic = not _force_opaque()
    theme = _theme(light, acrylic=want_acrylic)
    window = XamlReader.Parse(_xaml(data, ts, light, fleet, acrylic=want_acrylic))
    glass_clip = window.FindName("GlassClip")
    slide_transform = window.FindName("SlideTransform")
    if slide_transform is None:
        slide_transform = TranslateTransform()
        glass_clip.RenderTransform = slide_transform
    chrome = {"acrylic": False}

    def zero_expansion_state(root):
        """Capture disclosure state before a live content fragment is replaced."""
        states = {}
        stack = [root] if root is not None else []
        while stack:
            node = stack.pop()
            if isinstance(node, Expander):
                tag = str(node.Tag or "")
                if tag.startswith("ZeroUsage:"):
                    states[tag] = bool(node.IsExpanded)
            try:
                stack.extend(child for child in LogicalTreeHelper.GetChildren(node) if child is not None)
            except Exception:
                pass
        return states

    def restore_zero_expansion(root, states):
        stack = [root] if root is not None else []
        while stack:
            node = stack.pop()
            if isinstance(node, Expander):
                tag = str(node.Tag or "")
                if tag in states:
                    node.IsExpanded = states[tag]
            try:
                stack.extend(child for child in LogicalTreeHelper.GetChildren(node) if child is not None)
            except Exception:
                pass

    def swap_content(new_data, new_ts):
        """Replace the panel's contents without touching the window or its HWND.

        Creating a window costs ~37 ms of HWND and composition-target setup, and
        the old code paid that on every dismiss because it closed the window
        instead of hiding it. Parsing this fragment costs a few ms.
        """
        expanded = zero_expansion_state(glass_clip.Child)
        fragment = XamlReader.Parse(
            _content_xaml(new_data, new_ts, light, fleet, acrylic=chrome["acrylic"])
        )
        restore_zero_expansion(fragment, expanded)
        glass_clip.Child = fragment
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
        window.Deactivated += lambda sender, event: (deactivate.Stop(), deactivate.Start())
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
