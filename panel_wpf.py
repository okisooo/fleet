"""Native WPF flyout renderer for Fleet.

The warm host keeps one opaque WPF window ready between clicks.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
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
DWMWCP_ROUND = 2
MONITOR_DEFAULTTONEAREST = 2
PANEL_EDGE_GAP = 12.0
PANEL_ANCHOR_GAP = 10.0
# Must match what DWM actually rounds the window to. DWMWCP_ROUND is the Windows 11
# standard 8 px for a flyout of this size. Drawing the rim at a different radius put
# three curves on screen at once, which is what made the corners look mislayered.
PANEL_CORNER_RADIUS = 8.0
PANEL_WIDTH = 280.0
PANEL_PADDING = 10.0
PANEL_BORDER = 1.0
ROW_HEIGHT = 19.0
# Columns must add up to the content width or the last one gets squeezed to
# nothing: an over-wide bar is what cut "88%" down to "88" against the bar.
IDENTITY_COLUMN = 88.0
LABEL_COLUMN = 44.0
VALUE_COLUMN = 56.0
CONTENT_WIDTH = PANEL_WIDTH - 2 * PANEL_PADDING - 2 * PANEL_BORDER
BAR_COLUMN = CONTENT_WIDTH - IDENTITY_COLUMN - LABEL_COLUMN - VALUE_COLUMN
BAR_TRACK = BAR_COLUMN - 10.0  # the column carries a right margin


def _bar_fill(pct):
    """Bar width in DIPs. Zero stays empty rather than showing a stub."""
    if pct <= 0.0:
        return 0.0
    return max(3.0, BAR_TRACK * pct / 100.0)
PROVIDER_MARKS = {"Claude": "C", "Codex": "X", "Grok": "G", "Antigrav": "A"}
PROVIDER_ACCENTS = {"Claude": "#FFF59E0B", "Codex": "#FF22D3EE", "Grok": "#FFD946EF", "Antigrav": "#FF22C55E"}
GRAD = {"g": "#FF3FB950", "y": "#FFD29922", "r": "#FFF85149", "n": "#FF6E7681"}


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


def _present_prepared_window(window, anchor_rect, position, begin_slide):
    """Show the warm flyout once, already in its final visual state."""
    position(anchor_rect)
    window.UpdateLayout()
    window.Opacity = 1.0
    begin_slide()
    window.Show()
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


def _apply_window_chrome(hwnd, light):
    """Round the window and match the OS theme. No transparency is requested.

    Extending the frame or asking for a system backdrop only matters when the
    client area is meant to show through. This panel paints itself opaquely, so
    doing either just risks the black client area seen on recent Windows builds.
    """
    _set_dwm_int(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 0 if light else 1)
    return _set_dwm_int(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)


def _theme(light):
    """Colour tokens for one OS theme.

    Text sits on a live, moving backdrop, so every foreground colour is fully
    opaque. Semi-transparent greys were the old approach and they let the desktop
    modulate the text contrast, which is how the panel ended up grey-on-grey
    whenever a light window happened to pass behind it.
    """
    if light:
        return {
            # Opaque. Windows build 26xxx no longer honours
            # ACCENT_ENABLE_ACRYLICBLURBEHIND: the call succeeds but leaves the
            # client area undefined, and with AllowsTransparency="False" undefined
            # composites as black. Readability is not worth another attempt.
            "surface": "#FFF7F9FC",
            "border": "#FFD8DEE9",
            "divider": "#FFE7ECF3",
            "fg": "#FF10151E",
            "muted": "#FF48566B",
            "faint": "#FF6A788C",
            "track": "#FFDDE3EC",
        }
    return {
        "surface": "#FF1A1E25",
        "border": "#FF313842",
        "divider": "#FF262C35",
        "fg": "#FFF2F5F9",
        "muted": "#FFAFBCCC",
        "faint": "#FF8593A5",
        "track": "#FF2C333D",
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
}


def _short_label(value):
    raw = str(value or "").strip()
    return SHORT_LABELS.get(raw, raw.lower())


def _metric_row(
    metric,
    theme,
    name=None,
    mark=None,
    accent=None,
    status_text="",
    status_color=None,
):
    """One line: optional provider identity, then label, bar, percentage.

    The provider name lives on its first metric rather than in a header of its
    own. That removes a whole row plus its spacing per provider, which is most of
    where the old panel's height went.
    """
    label = _x(_short_label(metric.get("label")))
    pct = metric.get("pct")
    identity = ""
    if name is not None:
        icon = f"""
            <Grid Width="14" Height="14" Margin="0,0,7,0">
              <Border Background="{accent or theme['track']}" CornerRadius="7"/>
              <TextBlock Text="{_x(mark or '?')}" Foreground="{theme['surface']}" FontSize="8.5"
                         FontWeight="Bold" HorizontalAlignment="Center" VerticalAlignment="Center"/>
              <Ellipse Width="6" Height="6" Fill="{status_color or GRAD['n']}"
                       Stroke="{theme['surface']}" StrokeThickness="1"
                       HorizontalAlignment="Right" VerticalAlignment="Bottom"
                       ToolTip="{_x(status_text)}"/>
            </Grid>"""
        identity = f"""
          <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
            {icon}
            <TextBlock Text="{_x(name)}" Foreground="{theme['fg']}" FontSize="11.5"
                       FontWeight="SemiBold" VerticalAlignment="Center"/>
          </StackPanel>"""

    # One grid shape for both row kinds so every column stays in the same place.
    # Content width is PANEL_WIDTH minus padding and border; the bar takes what is
    # left after the fixed columns, instead of a hardcoded width that overflowed
    # and squeezed the percentage down to 12 px.
    columns = f"""
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="{IDENTITY_COLUMN:.0f}"/>
            <ColumnDefinition Width="{LABEL_COLUMN:.0f}"/>
            <ColumnDefinition Width="{BAR_COLUMN:.0f}"/>
            <ColumnDefinition Width="{VALUE_COLUMN:.0f}"/>
          </Grid.ColumnDefinitions>"""

    if pct is None:
        value = _x(metric.get("extra") or "—")
        return f"""
        <Grid Height="{ROW_HEIGHT:.0f}">{columns}
          {identity}
          <TextBlock Grid.Column="1" Text="{label}" Foreground="{theme['faint']}"
                     FontSize="10.5" VerticalAlignment="Center"
                     TextTrimming="CharacterEllipsis"/>
          <TextBlock Grid.Column="2" Grid.ColumnSpan="2" Text="{value}"
                     Foreground="{theme['muted']}" FontSize="10.5"
                     TextAlignment="Right" TextTrimming="CharacterEllipsis"
                     Typography.NumeralAlignment="Tabular" VerticalAlignment="Center"/>
        </Grid>"""

    pct = max(0.0, min(100.0, float(pct)))
    fill_color = GRAD[_sevkey(pct)]
    reset = _x(metric.get("_reset_text") or "")
    pct_alignment = "Left" if reset else "Right"
    reset_markup = (
        f"""<TextBlock Text="{reset}" Foreground="{theme['faint']}" FontSize="9.5"
                     HorizontalAlignment="Right" VerticalAlignment="Center"
                     Typography.NumeralAlignment="Tabular"/>"""
        if reset
        else ""
    )
    return f"""
      <Grid Height="{ROW_HEIGHT:.0f}">{columns}
        {identity}
        <TextBlock Grid.Column="1" Text="{label}" Foreground="{theme['faint']}"
                   FontSize="10.5" VerticalAlignment="Center"
                   TextTrimming="CharacterEllipsis"/>
        <Grid Grid.Column="2" Height="4" VerticalAlignment="Center" Margin="0,0,10,0">
          <Border Background="{theme['track']}" CornerRadius="2"/>
          <Border Width="{_bar_fill(pct):.1f}" HorizontalAlignment="Left"
                  CornerRadius="2" Background="{fill_color}"/>
        </Grid>
        <Grid Grid.Column="3">
          <TextBlock Text="{pct:.0f}%" Foreground="{theme['fg']}"
                     FontSize="11" FontWeight="SemiBold"
                     HorizontalAlignment="{pct_alignment}"
                     Typography.NumeralAlignment="Tabular" VerticalAlignment="Center"/>
          {reset_markup}
        </Grid>
      </Grid>"""


def _card(agent, theme, last=False):
    raw_name = agent.get("agent") or "Unknown"
    name = "Antigravity" if raw_name == "Antigrav" else raw_name
    mark = PROVIDER_MARKS.get(raw_name, "?")
    accent = PROVIDER_ACCENTS.get(raw_name, theme["track"])

    metrics = agent.get("metrics") or []
    status_text = agent.get("_status_text") or "unavailable"
    status_color = agent.get("_status_color") or GRAD["n"]
    if metrics:
        rows = "".join(
            _metric_row(
                metric,
                theme,
                name=name if index == 0 else None,
                mark=mark if index == 0 else None,
                accent=accent if index == 0 else None,
                status_text=status_text if index == 0 else "",
                status_color=status_color if index == 0 else None,
            )
            for index, metric in enumerate(metrics)
        )
    else:
        rows = _metric_row(
            {"label": "", "pct": None, "extra": agent.get("err") or "no data"},
            theme,
            name=name,
            mark=mark,
            accent=accent,
            status_text=status_text,
            status_color=status_color,
        )

    divider = "0" if last else "0,0,0,1"
    return f"""
    <Border BorderBrush="{theme['divider']}" BorderThickness="{divider}"
            Padding="0,3,0,3">
      <StackPanel>{rows}</StackPanel>
    </Border>"""


def _content_xaml(data, ts, light, fleet):
    """The panel's inner content, parseable on its own.

    Kept separate from the window shell so refreshed data can be swapped into a
    live window. Rebuilding the whole Window instead costs a fresh HWND every
    time, which is the bulk of the open/close cost.
    """
    import time

    theme = _theme(light)
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
  <Grid Height="18" Margin="0,0,0,3">
    <TextBlock Text="fleet" Foreground="{theme['muted']}" FontSize="10.5"
               FontWeight="SemiBold" VerticalAlignment="Center"/>
    <StackPanel Orientation="Horizontal" HorizontalAlignment="Right"
                VerticalAlignment="Center">
      <Ellipse Width="5" Height="5" Fill="{status_color}" Margin="0,0,5,0"
               VerticalAlignment="Center"/>
      <TextBlock Text="{_x(status_text)}" Foreground="{theme['faint']}"
                 FontSize="10" Typography.NumeralAlignment="Tabular"
                 VerticalAlignment="Center"/>
    </StackPanel>
  </Grid>
  {''.join(cards)}
</StackPanel>"""


def _xaml(data, ts, light, fleet):
    """The window shell with its first content already inside."""
    theme = _theme(light)
    content = _content_xaml(data, ts, light, fleet)
    # The fragment carries its own namespace declarations; strip them so it nests
    # cleanly inside the window.
    content = content.replace(
        '<StackPanel xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"\n'
        '            xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"\n'
        '            x:Name="ContentRoot"',
        '<StackPanel x:Name="ContentRoot"',
    )
    return f"""
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Width="{PANEL_WIDTH:.0f}" SizeToContent="Height" WindowStyle="None"
        ResizeMode="NoResize" AllowsTransparency="False"
        Background="{theme['surface']}" ShowInTaskbar="False"
        Topmost="True" Opacity="0" FontFamily="Bahnschrift"
        TextOptions.TextFormattingMode="Display" SnapsToDevicePixels="True"
        UseLayoutRounding="True">
  <!-- Opaque on purpose. Every transparency route on this Windows build ends in a
       black client area, and unreadable is worse than unfashionable. -->
  <Border x:Name="GlassClip" CornerRadius="8" BorderThickness="1"
          BorderBrush="{theme['border']}" Background="{theme['surface']}"
          Padding="10,8,10,8">
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
    from System.Windows import Duration, SystemParameters
    from System.Windows.Interop import WindowInteropHelper
    from System.Windows.Markup import XamlReader
    from System.Windows.Media import VisualTreeHelper
    from System.Windows.Media.Animation import (
        CubicEase,
        DoubleAnimation,
        EasingMode,
        FillBehavior,
    )
    from System.Windows.Threading import DispatcherTimer

    theme = _theme(light)
    window = XamlReader.Parse(_xaml(data, ts, light, fleet))
    glass_clip = window.FindName("GlassClip")

    def swap_content(new_data, new_ts):
        """Replace the panel's contents without touching the window or its HWND.

        Creating a window costs ~37 ms of HWND and composition-target setup, and
        the old code paid that on every dismiss because it closed the window
        instead of hiding it. Parsing this fragment costs a few ms.
        """
        fragment = XamlReader.Parse(_content_xaml(new_data, new_ts, light, fleet))
        glass_clip.Child = fragment
        window.UpdateLayout()
        if window.IsVisible:
            position(anchor["rect"])

    # No explicit rounded clip: it made the corner pixels transparent, and this
    # window cannot carry per-pixel alpha, so they composited black. DWM already
    # clips the window to its rounded shape.
    native = {"hwnd": 0}

    # WS_EX_TOOLWINDOW keeps this flyout out of Alt+Tab as well as the taskbar.
    def source_ready(sender, event):
        hwnd = int(WindowInteropHelper(window).Handle.ToInt64())
        get_style = ctypes.windll.user32.GetWindowLongW
        set_style = ctypes.windll.user32.SetWindowLongW
        exstyle = get_style(hwnd, -20)
        set_style(hwnd, -20, exstyle | 0x00000080)
        native["hwnd"] = hwnd
        _apply_window_chrome(hwnd, light)

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
    animations = {"open": None, "close": None}

    def ease(mode):
        curve = CubicEase()
        curve.EasingMode = mode
        return curve

    def begin_open_slide():
        closing["value"] = False
        if not SystemParameters.ClientAreaAnimation:
            return
        top = window.Top
        animation = DoubleAnimation(
            top + 10.0,
            top,
            Duration(TimeSpan.FromMilliseconds(130)),
        )
        animation.EasingFunction = ease(EasingMode.EaseOut)
        animation.FillBehavior = FillBehavior.Stop
        animations["open"] = animation
        window.BeginAnimation(window.TopProperty, animation)

    if auto_present:
        def auto_open(sender, event):
            position()
            window.UpdateLayout()
            window.Opacity = 1.0
            begin_open_slide()
            window.Activate()

        window.ContentRendered += auto_open

    def dismiss():
        """Hide, never close.

        Closing destroys the HWND and composition target, so the next open has to
        rebuild both -- about 37 ms versus 6 ms to re-show the same window. That
        was most of what made repeated opens feel sluggish.
        """
        window.BeginAnimation(window.TopProperty, None)
        window.Hide()
        window.Opacity = 0.0
        closing["value"] = False

    def close_animated(*_):
        if closing["value"] or not window.IsVisible:
            return
        closing["value"] = True
        if not SystemParameters.ClientAreaAnimation:
            dismiss()
            return
        top = window.Top
        animation = DoubleAnimation(
            top,
            top + 10.0,
            Duration(TimeSpan.FromMilliseconds(100)),
        )
        animation.EasingFunction = ease(EasingMode.EaseIn)
        animation.FillBehavior = FillBehavior.Stop
        animation.Completed += lambda sender, event: dismiss()
        animations["close"] = animation
        window.BeginAnimation(window.TopProperty, animation)

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
            close_animated()
            return
        return _present_prepared_window(
            window,
            anchor_rect,
            position,
            begin_open_slide,
        )

    return window, present, swap_content
