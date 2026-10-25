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
import atexit, ctypes, ctypes.wintypes, faulthandler, json, logging, os, subprocess, sys, threading, time, traceback
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet
import ledger
import panel_model
import usage_hub
import alerts
import settings as prefs
import tray_glyph

HERE = Path(__file__).resolve().parent
_BUNDLED = bool(getattr(sys, "frozen", False))
DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Fleet" if _BUNDLED else HERE
CACHE = DATA_ROOT / ".cache"
STATE = CACHE / "state.json"
PYW = HERE / ".venv" / "Scripts" / "pythonw.exe"
PANEL_PID = CACHE / "panel-host.pid"
PANEL_SIGNAL = CACHE / "panel.show"
PANEL_QUIT = CACHE / "panel.quit"
TRAY_HEARTBEAT = CACHE / "tray-heartbeat.json"
PANEL_HEARTBEAT = CACHE / "panel-heartbeat.json"
PAUSED = CACHE / "paused"
REFRESH_SIGNAL = CACHE / "refresh.request"
REFRESH_SOON = CACHE / "refresh.soon"
# The native panel touches this while it is on screen; readings then refresh faster.
PANEL_VISIBLE = CACHE / "panel.visible"
PANEL_MODEL = CACHE / "panel.json"
_WM_HEARTBEAT = 0x8001
# Posted straight to the native panel's window (must match PanelWindow.cs).
WM_FLEET_TOGGLE = 0x8000 + 0x46
WM_FLEET_DATA = 0x8000 + 0x47
WM_FLEET_SETTINGS = 0x8000 + 0x48
WM_FLEET_DASHBOARD = 0x8000 + 0x49
_LAST_POLL = time.time()
_UI_ACK = {"ts": 0.0, "registered": False}
_FAULT_FILE = None
REFRESH_SECS = 30
OPEN_REFRESH_SECS = 10
PANEL_VISIBLE_SECS = 6
PANEL_STALE_SECS = 35
TICK_SECS = 2
RESTART_SECS = 2
MARK = {"Claude": "CL", "Codex": "CX", "Grok": "GR", "Antigrav": "AG"}

# palette (dark card -> readable regardless of OS light/dark theme)
BG, CARD, FG, MUTED, TRACK = "#15151d", "#1e1e28", "#e8e8f0", "#9a9aa8", "#33333f"
ACCENT = {"Claude": "#f59e0b", "Codex": "#22d3ee", "Grok": "#d946ef", "Antigrav": "#22c55e"}
# API-key and custom providers are named by the user, so their mark and colour
# are derived from the name instead of a table.
API_ACCENT = ["#60a5fa", "#34d399", "#fbbf24", "#a78bfa", "#f472b6", "#38bdf8", "#fb923c", "#94a3b8"]
_SHUTDOWN = threading.Event()
_RUNTIME_LOGGER = logging.getLogger("fleet.runtime")


def _configure_runtime_logging():
    """Keep windowless failures inspectable without recording provider data."""
    if any(getattr(handler, "_fleet_runtime", False) for handler in _RUNTIME_LOGGER.handlers):
        return
    try:
        role = "panel" if "--panel-host" in sys.argv else "tray"
        handler = RotatingFileHandler(
            CACHE / f"{role}.log",
            maxBytes=256 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
    except OSError:
        return
    handler._fleet_runtime = True
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    for name in ("fleet.runtime", "pystray"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
    global _FAULT_FILE
    fault_path = CACHE / f"{role}-fault.log"
    try:
        if fault_path.exists() and fault_path.stat().st_size > 256 * 1024:
            fault_path.replace(fault_path.with_suffix(".previous.log"))
        _FAULT_FILE = fault_path.open("a", encoding="utf-8")
        # faulthandler records no timestamps, which left every past native crash
        # undatable. Stamp the session before arming it.
        _FAULT_FILE.write(
            "\n=== %s role=%s pid=%s ===\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), role, os.getpid())
        )
        _FAULT_FILE.flush()
        faulthandler.enable(_FAULT_FILE)
    except OSError as exc:
        _log_runtime_exception("fault log unavailable", exc)
    threading.excepthook = lambda args: _log_runtime_exception("thread failed", args.exc_value)
    atexit.register(lambda: _RUNTIME_LOGGER.info("process exiting pid=%s", os.getpid()))


def _log_runtime(message):
    _RUNTIME_LOGGER.warning(message)


def _log_runtime_exception(message, exc):
    frames = traceback.extract_tb(exc.__traceback__)
    location = " <- ".join(
        f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in frames[-8:]
    )
    _RUNTIME_LOGGER.error(
        "%s [%s]%s",
        message,
        type(exc).__name__,
        f" at {location}" if location else "",
    )


def _atomic_write(path, text, retries=4):
    """Replace `path` in one step, tolerating a reader that holds the old name.

    Windows opens files without FILE_SHARE_DELETE by default, so any process
    reading the destination -- the panel host, the recovery guard, an on-access
    scanner -- makes os.replace fail with WinError 5 for as long as it holds a
    handle. Those losses last milliseconds and are not a Fleet fault, so retry
    before reporting one.
    """
    staging = path.with_name(path.name + ".tmp")
    for attempt in range(retries):
        try:
            staging.write_text(text)
            os.replace(staging, path)
            return
        except OSError:
            if attempt + 1 >= retries:
                raise
            time.sleep(0.05 * (attempt + 1))


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


def ensure_tray_registration(icon):
    """Re-add an icon that the notification area has silently forgotten."""
    if _tray_icon_rect(icon) is not None:
        return True
    try:
        # pystray's regular image/title updates use NIM_MODIFY, which cannot
        # recreate a missing shell entry. Its TaskbarCreated handler uses the
        # same backend operation, but the shell can lose an entry without an
        # Explorer restart or a corresponding broadcast.
        icon._show()
    except Exception as e:
        fleet.dbg("tray registration recovery", type(e).__name__)
        return False
    return _tray_icon_rect(icon) is not None


def _write_panel_signal(icon=None):
    anchor = _tray_icon_rect(icon) if icon is not None else None
    payload = {
        "ts": time.time_ns(),
        "anchor": list(anchor) if anchor else None,
    }
    staging = PANEL_SIGNAL.with_suffix(".tmp")
    staging.write_text(json.dumps(payload))
    os.replace(staging, PANEL_SIGNAL)


def _native_panel():
    """FleetPanel.exe beside Fleet.exe, or the local build in development."""
    candidates = [Path(sys.executable).parent / "FleetPanel.exe"] if _BUNDLED else [
        HERE / "native" / "FleetPanel" / "bin" / "Release" / "net48" / "FleetPanel.exe"
    ]
    if os.environ.get("FLEET_PANEL") == "python":
        return None
    return next((path for path in candidates if path.exists()), None)


def _panel_host_command():
    """The native panel when present; otherwise the Python/WPF host."""
    native = _native_panel()
    if native is not None:
        return [str(native), "--cache", str(CACHE)]
    if _BUNDLED:
        return [sys.executable, "--panel-host"]
    exe = str(PYW) if PYW.exists() else "pythonw"
    return [exe, str(HERE / "tray.py"), "--panel-host"]


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


# ---------- popup panel ----------


def _os_light():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return bool(v)
    except Exception:
        return False





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
    from panel_wpf import show_panel
    data, ts = _panel_data()
    show_panel(data, ts, _os_light(), fleet)


_PANEL_PROCESS = None
_PANEL_START = 0.0
_PANEL_LOCK = threading.Lock()
# The last window the panel reported, for the moments its heartbeat is mid-replace.
_PANEL_HWND = 0
_PANEL_LATE = False


def _panel_heartbeat_age():
    """Seconds since the panel's last beat, or None if the file cannot be read.

    The panel swaps its heartbeat in with .NET's File.Replace, which leaves the
    name missing for a moment; a stat landing there raised FileNotFoundError and
    used to restart a healthy panel. A short retry outlasts the swap.
    """
    for attempt in range(3):
        try:
            return time.time() - PANEL_HEARTBEAT.stat().st_mtime
        except OSError:
            if attempt < 2:
                time.sleep(0.05)
    return None


def _panel_responds(timeout_ms=3000):
    """Ask the panel's UI thread directly: a hung thread cannot answer WM_NULL."""
    hwnd = _panel_window()
    if not hwnd:
        return False
    send = ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes = (ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
                     ctypes.wintypes.UINT, ctypes.wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))
    send.restype = ctypes.wintypes.LPARAM
    result = ctypes.c_size_t()
    SMTO_ABORTIFHUNG = 0x0002
    return bool(send(hwnd, 0, 0, 0, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)))


def ensure_panel_host(show=False, icon=None):
    """Own a process handle, never trust a stale or recycled PID file."""
    global _PANEL_PROCESS, _PANEL_START, _PANEL_HWND, _PANEL_LATE
    with _PANEL_LOCK:
        alive = _PANEL_PROCESS is not None and _PANEL_PROCESS.poll() is None
        if alive:
            # The host beats every 10s, so three missed beats is already
            # conclusive. The old 90s window meant a native panel fault left
            # a dead flyout behind for most of two minutes.
            age = _panel_heartbeat_age()
            stale = age is None or age > PANEL_STALE_SECS
            if not stale:
                _PANEL_LATE = False
            elif time.monotonic() - _PANEL_START > PANEL_STALE_SECS:
                # A late file is only a hint. A window that still answers takes
                # clicks, so it is kept; only a silent one is replaced.
                if _panel_responds():
                    if not _PANEL_LATE:
                        _log_runtime("panel heartbeat late but its window responds; keeping it")
                    _PANEL_LATE = True
                else:
                    _log_runtime("panel heartbeat stalled; restarting")
                    _PANEL_PROCESS.terminate()
                    _PANEL_PROCESS.wait(timeout=5)
                    alive = False
        if not alive:
            # ponytail: rate-limit a broken host instead of spawning on every click.
            if time.monotonic() - _PANEL_START < 10:
                return
            for path in (PANEL_PID, PANEL_QUIT, PANEL_SIGNAL, PANEL_HEARTBEAT):
                path.unlink(missing_ok=True)
            _PANEL_HWND = 0
            _PANEL_LATE = False
            _PANEL_START = time.monotonic()
            _PANEL_PROCESS = subprocess.Popen(
                _panel_host_command(), creationflags=0x08000000,
                env=os.environ | {"FLEET_PARENT_PID": str(os.getpid())},
            )
            _RUNTIME_LOGGER.info("panel launched pid=%s", _PANEL_PROCESS.pid)
        if show:
            _allow_panel_foreground(_PANEL_PROCESS.pid)
            _write_panel_signal(icon)


def _allow_panel_foreground(pid):
    """Pass the tray click's foreground right on to the panel host.

    The shell lets the owner of a clicked notification icon take the foreground.
    The flyout lives in the panel host, which never saw the click, and Windows
    rarely lets such a process activate a window -- so the flyout opened without
    keyboard focus (Escape did nothing) and without its active glass.
    """
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(int(pid))
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def _panel_window():
    """The native panel's window handle, if it belongs to the host this tray owns.

    The panel writes its handle into its heartbeat. A stale file, a recycled pid
    or a handle that now belongs to another process are all refused, so a click
    can never be posted to the wrong window. While the file is mid-replace the
    last handle it named is used, still checked against the host's pid.
    """
    global _PANEL_HWND
    process = _PANEL_PROCESS
    if process is None or process.poll() is not None:
        return None
    try:
        beat = json.loads(PANEL_HEARTBEAT.read_text())
        hwnd = int(beat.get("hwnd") or 0)
        if int(beat.get("pid") or 0) != process.pid:
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        hwnd = _PANEL_HWND
    if not hwnd:
        return None
    owner = ctypes.wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.wintypes.HWND(hwnd), ctypes.byref(owner))
    if owner.value != process.pid:
        return None
    _PANEL_HWND = hwnd
    return hwnd


def _post_panel(message, wparam=0, lparam=0):
    hwnd = _panel_window()
    if not hwnd:
        return False
    post = ctypes.windll.user32.PostMessageW
    post.argtypes = (ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)
    post.restype = ctypes.wintypes.BOOL
    return bool(post(hwnd, message, wparam, lparam))


def open_panel(icon=None, page=None):
    """The click path: one window message, no files, no waiting on a timer.

    The panel finds the icon's rectangle itself from the tray window passed in
    wParam; lParam carries the click's tick count so the panel can log latency.
    page="settings" opens straight onto the settings page.
    """
    process = _PANEL_PROCESS
    if process is not None and process.poll() is None:
        tray = int(getattr(icon, "_hwnd", 0) or 0)
        _allow_panel_foreground(process.pid)
        message = WM_FLEET_DASHBOARD if page == "dashboard" else WM_FLEET_SETTINGS if page == "settings" else WM_FLEET_TOGGLE
        if _post_panel(message, tray, ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF):
            return
    if page == "dashboard":
        ensure_panel_host()
        (CACHE / "dashboard.show").write_text(str(time.time_ns()))
    else:
        ensure_panel_host(show=True, icon=icon)


def run_panel_host():
    # Keep a handle to the actual parent: no orphan flyout after a tray crash.
    parent_pid = os.environ.get("FLEET_PARENT_PID")
    if parent_pid:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD)
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD)
        handle = kernel32.OpenProcess(0x00100000, False, int(parent_pid))
        if not handle:
            return
        def watch_parent():
            if kernel32.WaitForSingleObject(handle, 0xFFFFFFFF) == 0:
                os._exit(0)
        threading.Thread(target=watch_parent, daemon=True, name="fleet-parent").start()
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
    # Explicit quit remains stopped until Fleet is opened again.
    PAUSED.write_text("quit\n")
    _RUNTIME_LOGGER.info("user requested quit")
    _SHUTDOWN.set()
    run_stop = getattr(icon, "_fleet_stop_event", None)
    if run_stop is not None:
        run_stop.set()
    try:
        PANEL_QUIT.write_text(str(time.time_ns()))
    except OSError:
        pass
    icon.stop()


# ---------- tray loop ----------
_POLL_LOCK = threading.Lock()


_MODEL = {}
_MODEL_LOCK = threading.Lock()
_PENDING_ALERTS = []
ALERTS = CACHE / "alerts.json"
# The ledger reads the agents' local logs: a few seconds of work, then rest. The
# first read of a month of logs takes a few rounds; after that a round is new lines.
LEDGER_BUDGET_SECS = 4.0
LEDGER_CATCHUP_SECS = 2
LEDGER_OPEN_SECS = 15
LEDGER_IDLE_SECS = 60
# Only the running tray opens the ledger (main() enables it); tests and --dry never do.
_LEDGER = {"instance": None, "enabled": False}
_LEDGER_LOCK = threading.Lock()


def _ledger():
    with _LEDGER_LOCK:
        if _LEDGER["instance"] is None and _LEDGER["enabled"]:
            _LEDGER["instance"] = ledger.Ledger()
        return _LEDGER["instance"]


def _usage(data, current):
    """The ledger's figures for this poll, or None when cost is off or it failed."""
    book = _ledger()
    if current.get("cost") is False or book is None:
        return None
    try:
        return book.snapshot(spans=panel_model.window_spans(data))
    except Exception as exc:
        _log_runtime_exception("ledger snapshot failed", exc)
        return None


def _limit_rates(model, ts):
    """Record this poll's live readings and read back how fast each limit is filling."""
    rates = {}
    book = _ledger()
    if book is None:
        return rates
    try:
        for provider in model.get("providers") or []:
            if provider.get("freshness"):
                continue  # a cached or borrowed reading is not a new sample
            rows = [{"label": row["key"], "pct": row["pct"], "reset_at": row.get("reset_at")}
                    for group in provider.get("groups") or [] for row in group.get("rows") or []
                    if row.get("pct") is not None and row.get("key")]
            if not rows:
                continue
            book.record_limits(provider["id"], rows, ts)
            rates[provider["id"]] = {row["label"]: book.limit_rate(provider["id"], row["label"], ts) for row in rows}
    except Exception as exc:
        _log_runtime_exception("limit history failed", exc)
    return rates


def _write_panel_model(data, ts):
    """What the native panel draws; built here so the rules stay in tested Python."""
    global _MODEL
    with _MODEL_LOCK:
        try:
            current = prefs.load()
            usage = _usage(data, current)
            accounts = fleet.apikeys.account_status(fleet.apikeys.load_config(fleet._ROOT)[0])
            model = panel_model.build(data, ts, settings=current, usage=usage, accounts=accounts)
            rates = _limit_rates(model, ts)
            if rates:
                model = panel_model.build(data, ts, settings=current, usage=usage, rates=rates, accounts=accounts)
            _atomic_write(STATE.with_name(PANEL_MODEL.name), json.dumps(model))
            _MODEL = model
        except Exception as exc:
            _log_runtime_exception("panel model write failed", exc)
            return
    try:
        found, state = alerts.evaluate(model, current, alerts.load_state(ALERTS))
        alerts.save_state(ALERTS, state)
        _PENDING_ALERTS.extend(found[:3])
    except Exception as exc:
        _log_runtime_exception("alert check failed", exc)
    # The panel lays new data out at once, while hidden, so opening never waits.
    _post_panel(WM_FLEET_DATA)


def _panel_visible():
    try:
        return time.time() - PANEL_VISIBLE.stat().st_mtime < PANEL_VISIBLE_SECS
    except OSError:
        return False


def poll(block=True, force_claude=False):
    global _LAST_POLL
    if not _POLL_LOCK.acquire(blocking=block):
        return _panel_data()[0]
    try:
        data = fleet.collect_api(force_claude=force_claude) if force_claude else fleet.collect_api()
        # Atomic replace with retries: the panel host reads this file on a timer
        # and a plain write leaves a window where it sees a half-written state.
        ts = time.time()
        _atomic_write(STATE, json.dumps({"data": data, "ts": ts}))
        _write_panel_model(data, ts)
        return data
    except Exception as e:
        _log_runtime_exception("collection failed; retaining cached readings", e)
        return _panel_data()[0]
    finally:
        _LAST_POLL = time.time()
        _POLL_LOCK.release()


def _toggle_mute():
    current = prefs.load()
    prefs.save({"mute_until": 0 if prefs.muted(current) else prefs.end_of_today()})


def build_menu():
    import pystray
    return pystray.Menu(
        pystray.MenuItem("Open Fleet", lambda i, it: open_panel(i), default=True),
        pystray.MenuItem("Dashboard", lambda i, it: open_panel(i, page="dashboard"),
                         visible=lambda it: _native_panel() is not None),
        pystray.MenuItem("Refresh now", lambda i, it: threading.Thread(target=refresh, args=(i,), daemon=True).start()),
        pystray.MenuItem("Mute alerts for today", lambda i, it: _toggle_mute(),
                         checked=lambda it: prefs.muted(prefs.load())),
        pystray.MenuItem("Settings", lambda i, it: open_panel(i, page="settings")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Fleet", lambda i, it: quit_all(i)),
    )


_data = []


_SETTINGS_SEEN = {"mtime": None}


def _settings_changed():
    """The flyout saved new settings: rebuild what depends on them."""
    try:
        mtime = prefs.PATH.stat().st_mtime
    except OSError:
        mtime = None
    changed = _SETTINGS_SEEN["mtime"] is not None and mtime != _SETTINGS_SEEN["mtime"]
    _SETTINGS_SEEN["mtime"] = mtime
    return changed


def apply(icon):
    if _settings_changed() and _data:
        _write_panel_model(_data, _LAST_POLL or time.time())
    current = prefs.load()
    model = _MODEL or panel_model.build(_data, time.time(), settings=current)
    glyph = {"spec": tray_glyph.spec(model, current), "light": tray_glyph.taskbar_is_light(),
             "size": tray_glyph.small_icon_size()}
    if glyph != getattr(icon, "fleet_glyph", None):
        icon.fleet_glyph = glyph
        icon.icon = tray_glyph.draw(glyph["spec"], 48, glyph["light"])
    title = tray_glyph.tooltip(model, current)
    if icon.title != title:
        icon.title = title
    while _PENDING_ALERTS:
        title, body = _PENDING_ALERTS.pop(0)
        try:
            icon.notify(body, title)
        except Exception as exc:
            _log_runtime_exception("alert could not be shown", exc)
    ensure_tray_registration(icon)


def refresh(icon):
    global _data
    _data = poll(block=False, force_claude=True)
    apply(icon)


def _lower_thread_priority():
    """Below normal: reading logs must never compete with the user's own work."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
    except (AttributeError, OSError):
        pass


def ledger_loop(stop_event=None):
    """Keep the API-equivalent figures current from the agents' local logs."""
    stop_event = stop_event or _SHUTDOWN
    _lower_thread_priority()
    complete = None
    while not stop_event.is_set():
        try:
            stats = _ledger().update(budget=LEDGER_BUDGET_SECS) or {}
        except Exception as exc:
            _log_runtime_exception("ledger update failed", exc)
            stats = {"complete": True}
        done = bool(stats.get("complete"))
        if done and complete is False:
            _log_runtime(f"ledger caught up with the local logs ({stats.get('files', 0)} files this round)")
        # New lines, or the first read just finished: the flyout's figures moved.
        if (stats.get("events") or done != complete) and _data:
            _write_panel_model(_data, _LAST_POLL or time.time())
        complete = done
        wait = LEDGER_CATCHUP_SECS if not done else LEDGER_OPEN_SECS if _panel_visible() else LEDGER_IDLE_SECS
        if stop_event.wait(wait):
            return


def poll_loop(stop_event=None):
    global _data
    stop_event = stop_event or threading.Event()
    force_refresh = False
    while not stop_event.is_set():
        _data = poll(force_claude=force_refresh)
        force_refresh = False
        started = time.monotonic()
        while True:
            if stop_event.wait(1):
                return
            if REFRESH_SIGNAL.exists():
                REFRESH_SIGNAL.unlink(missing_ok=True)
                force_refresh = True
                break
            if REFRESH_SOON.exists():
                # The flyout opened: a fresh poll, but paced. Only Refresh now forces.
                REFRESH_SOON.unlink(missing_ok=True)
                break
            interval = OPEN_REFRESH_SECS if _panel_visible() else REFRESH_SECS
            if time.monotonic() - started >= interval:
                break


def _heartbeat(icon):
    """Acknowledged on the Windows UI thread, so a stuck message loop goes stale.

    Memory only. Writing the file here put disk I/O on the message pump, and a
    single lost rename race left the recovery guard reading a stale heartbeat
    and killing a healthy tray.
    """
    _UI_ACK["ts"] = time.time()
    _UI_ACK["registered"] = _tray_icon_rect(icon) is not None
    return 0


def _publish_heartbeat():
    """Publish the last UI acknowledgement from the update thread."""
    if not _UI_ACK["ts"]:
        return
    payload = {"pid": os.getpid(), "ts": _UI_ACK["ts"], "poll_ts": _LAST_POLL,
               "registered": _UI_ACK["registered"]}
    try:
        _atomic_write(TRAY_HEARTBEAT, json.dumps(payload))
    except OSError as exc:
        _log_runtime_exception("tray heartbeat unavailable", exc)


def loop(icon, stop_event=None):
    stop_event = stop_event or threading.Event()
    icon._fleet_stop_event = stop_event
    try:
        icon.visible = True
        tick = 0
        while not stop_event.is_set():
            if PAUSED.exists():
                quit_all(icon)
                return
            apply(icon)
            ctypes.windll.user32.PostMessageW(ctypes.wintypes.HWND(icon._hwnd), _WM_HEARTBEAT, 0, 0)
            if tick % 5 == 0:
                ensure_panel_host()
            _publish_heartbeat()
            tick += 1
            if stop_event.wait(TICK_SECS):
                return
    except Exception as exc:
        _log_runtime_exception("tray update loop failed", exc)
        stop_event.set()
        icon.stop()


def _icon_class():
    """pystray's icon, loading our pixel-exact frames at the taskbar's real size.

    pystray loads its icon at the large-icon size and lets the shell shrink it,
    which blurs a 16 px meter. Here every frame is drawn at its own size and the
    small-icon size is loaded directly.
    """
    import pystray
    from pystray._util import win32 as pwin32

    class FleetIcon(pystray.Icon):
        fleet_glyph = None

        def _assert_icon_handle(self):
            if self._icon_handle:
                return
            glyph = self.fleet_glyph
            if glyph is None:
                return super()._assert_icon_handle()
            size = glyph.get("size") or 16
            path = CACHE / "tray-icon.ico"
            tray_glyph.write_ico(glyph["spec"], path, glyph["light"],
                                 sizes=tuple(sorted(set(tray_glyph.FRAME_SIZES) | {size})))
            self._icon_handle = pwin32.LoadImage(None, str(path), pwin32.IMAGE_ICON, size, size,
                                                 pwin32.LR_LOADFROMFILE)

    return FleetIcon


def _new_tray_icon():
    current = prefs.load()
    model = _MODEL or panel_model.build(_data, time.time(), settings=current)
    glyph = {"spec": tray_glyph.spec(model, current), "light": tray_glyph.taskbar_is_light(),
             "size": tray_glyph.small_icon_size()}
    icon = _icon_class()("fleet", tray_glyph.draw(glyph["spec"], 48, glyph["light"]),
                         tray_glyph.tooltip(model, current), build_menu())
    icon.fleet_glyph = glyph
    icon._message_handlers[_WM_HEARTBEAT] = lambda w, l: _heartbeat(icon)
    return icon


def run_tray(stop_event=None, icon_factory=None):
    """Recreate the tray when pystray's Windows message loop ends unexpectedly."""
    stop_event = stop_event or _SHUTDOWN
    icon_factory = icon_factory or _new_tray_icon
    while not stop_event.is_set():
        run_stop = threading.Event()
        try:
            icon = icon_factory()
            icon._fleet_stop_event = run_stop
            icon.run(setup=lambda current, stopping=run_stop: loop(current, stopping))
        except Exception as exc:
            _log_runtime_exception("tray message loop failed", exc)
        finally:
            run_stop.set()
        if stop_event.is_set():
            return
        _log_runtime("tray message loop ended; restarting")
        if stop_event.wait(RESTART_SECS):
            return


def _enable_dpi_awareness():
    """Per-monitor DPI awareness: the icon is drawn for the taskbar's real pixels."""
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def main():
    global _data
    CACHE.mkdir(parents=True, exist_ok=True)
    if "--quit" in sys.argv:
        PAUSED.write_text("quit\n")
        return
    _configure_runtime_logging()
    role = "panel-host" if "--panel-host" in sys.argv else "panel" if "--panel" in sys.argv else "tray"
    _RUNTIME_LOGGER.info("process started role=%s pid=%s", role, os.getpid())
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
        current = prefs.load()
        print("tooltip:", tray_glyph.tooltip(panel_model.build(_data, time.time(), settings=current), current))
        for d in _data:
            print(f"- {d['agent']} [{d.get('plan','')}]",
                  [(m["label"], m["pct"]) for m in d["metrics"]] or d["err"])
        return
    if not claim_single_instance(r"Local\fleet-tray"):
        return
    _enable_dpi_awareness()
    PAUSED.unlink(missing_ok=True)
    try:
        usage_hub.start_usage_hub(CACHE, _SHUTDOWN, logger=_RUNTIME_LOGGER)
    except Exception as exc:
        _log_runtime_exception("T3 usage hub failed to start", exc)
    _data, started_ts = _panel_data()
    if _data:
        _write_panel_model(_data, started_ts)
    ensure_panel_host()
    threading.Thread(target=poll_loop, args=(_SHUTDOWN,), daemon=True, name="fleet-poll").start()
    _LEDGER["enabled"] = True
    threading.Thread(target=ledger_loop, args=(_SHUTDOWN,), daemon=True, name="fleet-ledger").start()
    run_tray()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log_runtime_exception("fleet process terminated", exc)
        raise
