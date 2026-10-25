using System;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace FleetPanel
{
    /// <summary>A single, lazy, resizable dashboard. It shares the tray's safe model;
    /// no credentials, network client or second collector live in this window.</summary>
    internal sealed class DashboardWindow : Window
    {
        private readonly DockPanel root = new DockPanel();
        private readonly ScrollViewer scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
        private readonly DispatcherTimer clock = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        private readonly DispatcherTimer resize = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(180) };
        private PanelView view;
        private PanelData data;
        private bool light;
        private int columns;
        public event Action RefreshRequested;
        public event Action SettingsRequested;
        public event Action<string> ActionRequested;

        public DashboardWindow(PanelData data, bool light)
        {
            this.data = data; this.light = light;
            Title = "Fleet dashboard";
            MinWidth = Math.Min(680, Math.Max(320, SystemParameters.WorkArea.Width - 24));
            MinHeight = Math.Min(420, Math.Max(240, SystemParameters.WorkArea.Height - 24));
            Width = Math.Max(MinWidth, Math.Min(1120, SystemParameters.WorkArea.Width - 48));
            Height = Math.Max(MinHeight, Math.Min(850, SystemParameters.WorkArea.Height - 48));
            WindowStartupLocation = WindowStartupLocation.CenterScreen;
            ResizeMode = ResizeMode.CanResize;
            ShowInTaskbar = true;
            if (Environment.GetEnvironmentVariable("FLEET_PANEL_OFFSCREEN") == "1")
            {
                WindowStartupLocation = WindowStartupLocation.Manual; Left = Top = -32000;
                ShowActivated = false; ShowInTaskbar = false;
            }
            Content = root;
            TextOptions.SetTextFormattingMode(root, TextFormattingMode.Display);
            UseLayoutRounding = true;
            BuildTheme();
            SourceInitialized += (s, e) => { FrameTheme(); LoadIcon(); };
            clock.Tick += (s, e) => { if (IsVisible && WindowState != WindowState.Minimized) view.Tick(); };
            resize.Tick += (s, e) => { resize.Stop(); Rebuild(); };
            SizeChanged += (s, e) => { if ((ActualWidth >= 900 ? 2 : 1) != columns) { resize.Stop(); resize.Start(); } };
            Closed += (s, e) => { clock.Stop(); resize.Stop(); };
            clock.Start();
        }

        public void Update(PanelData next)
        {
            data = next;
            Rebuild();
        }

        public void ChangeTheme(bool next)
        {
            if (light == next) return;
            light = next;
            BuildTheme();
            FrameTheme();
        }

        private void FrameTheme()
        {
            var hwnd = new WindowInteropHelper(this).Handle;
            if (hwnd == IntPtr.Zero) return;
            var dark = light ? 0 : 1;
            Native.DwmSetWindowAttribute(hwnd, 20, ref dark, sizeof(int));
        }

        private void LoadIcon()
        {
            var exe = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "Fleet.exe");
            if (!File.Exists(exe)) return;
            try
            {
                using (var icon = System.Drawing.Icon.ExtractAssociatedIcon(exe))
                {
                    if (icon == null) return;
                    Icon = Imaging.CreateBitmapSourceFromHIcon(icon.Handle, Int32Rect.Empty, BitmapSizeOptions.FromEmptyOptions());
                    Icon.Freeze();
                }
            }
            catch (ArgumentException) { }
            catch (System.ComponentModel.Win32Exception) { }
        }

        private void BuildTheme()
        {
            var theme = Theme.Create(light);
            Background = new SolidColorBrush(theme.Fallback);
            view = new PanelView(theme);
            view.RefreshClicked += () => RefreshRequested?.Invoke();
            view.ActionClicked += action => ActionRequested?.Invoke(action);
            root.Children.Clear();
            var header = view.DashboardHeader(() => SettingsRequested?.Invoke());
            DockPanel.SetDock(header, Dock.Top); root.Children.Add(header);
            root.Children.Add(scroll);
            Rebuild();
        }

        private void Rebuild()
        {
            var offset = scroll.VerticalOffset;
            var width = ActualWidth > 0 ? ActualWidth : Width;
            columns = width >= 900 ? 2 : 1;
            scroll.Content = view.Dashboard(data, width);
            scroll.UpdateLayout();
            scroll.ScrollToVerticalOffset(offset);
        }

        /// <summary>Same layout for the sample-data renderer; no desktop window.</summary>
        public static FrameworkElement Preview(PanelData data, bool light, double width)
        {
            var view = new PanelView(Theme.Create(light));
            var root = new DockPanel { Background = new SolidColorBrush(Theme.Create(light).Fallback) };
            var header = view.DashboardHeader(() => { });
            DockPanel.SetDock(header, Dock.Top); root.Children.Add(header);
            root.Children.Add(new ScrollViewer { Content = view.Dashboard(data, width),
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled });
            return root;
        }
    }
}
