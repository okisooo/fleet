using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace FleetPanel
{
    /// <summary>
    /// The warm flyout window. It is created once and never hidden: closed, it waits
    /// fully rendered far off every monitor, so an open is "move and slide", with no
    /// render target to rebuild. It is a fixed column above the taskbar with the card
    /// standing at its bottom; a window region keeps clicks above the card passing
    /// through to whatever is there, and a section can open without the window
    /// resizing under it.
    ///
    /// The tray drives it with window messages (instant) and, as a fallback, with
    /// files in the cache folder: panel.show, panel.quit, panel-heartbeat.json,
    /// refresh.request, and panel.visible while it is on screen.
    /// </summary>
    internal sealed class PanelWindow : Window
    {
        public const string WindowTitle = "Fleet usage panel";
        // Must match tray.py: WM_APP + 0x46 toggles, WM_APP + 0x47 says new data.
        private const int WM_FLEET_TOGGLE = 0x8000 + 0x46;
        private const int WM_FLEET_DATA = 0x8000 + 0x47;
        // WM_APP + 0x48 opens straight onto the settings page (tray menu: Settings).
        private const int WM_FLEET_SETTINGS = 0x8000 + 0x48;
        private const double EdgeGap = 12;   // DIP between the card and the taskbar edge
        private const int Away = -32000;     // where the closed flyout waits

        private readonly string cache;
        private readonly ScreenCapture capture = new ScreenCapture();
        // Input priority sits above rendering: a busy high-refresh render loop must
        // never starve the timer that hears the tray and writes the heartbeat.
        private readonly DispatcherTimer signalTimer = new DispatcherTimer(DispatcherPriority.Input) { Interval = TimeSpan.FromMilliseconds(100) };
        private readonly DispatcherTimer clockTimer = new DispatcherTimer(DispatcherPriority.Input) { Interval = TimeSpan.FromSeconds(1) };
        private readonly DispatcherTimer dismissTimer = new DispatcherTimer(DispatcherPriority.Input) { Interval = TimeSpan.FromMilliseconds(90) };
        // Click-away watching only runs while the flyout is open.
        private readonly DispatcherTimer clickTimer = new DispatcherTimer(DispatcherPriority.Input) { Interval = TimeSpan.FromMilliseconds(16) };
        private PanelView view;
        private bool light;
        private IntPtr hwnd;
        private bool liveCapture;
        private bool open;
        private bool closing;
        private bool warming;
        private bool pendingModel;
        private DateTime signalSeen = DateTime.MinValue;
        private DateTime modelSeen = DateTime.MinValue;
        private DateTime modelCheck = DateTime.MinValue;
        private DateTime themeCheck = DateTime.MinValue;
        private DateTime lastHeartbeat = DateTime.MinValue;
        private DateTime lastVisibleTouch = DateTime.MinValue;
        private double[] anchor;
        private readonly bool[] buttons = new bool[7];
        private string openTiming = "";
        // Where the window stands when open (device pixels) and the scale it was laid out at.
        private int placedLeft, placedTop, placedWidth, placedHeight;
        private double placedScale = 1;
        private Rect cardRect;
        private Native.RECT region;

        public PanelWindow(string cache)
        {
            this.cache = cache;
            // Not layered: a layered WPF window is read back through the CPU every
            // frame. A normal window with its frame extended over a transparent
            // client is composed by DWM on the GPU at the display's refresh rate.
            Title = WindowTitle;
            WindowStyle = WindowStyle.None;
            AllowsTransparency = false;
            Background = Brushes.Transparent;
            ShowInTaskbar = false;
            Topmost = true;
            ResizeMode = ResizeMode.NoResize;
            SizeToContent = SizeToContent.Manual;
            Width = PanelView.Width;
            Height = 640;
            // It waits off screen while closed, so it is shown once and must never take
            // the focus by being shown; an open activates it on purpose.
            ShowActivated = false;
            WindowStartupLocation = WindowStartupLocation.Manual;
            Left = Away;
            Top = Away;
            light = Theme.SystemIsLight();
            BuildView();

            SourceInitialized += (s, e) => OnSourceInitialized();
            Deactivated += (s, e) => { dismissTimer.Stop(); dismissTimer.Start(); };
            Activated += (s, e) => dismissTimer.Stop();
            dismissTimer.Tick += (s, e) => { dismissTimer.Stop(); if (!IsActive && open) Close(animate: true); };
            KeyDown += (s, e) => { if (e.Key == Key.Escape) Close(animate: true); };
            signalTimer.Tick += (s, e) => OnSignalTick();
            clockTimer.Tick += (s, e) => view.Tick();
            clickTimer.Tick += (s, e) => { if (open && !closing) WatchOutsideClicks(); };
        }

        public void Start()
        {
            // Realise the HWND and composition target off every monitor, and play the
            // open once there: the first real click then finds the animation code
            // compiled and the shaders loaded, instead of hitching.
            Show();
            warming = true;
            view.PrepareIntro();
            view.PlayIntro();
            var warm = new DispatcherTimer(DispatcherPriority.Input) { Interval = TimeSpan.FromMilliseconds(900) };
            warm.Tick += (s, e) =>
            {
                warm.Stop();
                if (warming)
                {
                    warming = false;
                    view.Rest();
                    // Size and lay out for the monitor under the pointer now, while away,
                    // so even the first open is only a move.
                    Place();
                    view.PrepareIntro();
                }
            };
            warm.Start();
            signalSeen = Mtime("panel.show");
            signalTimer.Start();
            WriteHeartbeat();
        }

        private void BuildView()
        {
            view = new PanelView(Theme.Create(light));
            view.RefreshClicked += RequestRefresh;
            view.ActionClicked += action =>
            {
                if (action == "claude-sign-in") SignIn();
                else if (action == "grok-renew") RenewGrok();
                else if (action == "anthropic-billing") OpenBilling();
            };
            view.SettingChanged += SaveSetting;
            view.CredentialChanged += (id, key) =>
            {
                var error = key == null ? ApiCredentials.Remove(id) : ApiCredentials.Save(id, key);
                if (error == "") RequestRefresh();
                return error;
            };
            view.CardMoved += rect =>
            {
                cardRect = rect;
                if (open) Region();
            };
            view.IntroMeasured += stats =>
            {
                if (warming)
                {
                    return;
                }
                try
                {
                    var tier = RenderCapability.Tier >> 16;
                    var hz = Native.RefreshRateAt(hwnd);
                    var log = PathOf("panel-perf.log");
                    if (File.Exists(log) && new FileInfo(log).Length > 64 * 1024)
                    {
                        File.Delete(log);
                    }
                    File.AppendAllText(log, DateTime.Now.ToString("s") + " " + openTiming + stats
                        + " monitor_hz=" + hz + " desired=" + Program.FrameRate + " tier=" + tier + Environment.NewLine);
                    openTiming = "";
                }
                catch (Exception)
                {
                }
            };
            Content = view.Root;
            if (placedHeight > 0)
            {
                view.SetFrame(placedHeight / placedScale, placedHeight / placedScale - PanelView.Margin + EdgeGap);
            }
            modelSeen = DateTime.MinValue;
            LoadModel();
            view.PrepareIntro();
        }

        private void OnSourceInitialized()
        {
            hwnd = new WindowInteropHelper(this).Handle;
            var style = Native.GetWindowLong(hwnd, Native.GWL_EXSTYLE);
            Native.SetWindowLong(hwnd, Native.GWL_EXSTYLE, style | Native.WS_EX_TOOLWINDOW);
            // Excluded from capture, the copy of the screen shows what lies beneath
            // the flyout instead of the flyout itself.
            liveCapture = Native.SetWindowDisplayAffinity(hwnd, Native.WDA_EXCLUDEFROMCAPTURE);
            var none = Native.DWMWCP_DONOTROUND;
            Native.DwmSetWindowAttribute(hwnd, Native.DWMWA_WINDOW_CORNER_PREFERENCE, ref none, sizeof(int));
            var noBorder = Native.DWMWA_COLOR_NONE;
            Native.DwmSetWindowAttribute(hwnd, Native.DWMWA_BORDER_COLOR, ref noBorder, sizeof(int));
            var margins = new Native.MARGINS { Left = -1, Right = -1, Top = -1, Bottom = -1 };
            Native.DwmExtendFrameIntoClientArea(hwnd, ref margins);
            var source = HwndSource.FromHwnd(hwnd);
            if (source != null)
            {
                if (source.CompositionTarget != null)
                {
                    source.CompositionTarget.BackgroundColor = Colors.Transparent;
                }
                source.AddHook(OnMessage);
            }
        }

        /// <summary>
        /// The instant path. The tray posts a toggle straight to this window the
        /// moment its icon is clicked (wParam: the tray's icon window, lParam: the
        /// tick count at the click), and a data message after every poll.
        /// </summary>
        private IntPtr OnMessage(IntPtr window, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            if (message == WM_FLEET_TOGGLE)
            {
                handled = true;
                var posted = unchecked((uint)lParam.ToInt64());
                var delay = unchecked((uint)Environment.TickCount - posted);
                anchor = Native.TrayIconRect(wParam) ?? anchor;
                openTiming = delay < 10000 ? "msg_ms=" + delay + " " : "";
                Toggle();
                return (IntPtr)1;
            }
            if (message == WM_FLEET_DATA)
            {
                handled = true;
                LoadModel();
                return (IntPtr)1;
            }
            if (message == WM_FLEET_SETTINGS)
            {
                handled = true;
                anchor = Native.TrayIconRect(wParam) ?? anchor;
                view.ShowSettings();
                UpdateLayout();
                if (!open || closing)
                {
                    Toggle();
                }
                return (IntPtr)1;
            }
            return IntPtr.Zero;
        }

        // ---------- tray contract ----------

        private string PathOf(string name) => Path.Combine(cache, name);

        private DateTime Mtime(string name)
        {
            try { return File.GetLastWriteTimeUtc(PathOf(name)); }
            catch (Exception) { return DateTime.MinValue; }
        }

        private void OnSignalTick()
        {
            if (File.Exists(PathOf("panel.quit")))
            {
                try { File.Delete(PathOf("panel.quit")); } catch (Exception) { }
                Application.Current.Shutdown();
                return;
            }
            var seen = Mtime("panel.show");
            if (seen > signalSeen && seen != DateTime.MinValue)
            {
                signalSeen = seen;
                anchor = ReadAnchor();
                openTiming = "file ";
                Toggle();
            }
            var now = DateTime.UtcNow;
            if ((now - lastHeartbeat).TotalSeconds >= 10)
            {
                WriteHeartbeat();
            }
            if (open && !closing && (now - lastVisibleTouch).TotalSeconds >= 2)
            {
                Touch("panel.visible");
                lastVisibleTouch = now;
            }
            // The tray announces new data with a message; this is the fallback.
            if (pendingModel || (open && (now - modelCheck).TotalMilliseconds >= 1000) || (now - modelCheck).TotalSeconds >= 10)
            {
                modelCheck = now;
                LoadModel();
            }
            // A theme switch is rebuilt while closed, never during a click.
            if (!open && (now - themeCheck).TotalSeconds >= 3)
            {
                themeCheck = now;
                if (Theme.SystemIsLight() != light)
                {
                    light = !light;
                    BuildView();
                }
            }
        }

        private double[] ReadAnchor()
        {
            try
            {
                var payload = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(PathOf("panel.show")))
                    as System.Collections.Generic.Dictionary<string, object>;
                if (payload != null && payload.TryGetValue("anchor", out var raw) && raw is object[] values && values.Length == 4)
                {
                    return values.Select(v => Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture)).ToArray();
                }
            }
            catch (Exception)
            {
            }
            return null;
        }

        private void LoadModel()
        {
            var stamp = Mtime("panel.json");
            if (stamp == DateTime.MinValue || stamp == modelSeen)
            {
                pendingModel = false;
                return;
            }
            // Never rebuild under moving parts: a new reading waits for the motion to end.
            if (view.Animating || closing)
            {
                pendingModel = true;
                return;
            }
            pendingModel = false;
            try
            {
                var data = PanelData.Load(PathOf("panel.json"));
                modelSeen = stamp;
                view.Show(data, animate: open);
                // Lay out now, while nobody is waiting, so an open never has to.
                UpdateLayout();
                if (!open)
                {
                    view.PrepareIntro();
                }
            }
            catch (IOException)
            {
                // Mid-replace by the tray; the next tick reads the new file.
            }
            catch (Exception exception)
            {
                modelSeen = stamp;
                Note("panel.json unreadable: " + exception.Message);
            }
        }

        /// <summary>
        /// The tray restarts a panel whose heartbeat is 35 s old, so a beat must not
        /// be lost to a scanner briefly holding the file: on failure it falls back to
        /// a plain write and, failing that too, retries on the next tick. It also
        /// tells the tray which window to post clicks to.
        /// </summary>
        private void WriteHeartbeat()
        {
            var json = "{\"pid\": " + Process.GetCurrentProcess().Id + ", \"hwnd\": " + hwnd.ToInt64() + ", \"ts\": "
                + Clock.Now().ToString("F3", System.Globalization.CultureInfo.InvariantCulture)
                + ", \"visible\": " + (open ? "true" : "false") + ", \"native\": true}";
            if (WriteAtomic("panel-heartbeat.json", json) || WriteDirect("panel-heartbeat.json", json))
            {
                lastHeartbeat = DateTime.UtcNow;
                heartbeatFailures = 0;
            }
            else if (++heartbeatFailures % 40 == 1)
            {
                Note("heartbeat write failed " + heartbeatFailures + " time(s)");
            }
        }

        private int heartbeatFailures;

        private bool WriteDirect(string name, string text)
        {
            try
            {
                File.WriteAllText(PathOf(name), text);
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private void Note(string message)
        {
            try
            {
                File.AppendAllText(PathOf("panel-error.log"), DateTime.Now.ToString("s") + " " + message + Environment.NewLine);
            }
            catch (Exception)
            {
            }
        }

        private void Touch(string name)
        {
            var text = DateTime.UtcNow.Ticks.ToString();
            if (!WriteAtomic(name, text))
            {
                WriteDirect(name, text);
            }
        }

        private bool WriteAtomic(string name, string text)
        {
            try
            {
                var staging = PathOf(name + ".tmp");
                File.WriteAllText(staging, text);
                if (File.Exists(PathOf(name))) File.Replace(staging, PathOf(name), null);
                else File.Move(staging, PathOf(name));
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private void RequestRefresh() => Touch("refresh.request");

        private void OpenBilling()
        {
            try
            {
                Process.Start(new ProcessStartInfo("https://platform.claude.com/settings/billing") { UseShellExecute = true });
            }
            catch (Exception)
            {
                Note("could not open Claude billing");
            }
        }

        private void SignIn()
        {
            var exe = FindClaude();
            if (exe == null)
            {
                return;
            }
            try
            {
                var process = Process.Start(new ProcessStartInfo("cmd.exe",
                    "/c title Claude sign-in & \"" + exe + "\" auth login & pause") { UseShellExecute = true });
                if (process != null)
                {
                    process.EnableRaisingEvents = true;
                    process.Exited += (s, e) => RequestRefresh();
                }
                Close(animate: true);
            }
            catch (Exception)
            {
            }
        }

        /// <summary>Grok renews its own sign-in; Fleet only opens the console for it.</summary>
        private void RenewGrok()
        {
            var exe = FindOnPath("grok.exe")
                ?? Existing(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".grok", "bin", "grok.exe"));
            if (exe == null)
            {
                return;
            }
            try
            {
                var process = Process.Start(new ProcessStartInfo("cmd.exe",
                    "/c title Grok sign-in & \"" + exe + "\" login & pause") { UseShellExecute = true });
                if (process != null)
                {
                    process.EnableRaisingEvents = true;
                    process.Exited += (s, e) => RequestRefresh();
                }
                Close(animate: true);
            }
            catch (Exception)
            {
            }
        }

        private static string Existing(string path) => File.Exists(path) ? path : null;

        private static string FindOnPath(string name)
        {
            foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
            {
                try
                {
                    var candidate = Path.Combine(dir.Trim(), name);
                    if (dir.Trim() != "" && File.Exists(candidate)) return candidate;
                }
                catch (ArgumentException)
                {
                }
            }
            return null;
        }

        /// <summary>
        /// settings.json sits beside the cache folder. Merge one key into it; the
        /// tray notices within two seconds and redraws the icon and the model.
        /// </summary>
        private void SaveSetting(string key, object value)
        {
            try
            {
                var folder = Directory.GetParent(cache.TrimEnd('\\', '/'))?.FullName ?? cache;
                var path = Path.Combine(folder, "settings.json");
                var serializer = new JavaScriptSerializer();
                var current = File.Exists(path)
                    ? serializer.DeserializeObject(File.ReadAllText(path)) as System.Collections.Generic.Dictionary<string, object>
                    : null;
                current = current ?? new System.Collections.Generic.Dictionary<string, object>();
                current[key] = value;
                var staging = path + ".tmp";
                File.WriteAllText(staging, serializer.Serialize(current));
                if (File.Exists(path)) File.Replace(staging, path, null);
                else File.Move(staging, path);
            }
            catch (Exception exception)
            {
                Note("settings not saved: " + exception.Message);
            }
        }

        private static string FindClaude()
        {
            var path = Environment.GetEnvironmentVariable("PATH") ?? "";
            foreach (var dir in path.Split(';'))
            {
                try
                {
                    var candidate = Path.Combine(dir.Trim(), "claude.exe");
                    if (dir.Trim() != "" && File.Exists(candidate)) return candidate;
                }
                catch (ArgumentException)
                {
                }
            }
            var native = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".local", "bin", "claude.exe");
            return File.Exists(native) ? native : null;
        }

        // ---------- open / close ----------

        private void Toggle()
        {
            if (warming)
            {
                warming = false;
                view.Rest();
            }
            if (open && !closing)
            {
                Close(animate: true);
                return;
            }
            Open();
        }

        /// <summary>
        /// Move and slide; nothing else stands between the click and the first frame.
        /// Data, layout and theme were prepared while closed. One copy of the screen
        /// behind is taken first so the glass is right from the first frame.
        /// </summary>
        private void Open()
        {
            var clock = Stopwatch.StartNew();
            closing = false;
            open = true;
            if (Cloaked())
            {
                // Waiting on another virtual desktop: showing it again brings it here.
                Hide();
                Show();
            }
            view.Tick();
            view.PrepareIntro();
            Place();
            CaptureBackdrop();
            var placed = clock.Elapsed.TotalMilliseconds;
            Native.SetWindowPos(hwnd, IntPtr.Zero, placedLeft, placedTop, 0, 0, Native.SWP_NOSIZE | Native.SWP_NOZORDER | Native.SWP_NOACTIVATE);
            view.PlayIntro();
            Activate();
            Native.SetForegroundWindow(hwnd);
            var shown = clock.Elapsed.TotalMilliseconds;
            EventHandler firstFrame = null;
            firstFrame = (s, e) =>
            {
                CompositionTarget.Rendering -= firstFrame;
                openTiming += string.Format(System.Globalization.CultureInfo.InvariantCulture,
                    "place_ms={0:F1} show_ms={1:F1} first_frame_ms={2:F1} ", placed, shown, clock.Elapsed.TotalMilliseconds);
            };
            CompositionTarget.Rendering += firstFrame;
            for (var i = 0; i < buttons.Length; i++) buttons[i] = (Native.GetAsyncKeyState(i) & 0x8000) != 0;
            clickTimer.Start();

            // Everything that can wait for the first frame does.
            Dispatcher.BeginInvoke(new Action(() =>
            {
                if (!open || closing)
                {
                    return;
                }
                Touch("panel.visible");
                lastVisibleTouch = DateTime.UtcNow;
                // A paced refresh: opening must not spend Claude's usage budget.
                Touch("refresh.soon");
                WriteHeartbeat();
                StartLiveGlass();
                clockTimer.Start();
            }), DispatcherPriority.Background);
        }

        private void Close(bool animate)
        {
            if (!open || closing)
            {
                return;
            }
            closing = true;
            dismissTimer.Stop();
            if (!animate || !SystemParameters.ClientAreaAnimation)
            {
                Dispatcher.BeginInvoke(new Action(Finish), DispatcherPriority.Background);
                return;
            }
            view.PlayOutro(() => Dispatcher.BeginInvoke(new Action(() => { if (closing) Finish(); }), DispatcherPriority.Background));
        }

        private void Finish()
        {
            view.ClearApiEditor();
            clickTimer.Stop();
            StopLiveGlass();
            clockTimer.Stop();
            view.Rest();
            Native.SetWindowPos(hwnd, IntPtr.Zero, Away, Away, 0, 0, Native.SWP_NOSIZE | Native.SWP_NOZORDER | Native.SWP_NOACTIVATE);
            open = false;
            closing = false;
            // The next open starts on the usage page with every section closed, laid
            // out while nobody is looking.
            view.ShowUsage();
            if (pendingModel)
            {
                LoadModel();
            }
            UpdateLayout();
            view.PrepareIntro();
            try { File.Delete(PathOf("panel.visible")); } catch (Exception) { }
            WriteHeartbeat();
        }

        private bool Cloaked() =>
            hwnd != IntPtr.Zero && Native.DwmGetWindowAttribute(hwnd, Native.DWMWA_CLOAKED, out var cloaked, sizeof(int)) == 0 && cloaked != 0;

        // The glass follows the screen from a background thread (LiveBackdrop); the
        // UI swaps in the newest copy when nothing is moving, about 0.1 ms. Capture
        // count and cost are logged on close.
        private readonly LiveBackdrop live = new LiveBackdrop();
        private EventHandler liveFrame;
        private WriteableBitmap liveBitmap;
        private byte[] liveBytes;

        private void StartLiveGlass()
        {
            if (!liveCapture || liveFrame != null || !Theme.TransparencyEnabled() || hwnd == IntPtr.Zero)
            {
                return;
            }
            live.Start(placedLeft, placedTop, placedWidth, placedHeight);
            liveFrame = (s, e) =>
            {
                if (!open || closing || view.Animating || !live.TryTake(ref liveBytes, out var w, out var h, out var lum))
                {
                    return;
                }
                if (liveBitmap == null || liveBitmap.PixelWidth != w || liveBitmap.PixelHeight != h)
                {
                    liveBitmap = new WriteableBitmap(w, h, 96, 96, PixelFormats.Bgr32, null);
                }
                liveBitmap.WritePixels(new Int32Rect(0, 0, w, h), liveBytes, w * 4, 0);
                view.SetBackdrop(liveBitmap, lum);
            };
            CompositionTarget.Rendering += liveFrame;
        }

        private void StopLiveGlass()
        {
            live.Stop();
            if (liveFrame == null)
            {
                return;
            }
            CompositionTarget.Rendering -= liveFrame;
            liveFrame = null;
            var captures = live.Captures;
            if (captures > 0)
            {
                try
                {
                    File.AppendAllText(PathOf("panel-perf.log"), DateTime.Now.ToString("s") + string.Format(
                        System.Globalization.CultureInfo.InvariantCulture, " live_glass thread captures={0} capture_avg_ms={1:F2}",
                        captures, live.TotalMs / captures) + Environment.NewLine);
                }
                catch (Exception)
                {
                }
            }
        }

        private void CaptureBackdrop()
        {
            // With Windows' transparency effects off the surface stays opaque.
            if (!Theme.TransparencyEnabled() || placedWidth <= 0)
            {
                return;
            }
            if (capture.Capture(placedLeft, placedTop, placedWidth, placedHeight))
            {
                view.SetBackdrop(capture.Bitmap, capture.Luminance);
            }
        }

        private void WatchOutsideClicks()
        {
            var pressed = false;
            foreach (var key in new[] { 1, 2, 4, 5, 6 })
            {
                var state = Native.GetAsyncKeyState(key);
                var down = (state & 0x8000) != 0;
                pressed |= (state & 0x0001) != 0 || (down && !buttons[key]);
                buttons[key] = down;
            }
            if (!pressed)
            {
                return;
            }
            Native.GetCursorPos(out var p);
            var card = CardOnScreen();
            var insideCard = p.X >= card.Left && p.X < card.Right && p.Y >= card.Top && p.Y < card.Bottom;
            var onAnchor = anchor != null && p.X >= anchor[0] && p.X < anchor[2] && p.Y >= anchor[1] && p.Y < anchor[3];
            if (!insideCard && !onAnchor)
            {
                Close(animate: true);
            }
        }

        /// <summary>The card's rectangle on screen, in device pixels.</summary>
        private Native.RECT CardOnScreen() => new Native.RECT
        {
            Left = placedLeft + (int)Math.Floor(cardRect.Left * placedScale),
            Top = placedTop + (int)Math.Floor(cardRect.Top * placedScale),
            Right = placedLeft + (int)Math.Ceiling(cardRect.Right * placedScale),
            Bottom = placedTop + (int)Math.Ceiling(cardRect.Bottom * placedScale),
        };

        /// <summary>
        /// The window is a whole column; only the card and its shadow are the flyout.
        /// A region the size of that keeps every click above it going to the window
        /// beneath, as if the column were not there.
        /// </summary>
        private void Region()
        {
            if (hwnd == IntPtr.Zero || placedWidth <= 0)
            {
                return;
            }
            // From the top of the card's shadow down to the taskbar's edge: the strip of
            // window over the taskbar must not swallow clicks meant for the tray icon.
            var margin = PanelView.Margin * placedScale;
            var top = Math.Max(0, (int)Math.Floor((cardRect.Top * placedScale) - margin));
            var bottom = placedHeight - (int)Math.Round((PanelView.Margin - EdgeGap) * placedScale);
            var next = new Native.RECT { Left = 0, Top = top, Right = placedWidth, Bottom = Math.Max(top + 1, bottom) };
            if (next.Top == region.Top && next.Right == region.Right && next.Bottom == region.Bottom)
            {
                return;
            }
            var handle = Native.CreateRectRgn(next.Left, next.Top, next.Right, next.Bottom);
            if (Native.SetWindowRgn(hwnd, handle, false) != 0)
            {
                region = next;
            }
            else
            {
                Native.DeleteObject(handle);
            }
        }

        /// <summary>
        /// Lay the window out as a column standing on the taskbar edge that holds the
        /// tray icon, the card centred on the icon. The window moves on screen only
        /// when the slide starts; until then it waits away.
        /// </summary>
        private void Place()
        {
            Native.POINT point;
            if (anchor != null)
            {
                point = new Native.POINT { X = (int)((anchor[0] + anchor[2]) / 2), Y = (int)((anchor[1] + anchor[3]) / 2) };
            }
            else
            {
                Native.GetCursorPos(out point);
            }
            var monitor = Native.MonitorFromPoint(point, Native.MONITOR_DEFAULTTONEAREST);
            var info = new Native.MONITORINFO { cbSize = System.Runtime.InteropServices.Marshal.SizeOf(typeof(Native.MONITORINFO)) };
            if (!Native.GetMonitorInfo(monitor, ref info))
            {
                return;
            }
            double scale = 1;
            if (Native.GetDpiForMonitor(monitor, 0, out var dpiX, out _) == 0)
            {
                scale = dpiX / 96.0;
            }
            var work = info.rcWork;
            var margin = PanelView.Margin * scale;
            var gap = EdgeGap * scale;
            var width = (int)Math.Round(PanelView.Width * scale);
            var height = (int)Math.Round((work.Bottom - work.Top) - 2 * gap + 2 * margin);
            double left, top;
            // Windows 11 keeps the taskbar at the bottom; a side or top bar (older
            // shells, third-party tools) gets the same column against the work area.
            if (anchor != null && anchor[0] >= work.Right - 1)
            {
                left = work.Right - gap + margin - width;
            }
            else if (anchor != null && anchor[2] <= work.Left + 1)
            {
                left = work.Left + gap - margin;
            }
            else
            {
                left = Math.Max(work.Left + gap - margin, Math.Min(point.X - width / 2.0, work.Right - gap + margin - width));
            }
            top = work.Bottom - gap + margin - height;
            var rescaled = Math.Abs(scale - placedScale) > 0.001 || height != placedHeight || width != placedWidth;
            placedLeft = (int)Math.Round(left);
            placedTop = (int)Math.Round(top);
            placedWidth = width;
            placedHeight = height;
            placedScale = scale;
            if (rescaled)
            {
                // A new monitor or work area: resize while still away, then lay out.
                Native.SetWindowPos(hwnd, IntPtr.Zero, Away, Away, width, height, Native.SWP_NOZORDER | Native.SWP_NOACTIVATE);
                view.SetFrame(height / scale, height / scale - PanelView.Margin + EdgeGap);
                UpdateLayout();
                view.PrepareIntro();
                region = default(Native.RECT);
            }
            Region();
            if (Environment.GetEnvironmentVariable("FLEET_PANEL_OFFSCREEN") == "1")
            {
                // Test runs exercise the real open/close path without showing anything;
                // where it would have gone is logged for the test to check.
                try
                {
                    var card = CardOnScreen();
                    File.AppendAllText(PathOf("panel-place.log"), string.Format(System.Globalization.CultureInfo.InvariantCulture,
                        "{0} {1} {2} {3} {4} {5} {6} {7}" + Environment.NewLine,
                        card.Left, card.Top, card.Right - card.Left, card.Bottom - card.Top, work.Left, work.Top, work.Right, work.Bottom));
                }
                catch (Exception)
                {
                }
                placedLeft = Away;
                placedTop = Away;
            }
            live.Move(placedLeft, placedTop, placedWidth, placedHeight);
        }
    }
}
