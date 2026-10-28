using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace FleetPanel
{
    internal static class Program
    {
        public static int FrameRate = 60;

        /// <summary>
        /// FleetPanel --cache DIR              the live flyout, launched by the tray
        /// FleetPanel --render MODEL BACKDROP OUT.png light|dark [scale] [usage|settings|detail:ID]
        ///                                    draw the flyout offscreen over a picture
        /// </summary>
        [STAThread]
        public static int Main(string[] args)
        {
            // WPF animates at 60 fps unless told otherwise; match the display, so a
            // 320 Hz monitor gets 320 Hz motion.
            FrameRate = Math.Max(60, Native.FastestRefreshRate());
            System.Windows.Media.Animation.Timeline.DesiredFrameRateProperty.OverrideMetadata(
                typeof(System.Windows.Media.Animation.Timeline),
                new FrameworkPropertyMetadata { DefaultValue = (int?)FrameRate });
            if (args.Length > 0 && args[0] == "--diag")
            {
                Console.WriteLine("fastest_hz=" + FrameRate + " primary_hz=" + Native.RefreshRate()
                    + " tier=" + (RenderCapability.Tier >> 16));
                return 0;
            }
            if (args.Length >= 5 && args[0] == "--render")
            {
                return Render(args[1], args[2], args[3], args[4] == "light",
                    args.Length > 5 ? double.Parse(args[5], System.Globalization.CultureInfo.InvariantCulture) : 2.0,
                    args.Length > 6 ? args[6] : "usage");
            }
            var cache = Arg(args, "--cache");
            if (cache == null || !Directory.Exists(cache))
            {
                return 2;
            }
            // One panel per cache folder: a test run or a second tray never collides
            // with the installed one. GetHashCode is stable for a string on .NET Framework.
            var scope = Path.GetFullPath(cache).TrimEnd('\\').ToLowerInvariant().GetHashCode().ToString("x8");
            using (var single = new Mutex(true, @"Local\fleet-panel-" + scope, out var first))
            {
                if (!first)
                {
                    return 0;
                }
                var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
                // A fault in the flyout leaves a note for the tray log instead of vanishing.
                app.DispatcherUnhandledException += (s, e) =>
                {
                    // Exception messages may include input. Keep type and stack,
                    // never credential text, and bound both the entry and the file.
                    Note(cache, e.Exception.GetType().FullName + Environment.NewLine + e.Exception.StackTrace);
                };
                WatchParent(app);
                var window = new PanelWindow(cache);
                window.Start();
                app.Run();
            }
            return 0;
        }

        private static string Arg(string[] args, string name)
        {
            var i = Array.IndexOf(args, name);
            return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
        }

        internal static void Note(string cache, string message)
        {
            try
            {
                var path = Path.Combine(cache, "panel-error.log");
                if (File.Exists(path) && new FileInfo(path).Length > 64 * 1024)
                {
                    var previous = path + ".previous";
                    if (File.Exists(previous)) File.Delete(previous);
                    File.Move(path, previous);
                }
                if (message.Length > 4096) message = message.Substring(0, 4096);
                File.AppendAllText(path, DateTime.Now.ToString("s") + " " + message + Environment.NewLine);
            }
            catch (Exception) { }
        }

        /// <summary>No orphan flyout: leave when the tray that started us is gone.</summary>
        private static void WatchParent(Application app)
        {
            if (!int.TryParse(Environment.GetEnvironmentVariable("FLEET_PARENT_PID"), out var pid))
            {
                return;
            }
            Process parent;
            try { parent = Process.GetProcessById(pid); }
            catch (ArgumentException) { Environment.Exit(0); return; }
            new Thread(() =>
            {
                parent.WaitForExit();
                app.Dispatcher.BeginInvoke(new Action(app.Shutdown));
            }) { IsBackground = true, Name = "fleet-parent" }.Start();
        }

        /// <summary>page: "usage", "settings", or "detail:&lt;provider id&gt;".</summary>
        private static int Render(string model, string backdropPath, string output, bool light, double scale, string page)
        {
            var view = new PanelView(Theme.Create(light));
            view.Show(PanelData.Load(model));
            if (page == "settings") view.ShowSettings();
            else if (page == "dashboard") { view.ShowDashboard(); view.SetFrame(900, 900); }
            else if (page == "dashboard-full") view.ShowDashboard();
            else if (page.StartsWith("detail:")) view.Select(page.Substring("detail:".Length));
            var root = view.Root;
            // Twice: the first pass sizes the card, which then pins the backdrop to it.
            var height = 0.0;
            for (var pass = 0; pass < 2; pass++)
            {
                root.Measure(new Size(PanelView.Width, double.PositiveInfinity));
                height = Math.Ceiling(root.DesiredSize.Height);
                root.Arrange(new Rect(0, 0, PanelView.Width, height));
                root.UpdateLayout();
            }

            // The backdrop picture stands in for the screen behind the flyout.
            var behind = new BitmapImage(new Uri(Path.GetFullPath(backdropPath)));
            var frame = new CroppedBitmap(behind, new Int32Rect(0, 0,
                Math.Min(behind.PixelWidth, (int)(PanelView.Width * scale)),
                Math.Min(behind.PixelHeight, (int)(height * scale))));
            var pixels = new byte[frame.PixelWidth * frame.PixelHeight * 4];
            var bgra = new FormatConvertedBitmap(frame, PixelFormats.Bgra32, null, 0);
            bgra.CopyPixels(pixels, frame.PixelWidth * 4, 0);
            var handle = System.Runtime.InteropServices.GCHandle.Alloc(pixels, System.Runtime.InteropServices.GCHandleType.Pinned);
            double luminance;
            try { luminance = ScreenCapture.Measure(handle.AddrOfPinnedObject(), frame.PixelWidth, frame.PixelHeight); }
            finally { handle.Free(); }
            view.SetBackdrop(frame, luminance);
            root.UpdateLayout();

            var canvas = new DrawingVisual();
            using (var dc = canvas.RenderOpen())
            {
                dc.DrawImage(frame, new Rect(0, 0, PanelView.Width, height));
                // Map the layout box 1:1: by default a VisualBrush fits the visual's whole
                // render bounds, shadow and blur overflow included, and shrinks the panel.
                dc.DrawRectangle(new VisualBrush(root)
                {
                    ViewboxUnits = BrushMappingMode.Absolute, Viewbox = new Rect(0, 0, PanelView.Width, height),
                    ViewportUnits = BrushMappingMode.Absolute, Viewport = new Rect(0, 0, PanelView.Width, height),
                    Stretch = Stretch.Fill,
                }, null, new Rect(0, 0, PanelView.Width, height));
            }
            var bitmap = new RenderTargetBitmap((int)(PanelView.Width * scale), (int)(height * scale), 96 * scale, 96 * scale, PixelFormats.Pbgra32);
            bitmap.Render(canvas);
            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(bitmap));
            using (var stream = File.Create(output))
            {
                encoder.Save(stream);
            }
            return 0;
        }
    }
}
