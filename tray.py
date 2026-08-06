#!/usr/bin/env python
"""
tray.py - system-tray widget for fleet.

Tray icon cycles logo-led provider usage badges. Single-click (or menu -> Show
panel) opens a native popup card with per-agent bars. The popup is a SEPARATE
process, so it displays reliably and closes on click-away. Provider data refreshes
every 120s.

Run (hidden):  fleet-tray.cmd
Panel only:    pythonw tray.py --panel
Test data:     uv run --with pystray --with pillow python tray.py --dry
"""
from __future__ import annotations
import ctypes, ctypes.wintypes, json, os, subprocess, sys, threading, time
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet

HERE = Path(__file__).resolve().parent
_BUNDLED = bool(getattr(sys, "frozen", False))
DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Fleet" if _BUNDLED else HERE
CACHE = DATA_ROOT / ".cache"
STATE = CACHE / "state.json"
PYW = HERE / ".venv" / "Scripts" / "pythonw.exe"
PANEL_PID = CACHE / "panel-host.pid"
PANEL_SIGNAL = CACHE / "panel.show"
PANEL_QUIT = CACHE / "panel.quit"
REFRESH_SECS = 120
CYCLE_SECS = 2
CYCLE_CAP = 6  # icon frames per loop; see usage_cycle
MARK = {"Claude": "CL", "Codex": "CX", "Grok": "GR", "Antigrav": "AG"}

# palette (dark card -> readable regardless of OS light/dark theme)
BG, CARD, FG, MUTED, TRACK = "#15151d", "#1e1e28", "#e8e8f0", "#9a9aa8", "#33333f"
ACCENT = {"Claude": "#f59e0b", "Codex": "#22d3ee", "Grok": "#d946ef", "Antigrav": "#22c55e"}
# API-key and custom providers are named by the user, so their mark and colour
# are derived from the name instead of a table.
API_ACCENT = ["#60a5fa", "#34d399", "#fbbf24", "#a78bfa", "#f472b6", "#38bdf8", "#fb923c", "#94a3b8"]


def _mark_for(agent):
    letters = [char for char in str(agent or "") if char.isalnum()]
    capitals = [char for char in letters if char.isupper()]
    if agent in MARK:
        return MARK[agent]
    if len(capitals) >= 2:
        return "".join(capitals[:2])
    return "".join(letters[:2]).upper() if letters else "F"


def _accent_for(agent):
    # Stable across restarts: str hash is salted per process.
    if agent in ACCENT:
        return ACCENT[agent]
    return API_ACCENT[sum(ord(char) for char in str(agent or "")) % len(API_ACCENT)]


_ERROR_ALREADY_EXISTS = 183
_INSTANCE_LOCKS = []  # holds the mutex handles for the life of the process


def claim_single_instance(name):
    """Take a named mutex, or return False when another instance already holds it.

    Launching the tray twice would poll every provider twice, which can trigger
    provider rate limits, and would leave two panel hosts answering the
    same panel.show signal, so one click builds two stacked flyouts.

    Note that one fleet process shows up as two entries in the process list: the
    venv's pythonw.exe is a uv shim that runs the real interpreter as a child
    with an identical command line. Only the child executes this, so the shim
    does not trip the guard.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.LPCWSTR,
    )
    kernel32.CloseHandle.argtypes = (ctypes.wintypes.HANDLE,)
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        return True  # cannot arbitrate; do not block the only instance
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _INSTANCE_LOCKS.append(handle)
    return True


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.wintypes.DWORD),
        ("Data2", ctypes.wintypes.WORD),
        ("Data3", ctypes.wintypes.WORD),
        ("Data4", ctypes.wintypes.BYTE * 8),
    ]


class _NotifyIconIdentifier(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("hWnd", ctypes.wintypes.HWND),
        ("uID", ctypes.wintypes.UINT),
        ("guidItem", _Guid),
    ]


def _tray_icon_rect(icon):
    """Return the real notification-icon bounds, not the mouse position."""
    hwnd = int(getattr(icon, "_hwnd", 0) or 0)
    if not hwnd:
        return None
    identifier = _NotifyIconIdentifier()
    identifier.cbSize = ctypes.sizeof(identifier)
    identifier.hWnd = hwnd
    # pystray registers one icon per hidden HWND and leaves uID at zero.
    identifier.uID = 0
    rect = ctypes.wintypes.RECT()
    result = ctypes.windll.shell32.Shell_NotifyIconGetRect(
        ctypes.byref(identifier), ctypes.byref(rect)
    )
    if result != 0:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def _write_panel_signal(icon=None):
    anchor = _tray_icon_rect(icon) if icon is not None else None
    payload = {
        "ts": time.time_ns(),
        "anchor": list(anchor) if anchor else None,
    }
    PANEL_SIGNAL.write_text(json.dumps(payload))


def _panel_host_command():
    """Run the bundled executable again, or the source script in development."""
    if _BUNDLED:
        return [sys.executable, "--panel-host"]
    exe = str(PYW) if PYW.exists() else "pythonw"
    return [exe, str(HERE / "tray.py"), "--panel-host"]


def worst_pct(data):
    vals = [m["pct"] for d in data for m in d["metrics"] if m["pct"] is not None]
    return max(vals) if vals else None


def sev(pct):
    if pct is None:
        return "#64748b"
    # Match the popup meters: calm blue, then amber and coral under pressure.
    return "#4f7cff" if pct < 60 else "#d89a2b" if pct < 85 else "#e05252"


# ---------- tray icon ----------
def usage_cycle(data):
    """Icon frames, worst first for API providers and capped.

    Every configured API key would otherwise add two seconds to the loop, so a
    user with a dozen keys would wait half a minute to see Claude come round.
    """
    local, api = [], []
    for provider in data:
        pcts = [
            metric["pct"]
            for metric in provider.get("metrics", [])
            if metric.get("pct") is not None
        ]
        # Compact API providers that only expose a balance add no actionable
        # utilization signal. Keep them in the popup, not in the icon carousel.
        if provider.get("compact") and not pcts:
            continue
        frame = (provider.get("agent"), max(pcts) if pcts else None)
        (api if provider.get("compact") else local).append(frame)
    api.sort(key=lambda frame: -(frame[1] or 0.0))
    frames = local + api[: max(0, CYCLE_CAP - len(local))]
    return frames or [(None, None)]


def _hex_to_rgb(hex_str):
    h = str(hex_str or "").lstrip("#")
    if len(h) == 8:
        return tuple(int(h[i:i + 2], 16) for i in (2, 4, 6))
    if len(h) == 6:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (148, 163, 184)


def _get_icon_font(size):
    for fpath in (
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\bahnschrift.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
    ):
        if Path(fpath).exists():
            try:
                from PIL import ImageFont
                return ImageFont.truetype(fpath, size)
            except Exception:
                pass
    from PIL import ImageFont
    return ImageFont.load_default(size=size)


ASSETS_DIR = HERE / "assets" / "providers"
PROVIDER_ASSETS = {
    "claude": "claude.png",
    "anthropic": "claude.png",
    "anthropicorg": "claude.png",
    "codex": "codex.png",
    "openai": "codex.png",
    "openaiorg": "codex.png",
    "grok": "grok.ico",
    "xai": "grok.ico",
    "antigrav": "antigravity.png",
    "antigravity": "antigravity.png",
}


def _provider_asset_path(agent):
    key = "".join(char for char in str(agent or "").casefold() if char.isalnum())
    filename = PROVIDER_ASSETS.get(key)
    path = ASSETS_DIR / filename if filename else None
    return path if path and path.is_file() else None


def _provider_art(agent):
    """Load and tightly crop provider artwork without mutating the source file."""
    from PIL import Image, ImageChops

    path = _provider_asset_path(agent)
    if path is None:
        return None
    try:
        with Image.open(path) as source:
            source.seek(0)
            art = source.convert("RGBA")
    except (OSError, ValueError):
        return None

    alpha = art.getchannel("A")
    if alpha.getextrema()[0] < 255:
        bounds = alpha.getbbox()
    else:
        background = Image.new("RGBA", art.size, art.getpixel((0, 0)))
        bounds = ImageChops.difference(art, background).convert("RGB").getbbox()
    return art.crop(bounds) if bounds else art


def _draw_provider_badge(img, draw, agent, box):
    """Draw real artwork when available, with a two-letter fallback for custom APIs."""
    from PIL import Image, ImageOps

    left, top, right, bottom = box
    width, height = right - left, bottom - top
    art = _provider_art(agent)
    if art is not None:
        fitted = ImageOps.contain(
            art,
            (max(1, int(width * 0.88)), max(1, int(height * 0.88))),
            method=Image.Resampling.LANCZOS,
        )
        x = int(left + (width - fitted.width) / 2)
        y = int(top + (height - fitted.height) / 2)
        img.alpha_composite(fitted, (x, y))
        return

    mark = _mark_for(agent)
    font = _get_icon_font(int(height * (0.48 if len(mark) == 1 else 0.38)))
    ink = draw.textbbox((0, 0), mark, font=font)
    x = left + (width - (ink[2] - ink[0])) / 2 - ink[0]
    y = top + (height - (ink[3] - ink[1])) / 2 - ink[1] - 2
    draw.text((x, y), mark, fill=(15, 23, 42, 255), font=font)


@lru_cache(maxsize=32)
def make_icon_image(agent, pct):
    from PIL import Image, ImageDraw

    HIGH_S = 256
    METER_TOP = 194

    img = Image.new("RGBA", (HIGH_S, HIGH_S), (248, 250, 252, 255))
    d = ImageDraw.Draw(img)

    # The shell shows this around 16 px. A full-bleed tile uses the whole slot;
    # the 62 px source meter becomes a legible four-pixel band instead of the
    # old 1.4 px ring. Returning the 256 px source also lets Pillow build ICO
    # frames directly, avoiding the old 256 -> 64 -> shell-size double resample.
    track = (100, 116, 139, 255) if pct is None else (51, 65, 85, 255)
    d.rectangle((0, METER_TOP, HIGH_S, HIGH_S), fill=track)

    if pct is not None:
        value = max(0.0, min(100.0, float(pct)))
        if value > 0:
            color = _hex_to_rgb(sev(value)) + (255,)
            # Preserve one logical pixel after pystray's ICO32 -> shell16 pass.
            # Without this low positive fills disappear into the rounded corner.
            fill_width = max(HIGH_S * 3 // 32, round(HIGH_S * value / 100.0))
            d.rectangle((0, METER_TOP, fill_width - 1, HIGH_S), fill=color)

    _draw_provider_badge(img, d, agent, (24, 16, 232, 190))
    d.rounded_rectangle(
        (2, 2, HIGH_S - 3, HIGH_S - 3),
        radius=44,
        outline=(148, 163, 184, 255),
        width=7,
    )
    mask = Image.new("L", (HIGH_S, HIGH_S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, HIGH_S - 1, HIGH_S - 1), radius=46, fill=255
    )
    img.putalpha(mask)

    return img


def tooltip(data):
    # Same selection as the icon cycle: a Windows tooltip is capped at 128 chars,
    # so listing every configured key would cut a provider in half.
    by_name = {d.get("agent"): d for d in data}
    parts = []
    for agent, pct in usage_cycle(data):
        d = by_name.get(agent)
        if d is None:
            continue
        name = "Antigravity" if agent == "Antigrav" else str(agent or "Fleet")
        if pct is not None:
            parts.append(f"{name} {pct:.0f}%")
        elif d.get("metrics"):
            first = d["metrics"][0]
            parts.append(f"{name} {(first.get('extra') or first.get('label') or '')[:8]}".strip())
        else:
            parts.append(f"{name} –")
    return ("FLEET  ·  " + "  ·  ".join(parts))[:126]


# ---------- popup panel ----------
GRAD = {  # severity fill gradients + glow
    "g": ("#22c55e", "#4ade80"), "y": ("#eab308", "#facc15"), "r": ("#ef4444", "#fb7185"),
    "n": ("#64748b", "#94a3b8"),
}


def _sevkey(pct):
    if pct is None:
        return "n"
    return "g" if pct < 60 else "y" if pct < 85 else "r"


def _os_light():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return bool(v)
    except Exception:
        return False


def _tkpal(light):
    if light:
        return dict(win="#eef0f4", card="#ffffff", cardbd="#e7e7ee", fg="#1b1b22",
                    muted="#6b7280", clock="#9aa0ac", pill="#eef0f4", pillfg="#4b5563",
                    track=(214, 217, 224), sep="#eceef2", ft="#a9adba")
    return dict(win="#14141c", card="#1e1e28", cardbd="#2a2a36", fg="#eef0f6",
                muted="#8b8fa3", clock="#8b8fa3", pill="#2b2b38", pillfg="#c9ccda",
                track=(46, 46, 60), sep="#242430", ft="#4d4d59")


_IMGS = []  # keep PhotoImage refs alive


def _bar_photo(pct, key, track_rgb, W=150, H=8):
    from PIL import Image, ImageDraw, ImageTk
    c1, c2 = tuple(int(GRAD[key][0][i:i+2], 16) for i in (1, 3, 5)), tuple(int(GRAD[key][1][i:i+2], 16) for i in (1, 3, 5))
    S = 3
    im = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    rad = H * S / 2
    d.rounded_rectangle([0, 0, W * S - 1, H * S - 1], radius=rad, fill=track_rgb + (255,))
    fw = max(H * S, int(W * S * min(100.0, max(0.0, pct)) / 100))
    grad = Image.new("RGBA", (fw, H * S), (0, 0, 0, 0))
    px = grad.load()
    for x in range(fw):
        t = x / max(1, fw - 1)
        col = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
        for y in range(H * S):
            px[x, y] = col + (255,)
    mask = Image.new("L", (fw, H * S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, fw - 1, H * S - 1], radius=rad, fill=255)
    im.paste(grad, (0, 0), mask)
    ph = ImageTk.PhotoImage(im.resize((W, H), Image.LANCZOS))
    _IMGS.append(ph)
    return ph


def _dot_photo(hex_color, D=9):
    from PIL import Image, ImageDraw, ImageTk
    rgb = tuple(int(hex_color[i:i+2], 16) for i in (1, 3, 5))
    S = 4
    im = Image.new("RGBA", (D * S, D * S), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse([0, 0, D * S - 1, D * S - 1], fill=rgb + (255,))
    ph = ImageTk.PhotoImage(im.resize((D, D), Image.LANCZOS))
    _IMGS.append(ph)
    return ph


_LAST_PANEL_DATA = {"value": None}


def _panel_data():
    """Read cached state for the panel. Never collects.

    This runs on the panel host's WPF dispatcher thread, so it must not block.
    It used to fall back to fleet.collect_api() on any exception, which meant a
    torn read of state.json -- easy to hit, since poll() rewrites it underneath --
    turned a click into four synchronous provider requests with the UI frozen
    behind them. The tray owns collection; the panel only ever reads.
    """
    for attempt in range(3):
        try:
            state = json.loads(STATE.read_text())
            _LAST_PANEL_DATA["value"] = (state["data"], state["ts"])
            return _LAST_PANEL_DATA["value"]
        except (OSError, ValueError, KeyError, TypeError):
            if attempt < 2:
                time.sleep(0.01)
    if _LAST_PANEL_DATA["value"] is not None:
        return _LAST_PANEL_DATA["value"]
    return [], time.time()


def run_panel():
    import tkinter as tk
    import ctypes
    data, ts = _panel_data()

    light = _os_light()
    try:
        from panel_wpf import show_panel
        show_panel(data, ts, light, fleet)
        return
    except Exception as e:
        fleet.dbg("WPF panel fallback", e)

    p = _tkpal(light)
    root = tk.Tk()
    root.overrideredirect(True)            # borderless => NO taskbar button, NO alt-tab, instant
    root.attributes("-topmost", True)
    root.configure(bg=p["win"])
    OUT = 9                                # outer padding (window bg frame)
    wrap = tk.Frame(root, bg=p["win"])
    wrap.pack(fill="both", expand=True, padx=OUT, pady=OUT)

    hd = tk.Frame(wrap, bg=p["win"])
    hd.pack(fill="x", pady=(0, 4))
    tk.Label(hd, text="◆ FLEET", bg=p["win"], fg=p["fg"], font=("Segoe UI Semibold", 12)).pack(side="left")
    tk.Label(hd, text=time.strftime("%H:%M", time.localtime(ts)), bg=p["win"], fg=p["clock"],
             font=("Segoe UI", 9)).pack(side="right")

    for d in data:
        acc = ACCENT.get(d["agent"], "#8b8fa3")
        card = tk.Frame(wrap, bg=p["card"], highlightbackground=p["cardbd"], highlightthickness=1)
        card.pack(fill="x", pady=4)
        top = tk.Frame(card, bg=p["card"])
        top.pack(fill="x", padx=12, pady=(9, 4))
        tk.Label(top, image=_dot_photo(acc), bg=p["card"]).pack(side="left")
        tk.Label(top, text="  " + d["agent"], bg=p["card"], fg=p["fg"],
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        if d.get("plan"):
            tk.Label(top, text=f" {d['plan']} ", bg=p["pill"], fg=p["pillfg"],
                     font=("Segoe UI", 8)).pack(side="right")
        if not d["metrics"]:
            tk.Label(card, text=d["err"] or "—", bg=p["card"], fg=p["muted"], anchor="w",
                     font=("Segoe UI", 8)).pack(fill="x", padx=12, pady=(0, 9))
            continue
        for m in d["metrics"]:
            row = tk.Frame(card, bg=p["card"])
            row.pack(fill="x", padx=12, pady=2)
            tk.Label(row, text=m["label"], bg=p["card"], fg=p["muted"], width=10, anchor="w",
                     font=("Segoe UI", 9)).pack(side="left")
            if m["pct"] is not None:
                tk.Label(row, image=_bar_photo(m["pct"], _sevkey(m["pct"]), p["track"]),
                         bg=p["card"]).pack(side="left", padx=(2, 8))
                tk.Label(row, text=f"{m['pct']:.0f}%", bg=p["card"], fg=p["fg"], width=4, anchor="e",
                         font=("Segoe UI Semibold", 9)).pack(side="left")
                if m.get("resets"):
                    tk.Label(row, text=fleet._until(m["resets"]), bg=p["card"], fg=p["muted"],
                             font=("Segoe UI", 8)).pack(side="right")
            else:
                tk.Label(row, text=m["extra"] or "—", bg=p["card"], fg=p["fg"], anchor="w",
                         font=("Segoe UI", 9)).pack(side="left", padx=2)
        if d.get("err"):
            tk.Label(card, text=d["err"], bg=p["card"], fg="#c2831c", anchor="w",
                     font=("Segoe UI", 7)).pack(fill="x", padx=12, pady=(0, 6))
        tk.Frame(card, bg=p["card"], height=3).pack()

    tk.Label(wrap, text="auto-refresh 120s · click away to close", bg=p["win"], fg=p["ft"],
             font=("Segoe UI", 7)).pack(pady=(4, 0))

    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw = root.winfo_screenwidth()
    sh_work = root.winfo_screenheight()
    X, Y = sw - w - 8, sh_work - h - 56
    RISE = 10                                   # px of travel, like Flow Launcher
    root.attributes("-alpha", 0.0)              # start invisible; fade in below
    root.geometry(f"{w}x{h}+{X}+{Y + RISE}")

    def _round():
        try:
            hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
            ww, hh = root.winfo_width(), root.winfo_height()
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, ww + 1, hh + 1, 20, 20)
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)           # clip to rounded shape
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
        except Exception:
            pass
    root.after(10, _round)

    # --- Flow-Launcher-style motion: soft fade + rise in, fade + settle out ---
    FIN, FOUT, STEP = 130.0, 110.0, 12          # ms
    MAXA = 0.985
    closing = {"v": False}

    def _ease_out(t):                            # cubic ease-out: fast start, soft landing
        return 1 - (1 - t) ** 3

    def fade_in(t0=None):
        t0 = t0 if t0 is not None else time.perf_counter()
        el = (time.perf_counter() - t0) * 1000
        k = _ease_out(min(1.0, el / FIN))
        root.attributes("-alpha", MAXA * k)
        root.geometry(f"+{X}+{int(Y + RISE * (1 - k))}")
        if k < 1.0:
            root.after(STEP, lambda: fade_in(t0))

    def fade_out(t0=None):
        if t0 is None:
            if closing["v"]:
                return
            closing["v"] = True
            t0 = time.perf_counter()
        el = (time.perf_counter() - t0) * 1000
        k = min(1.0, el / FOUT)
        root.attributes("-alpha", MAXA * (1 - k))
        root.geometry(f"+{X}+{int(Y + RISE * 0.6 * k)}")
        if k < 1.0:
            root.after(STEP, lambda: fade_out(t0))
        else:
            root.destroy()

    root.after(0, lambda: fade_in())
    root.after(40, root.focus_force)
    if not os.environ.get("FLEET_PANEL_STAY"):
        root.bind("<FocusOut>", lambda e: fade_out())
        root.bind("<Escape>", lambda e: fade_out())
        root.bind("<Button-1>", lambda e: fade_out())
    root.mainloop()


def _process_alive(pid):
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def _panel_host_alive():
    try:
        return _process_alive(int(PANEL_PID.read_text()))
    except Exception:
        return False


def ensure_panel_host(show=False, icon=None):
    if _panel_host_alive():
        if show:
            _write_panel_signal(icon)
        return
    for path in (PANEL_PID, PANEL_QUIT):
        try:
            path.unlink()
        except OSError:
            pass
    if not show:
        try:
            PANEL_SIGNAL.unlink()
        except OSError:
            pass
    subprocess.Popen(
        _panel_host_command(),
        creationflags=0x08000000,
    )
    if show:
        _write_panel_signal(icon)


def open_panel(icon=None):
    ensure_panel_host(show=True, icon=icon)


def run_panel_host():
    PANEL_PID.write_text(str(os.getpid()))
    try:
        from panel_wpf import run_panel_host as host
        host(_panel_data, _os_light, fleet, PANEL_SIGNAL, PANEL_QUIT)
    finally:
        try:
            if PANEL_PID.read_text().strip() == str(os.getpid()):
                PANEL_PID.unlink()
        except OSError:
            pass


def quit_all(icon):
    try:
        PANEL_QUIT.write_text(str(time.time_ns()))
    except OSError:
        pass
    icon.stop()


# ---------- tray loop ----------
_POLL_LOCK = threading.Lock()


def poll(block=True, force_claude=False):
    if not _POLL_LOCK.acquire(blocking=block):
        return _panel_data()[0]
    try:
        data = fleet.collect_api(force_claude=force_claude) if force_claude else fleet.collect_api()
        # Atomic replace: a plain write_text leaves a window where the panel host
        # can read a half-written file.
        payload = json.dumps({"data": data, "ts": time.time()})
        staging = STATE.with_name(STATE.name + ".tmp")
        staging.write_text(payload)
        os.replace(staging, STATE)
        return data
    except Exception as e:
        fleet.dbg("poll", e)
        return _panel_data()[0]
    finally:
        _POLL_LOCK.release()


def build_menu():
    import pystray
    return pystray.Menu(
        pystray.MenuItem("Toggle panel", lambda i, it: open_panel(i), default=True),
        pystray.MenuItem("Refresh now", lambda i, it: refresh(i)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda i, it: quit_all(i)),
    )


_data = []


def apply(icon, frame=0):
    cycle = usage_cycle(_data)
    icon.icon = make_icon_image(*cycle[frame % len(cycle)])
    icon.title = tooltip(_data)


def refresh(icon):
    global _data
    _data = poll(block=False, force_claude=True)
    apply(icon)


def poll_loop():
    global _data
    while True:
        _data = poll()
        time.sleep(REFRESH_SECS)


def loop(icon):
    icon.visible = True
    threading.Thread(target=poll_loop, daemon=True).start()
    frame = 0
    while True:
        apply(icon, frame)
        frame += 1
        time.sleep(CYCLE_SECS)


def main():
    global _data
    CACHE.mkdir(parents=True, exist_ok=True)
    if "--panel" in sys.argv:
        run_panel()
        return
    if "--panel-host" in sys.argv:
        if not claim_single_instance(r"Local\fleet-panel-host"):
            return
        run_panel_host()
        return
    if "--dry" in sys.argv:
        _data = poll()
        print("tooltip:", tooltip(_data))
        for d in _data:
            print(f"- {d['agent']} [{d.get('plan','')}]",
                  [(m["label"], m["pct"]) for m in d["metrics"]] or d["err"])
        return
    if not claim_single_instance(r"Local\fleet-tray"):
        return
    import pystray
    ensure_panel_host()
    _data, _ = _panel_data()
    icon = pystray.Icon(
        "fleet",
        make_icon_image(*usage_cycle(_data)[0]),
        tooltip(_data),
        build_menu(),
    )
    icon.run(setup=loop)


if __name__ == "__main__":
    main()
