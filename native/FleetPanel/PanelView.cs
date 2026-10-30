using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Effects;
using System.Windows.Media.Imaging;
using System.Windows.Media.Media3D;
using System.Windows.Shapes;

namespace FleetPanel
{
    /// <summary>
    /// The flyout: one acrylic card standing on the taskbar, built from Windows 11's
    /// own tokens and type ramp. On top, what the agents' tokens came to at API list
    /// prices (today, 7 and 30 days). Below, one flat section per provider: its
    /// limits as slim meters (used, a notch where an even pace would be, time to the
    /// reset), and a line only when something needs attention. A click opens the
    /// section in place: activity over the last day, burn rate, spend by span, model
    /// and project, and how each limit is pacing. Logins that need a hand are a row
    /// with the fix. Shared by the live window and the offscreen renderer, so
    /// previews show exactly what ships.
    ///
    /// Motion is render-only: the card slides out of the taskbar on a cached layer,
    /// and a section opens by revealing its height, so frames stay cheap at any
    /// refresh rate.
    /// </summary>
    internal sealed partial class PanelView
    {
        public const double CardWidth = 380;
        // Room for the whole shadow: one that runs past the window edge is cut off
        // there, and shows as a hard square around the flyout.
        public const double Margin = 28;
        public const double Width = CardWidth + 2 * Margin;
        public const double Radius = 16;
        private const double Pad = 18;
        // Meters line up under the provider's name, past its icon.
        private const double Indent = 26;
        private const double LabelColumn = 64;
        private const double KeyColumn = 92;

        private static readonly FontFamily DisplayFont = new FontFamily("Segoe UI Variable Display, Segoe UI");
        private static readonly FontFamily TextFont = new FontFamily("Segoe UI Variable Text, Segoe UI");
        private static readonly FontFamily SmallFont = new FontFamily("Segoe UI Variable Small, Segoe UI");
        private static readonly FontFamily IconFont = new FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets");
        // Windows' own curves: things arrive fast and settle; they leave by speeding up.
        private static readonly IEasingFunction Decelerate = Frozen(new Bezier(0.1, 0.9, 0.2, 1.0));
        private static readonly IEasingFunction Accelerate = Frozen(new Bezier(0.7, 0.0, 1.0, 0.5));
        private static readonly IEasingFunction Standard = Frozen(new Bezier(0.55, 0.0, 0.1, 1.0));

        private readonly Theme theme;
        private readonly Grid card = new Grid { Width = CardWidth, VerticalAlignment = VerticalAlignment.Bottom, HorizontalAlignment = HorizontalAlignment.Center };
        private readonly Pinned pinned = new Pinned();
        private readonly TranslateTransform slabShift = new TranslateTransform();
        private readonly RectangleGeometry glassClip = new RectangleGeometry { RadiusX = Radius, RadiusY = Radius };
        private readonly Image backdrop = new Image { Stretch = Stretch.Fill };
        private readonly GlassEffect glass;
        private readonly StackPanel content = new StackPanel();
        private readonly StackPanel navigation = new StackPanel();
        private readonly ScrollViewer scroller;
        private readonly TranslateTransform lift = new TranslateTransform();
        private readonly List<Action> tickers = new List<Action>();
        private readonly Dictionary<string, SectionParts> sections = new Dictionary<string, SectionParts>();
        private readonly Dictionary<string, ImageSource> masks = new Dictionary<string, ImageSource>();
        // Frozen brushes are safe to share, including the small custom drawings.
        // Bound this cache even if a future theme supplies arbitrary colours.
        private static readonly Dictionary<Color, SolidColorBrush> brushes = new Dictionary<Color, SolidColorBrush>();
        private string renderedKey;
        private readonly RotateTransform spin = new RotateTransform();
        private PanelData data = new PanelData();
        private string page = "usage";
        private string selected = "";
        private string units;
        private string apiEditing = "", apiFeedback = "";
        private PasswordBox apiInput;
        private double updated;
        private DateTime busyUntil = DateTime.MinValue;
        private EventHandler counting;

        public event Action RefreshClicked;
        /// <summary>Raised once the open animation settles, with its measured frame pacing.</summary>
        public event Action<string> IntroMeasured;
        /// <summary>A fix the user asked for: "claude-sign-in", "grok-renew".</summary>
        public event Action<string> ActionClicked;
        /// <summary>A setting changed on the settings page: key and new value.</summary>
        public event Action<string, object> SettingChanged;
        /// <summary>Direct to Windows Credential Manager; never settings or IPC.</summary>
        public event Func<string, string, string> CredentialChanged;
        /// <summary>The card's rectangle in Root coordinates (DIP), whenever it moves or resizes.</summary>
        public event Action<Rect> CardMoved;

        public Grid Root { get; } = new Grid { Width = Width };

        /// <summary>True while motion runs: the window holds new backdrops until it ends.</summary>
        public bool Animating => DateTime.UtcNow < busyUntil;

        /// <summary>Where the card sits in Root (DIP), ignoring the open/close slide.</summary>
        public Rect Card => new Rect(Margin, Math.Max(0, Root.ActualHeight - Margin - card.ActualHeight), card.ActualWidth, card.ActualHeight);

        public PanelView(Theme theme)
        {
            this.theme = theme;
            glass = new GlassEffect(theme);
            TextOptions.SetTextFormattingMode(Root, TextFormattingMode.Display);
            Root.SnapsToDevicePixels = true;
            Root.UseLayoutRounding = true;
            card.Margin = new Thickness(Margin);
            card.RenderTransform = lift;
            Root.Children.Add(card);

            // The shadow is its own cached layer under the glass.
            var shadow = new Border
            {
                CornerRadius = new CornerRadius(Radius),
                Background = new SolidColorBrush(theme.Fallback),
                Effect = new DropShadowEffect
                {
                    BlurRadius = 24, ShadowDepth = 5, Direction = 270, Opacity = theme.ShadowOpacity, Color = theme.Shadow,
                    RenderingBias = RenderingBias.Quality,
                },
                IsHitTestVisible = false,
            };
            card.Children.Add(Cached(shadow));

            // The capture of what is behind the window, blurred and laid in Windows'
            // acrylic layers. It is as big as the window and pinned to it, so a section
            // opening only uncovers more of it: the card's clip moves, nothing redraws.
            backdrop.Effect = new BlurEffect { Radius = 32, KernelType = KernelType.Gaussian, RenderingBias = RenderingBias.Quality };
            // The blur overflows its image; clipped on an inner layer, the acrylic only
            // ever sees the frame's own pixels.
            var frosted = new Grid { ClipToBounds = true };
            frosted.Children.Add(new Border { Background = new SolidColorBrush(theme.Fallback) });
            frosted.Children.Add(backdrop);
            var slab = new Grid { Effect = glass, CacheMode = new BitmapCache { SnapsToDevicePixels = true } };
            slab.Children.Add(frosted);
            pinned.Child = slab;
            pinned.RenderTransform = slabShift;
            var glassHost = new Grid { Clip = glassClip, IsHitTestVisible = false };
            glassHost.Children.Add(pinned);
            card.Children.Add(glassHost);

            card.Children.Add(new Border
            {
                CornerRadius = new CornerRadius(Radius),
                BorderThickness = new Thickness(1),
                BorderBrush = Brush(theme.Edge),
                IsHitTestVisible = false,
            });

            scroller = new ScrollViewer
            {
                Content = content,
                VerticalScrollBarVisibility = ScrollBarVisibility.Hidden,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                PanningMode = PanningMode.VerticalOnly,
                Focusable = false,
            };
            var pages = new DockPanel();
            DockPanel.SetDock(navigation, Dock.Top);
            pages.Children.Add(navigation);
            pages.Children.Add(scroller);
            card.Children.Add(new Border { Padding = new Thickness(Pad, 8, Pad, 10), Child = pages });

            card.SizeChanged += (s, e) => Reframe();
            Root.SizeChanged += (s, e) => Reframe();
        }

        /// <summary>
        /// Window mode: the window is a fixed column above the taskbar, the card stands
        /// at its bottom, and nothing below <paramref name="clipBottom"/> (the taskbar's
        /// edge) is drawn, so the card slides out of the taskbar and back into it.
        /// </summary>
        public void SetFrame(double height, double clipBottom)
        {
            Root.Height = height;
            card.MaxHeight = Math.Max(120, height - 2 * Margin);
            Root.Clip = new RectangleGeometry(new Rect(0, 0, Width, clipBottom));
        }

        private void Reframe()
        {
            var w = card.ActualWidth;
            var h = card.ActualHeight;
            glassClip.Rect = new Rect(0, 0, w, h);
            var extent = new Size(Root.ActualWidth, Root.ActualHeight);
            if (pinned.Extent != extent)
            {
                pinned.Extent = extent;
                pinned.InvalidateMeasure();
                var dpi = VisualTreeHelper.GetDpi(Root);
                glass.Size = new Point4D(extent.Width * dpi.DpiScaleX, extent.Height * dpi.DpiScaleY, 0, 0);
            }
            slabShift.X = -Margin;
            slabShift.Y = -(Root.ActualHeight - Margin - h);
            CardMoved?.Invoke(Card);
        }

        private static Grid Cached(UIElement child)
        {
            var host = new Grid { CacheMode = new BitmapCache { SnapsToDevicePixels = true }, IsHitTestVisible = false };
            host.Children.Add(child);
            return host;
        }

        private static IEasingFunction Frozen(EasingFunctionBase ease)
        {
            ease.Freeze();
            return ease;
        }

        public void SetBackdrop(ImageSource source, double luminance) => backdrop.Source = source;

        public void Tick()
        {
            foreach (var tick in tickers)
            {
                tick();
            }
        }

        // ---------- data ----------

        private string Units => units ?? data.Units;

        public void Show(PanelData next, bool animate = false)
        {
            data = next ?? new PanelData();
            updated = data.Ts;
            spin.BeginAnimation(RotateTransform.AngleProperty, null);
            spin.Angle = 0;
            if (selected != "" && data.Providers.All(p => p.Id != selected))
            {
                selected = "";
            }
            // A scheduled data push must not erase a key while the user types.
            if (page == "settings" && apiEditing != "") return;
            var feedbackChanged = apiFeedback == "Key saved. Checking connection…";
            if (feedbackChanged) apiFeedback = "";
            if (!feedbackChanged && data.ContentKey != null && renderedKey == data.ContentKey)
            {
                Tick();
                return;
            }
            Rebuild(keepScroll: true);
        }

        public void ShowSettings()
        {
            page = "settings";
            Rebuild();
        }

        public void ShowDashboard()
        {
            ClearApiEditor();
            page = "dashboard";
            selected = "";
            Rebuild();
        }

        public void ShowUsage()
        {
            ClearApiEditor();
            if (page != "usage" || selected != "")
            {
                page = "usage";
                selected = "";
                Rebuild();
            }
            else scroller.ScrollToTop();
        }

        public void ClearApiEditor()
        {
            apiInput?.Clear();
            apiInput = null;
            apiEditing = apiFeedback = "";
        }

        /// <summary>For offscreen previews: open one provider's section.</summary>
        public void Select(string id)
        {
            selected = id ?? "";
            Rebuild();
        }

        private void Rebuild(bool keepScroll = false)
        {
            renderedKey = data.ContentKey;
            var offset = keepScroll ? scroller.VerticalOffset : 0;
            apiInput?.Clear();
            apiInput = null;
            content.Children.Clear();
            navigation.Children.Clear();
            tickers.Clear();
            sections.Clear();
            scroller.VerticalScrollBarVisibility = ScrollBarVisibility.Hidden;
            if (page == "dashboard")
            {
                navigation.Children.Add(Header("Dashboard", back: true));
                content.Children.Add(DashboardPage());
                Tick();
                scroller.UpdateLayout();
                scroller.ScrollToVerticalOffset(offset);
                return;
            }
            if (page == "settings")
            {
                content.Children.Add(Header("Settings", back: true));
                content.Children.Add(SettingsPage());
                Tick();
                scroller.ScrollToVerticalOffset(offset);
                return;
            }
            content.Children.Add(Header("Fleet", back: false));
            var band = Band();
            if (band != null)
            {
                content.Children.Add(band);
            }
            if (data.Providers.Count == 0 && data.Inactive.Count == 0)
            {
                content.Children.Add(Divider());
                var empty = Text("No usage to show yet", 12, theme.TextSecondary);
                empty.Margin = new Thickness(0, 14, 0, 14);
                empty.HorizontalAlignment = HorizontalAlignment.Center;
                content.Children.Add(empty);
            }
            foreach (var provider in data.Providers)
            {
                content.Children.Add(Section(provider));
            }
            foreach (var item in data.Inactive)
            {
                content.Children.Add(Divider());
                content.Children.Add(InactiveRow(item));
            }
            content.Children.Add(Divider());
            content.Children.Add(Footer());
            Tick();
            scroller.ScrollToVerticalOffset(offset);
        }

        // ---------- motion ----------

        /// <summary>Windows' "Animation effects" switch: off means no motion at all.</summary>
        private static bool Motion => SystemParameters.ClientAreaAnimation;

        /// <summary>A short travel keeps large popups from sweeping across the screen.</summary>
        private double Drop => 12;

        /// <summary>Before showing: the card waits below the taskbar's edge.</summary>
        public void PrepareIntro()
        {
            lift.BeginAnimation(TranslateTransform.YProperty, null);
            card.BeginAnimation(UIElement.OpacityProperty, null);
            card.Opacity = Motion ? 0 : 1;
            lift.Y = Motion ? Drop : 0;
        }

        /// <summary>The open: the card rises out of the taskbar and settles.</summary>
        public void PlayIntro()
        {
            if (!Motion)
            {
                lift.BeginAnimation(TranslateTransform.YProperty, null);
                lift.Y = 0;
                card.Opacity = 1;
                card.CacheMode = null;
                IntroMeasured?.Invoke("frames=0 motion=off");
                return;
            }
            busyUntil = DateTime.UtcNow.AddMilliseconds(220);
            // Cache the whole surface only during travel: nested blur/shadow layers
            // should not be recomposed at every animation frame. Text is live at rest.
            card.CacheMode = new BitmapCache { SnapsToDevicePixels = true };
            var entrance = Anim(lift.Y, 0, 200, 0, Decelerate);
            entrance.Completed += (s, e) => card.CacheMode = null;
            lift.BeginAnimation(TranslateTransform.YProperty, entrance);
            card.BeginAnimation(UIElement.OpacityProperty, Anim(card.Opacity, 1, 150, 0, Decelerate));
            var started = DateTime.UtcNow;
            var frames = new List<TimeSpan>();
            if (counting != null)
            {
                CompositionTarget.Rendering -= counting;
            }
            counting = (s, e) =>
            {
                // Rendering fires more than once per frame; RenderingTime only moves on a real frame.
                var at = ((RenderingEventArgs)e).RenderingTime;
                if (frames.Count == 0 || frames[frames.Count - 1] != at)
                {
                    frames.Add(at);
                }
                if ((DateTime.UtcNow - started).TotalMilliseconds > 360)
                {
                    CompositionTarget.Rendering -= counting;
                    counting = null;
                    IntroMeasured?.Invoke(Pacing(frames));
                }
            };
            CompositionTarget.Rendering += counting;
        }

        private static string Pacing(List<TimeSpan> frames)
        {
            if (frames.Count < 3)
            {
                return "frames=" + frames.Count;
            }
            var gaps = new List<double>();
            for (var i = 1; i < frames.Count; i++)
            {
                gaps.Add((frames[i] - frames[i - 1]).TotalMilliseconds);
            }
            gaps.Sort();
            var span = (frames[frames.Count - 1] - frames[0]).TotalSeconds;
            return string.Format(CultureInfo.InvariantCulture,
                "frames={0} fps={1:F0} median_ms={2:F2} p95_ms={3:F2} max_ms={4:F1}",
                frames.Count, (frames.Count - 1) / Math.Max(0.001, span), gaps[gaps.Count / 2],
                gaps[(int)(gaps.Count * 0.95)], gaps[gaps.Count - 1]);
        }

        /// <summary>Stop everything that runs per frame: nothing renders while closed.</summary>
        public void Rest()
        {
            card.CacheMode = null;
            spin.BeginAnimation(RotateTransform.AngleProperty, null);
            spin.Angle = 0;
            if (counting != null)
            {
                CompositionTarget.Rendering -= counting;
                counting = null;
            }
        }

        /// <summary>The close: the card drops back into the taskbar, speeding up as it goes.</summary>
        public void PlayOutro(Action finished)
        {
            busyUntil = DateTime.UtcNow.AddMilliseconds(140);
            card.CacheMode = new BitmapCache { SnapsToDevicePixels = true };
            card.BeginAnimation(UIElement.OpacityProperty, Anim(card.Opacity, 0, 110, 0, Accelerate));
            var drop = Anim(lift.Y, Drop, 120, 0, Accelerate);
            drop.Completed += (s, e) => finished();
            lift.BeginAnimation(TranslateTransform.YProperty, drop);
        }

        public void Spin()
        {
            var turn = new DoubleAnimation(0, 360, TimeSpan.FromMilliseconds(800)) { RepeatBehavior = RepeatBehavior.Forever };
            spin.BeginAnimation(RotateTransform.AngleProperty, turn);
        }

        private static DoubleAnimation Anim(double from, double to, double ms, double delay, IEasingFunction ease) =>
            new DoubleAnimation(from, to, TimeSpan.FromMilliseconds(ms))
            {
                BeginTime = TimeSpan.FromMilliseconds(delay),
                EasingFunction = ease,
                FillBehavior = FillBehavior.HoldEnd,
            };

        // ---------- header, totals, footer ----------

        private UIElement Header(string title, bool back)
        {
            var grid = new Grid { Height = 46 };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var left = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            if (back)
            {
                var button = IconButton("", "Back to usage", ShowUsage);
                button.Margin = new Thickness(-8, 0, 4, 0);
                left.Children.Add(button);
            }
            if (!back)
                left.Children.Add(new Border { Width = 6, Height = 18, CornerRadius = new CornerRadius(3), Background = Brush(theme.Accent), Margin = new Thickness(0, 0, 8, 0) });
            left.Children.Add(Text(title, 16, theme.Text, FontWeights.SemiBold));
            grid.Children.Add(left);
            if (!back || page == "dashboard")
            {
                var actions = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
                if (!back)
                {
                    var dashboard = Button("Dashboard", "View more usage in this popup", ShowDashboard);
                    dashboard.Margin = new Thickness(0, 0, 8, 0);
                    actions.Children.Add(dashboard);
                }
                var refresh = IconButton("", "Refresh usage now", () => { Spin(); RefreshClicked?.Invoke(); });
                ((TextBlock)refresh.Child).RenderTransformOrigin = new Point(0.5, 0.5);
                ((TextBlock)refresh.Child).RenderTransform = spin;
                actions.Children.Add(refresh);
                actions.Children.Add(IconButton("", "Settings", ShowSettings));
                actions.Margin = new Thickness(0, 0, -8, 0);
                Grid.SetColumn(actions, 1);
                grid.Children.Add(actions);
            }
            return grid;
        }

        private const string CostNote =
            "What the tokens in your local Claude Code and Codex logs would cost at each model's public API price. "
            + "Subscriptions don't charge per token: this is the value your plans returned.";

        /// <summary>The totals band: today, 7 days and 30 days at API list prices.</summary>
        private UIElement Band()
        {
            var summary = data.Summary;
            if (!data.Cost || summary == null || summary.State == "off")
            {
                return null;
            }
            if (summary.Today == null)
            {
                var note = Text("Reading local agent logs…", 12, theme.TextSecondary);
                note.Margin = new Thickness(0, 0, 0, 10);
                return note;
            }
            var grid = new Grid { Margin = new Thickness(12, 12, 12, 12), ToolTip = CostNote, Background = Brushes.Transparent };
            ToolTipService.SetInitialShowDelay(grid, 400);
            var cells = new[]
            {
                ("API value today", summary.Today),
                ("7 days", summary.Week),
                ("30 days", summary.Month),
            };
            for (var i = 0; i < cells.Length; i++)
            {
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(i == 0 ? 1.25 : 1, GridUnitType.Star) });
                var cell = new StackPanel();
                cell.Children.Add(Text(cells[i].Item1, 11, theme.TextSecondary));
                var value = Text(cells[i].Item2 == null ? "—" : Format.Money(cells[i].Item2.Value), 20, i == 0 ? theme.Accent : theme.Text, FontWeights.SemiBold, tabular: true);
                value.Margin = new Thickness(0, 1, 0, 0);
                cell.Children.Add(value);
                Grid.SetColumn(cell, i);
                grid.Children.Add(cell);
            }
            return new Border { Child = grid, CornerRadius = new CornerRadius(12), Background = Brush(Theme.WithAlpha(theme.Accent, theme.Light ? 0.07 : 0.10)), Margin = new Thickness(0, 2, 0, 12) };
        }

        private Border Divider() => new Border
        {
            Height = 1, Margin = new Thickness(0, 3, 0, 3), Background = Brush(theme.Divider), IsHitTestVisible = false,
        };

        private UIElement Footer()
        {
            var grid = new Grid { Margin = new Thickness(0, 7, 0, 0) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var when = Text("", 12, theme.TextTertiary);
            tickers.Add(() => when.Text = "Updated " + Clock.Ago(updated));
            grid.Children.Add(when);
            if (data.Cost && data.Summary?.Today != null)
            {
                var basis = Text(data.Summary.Estimated ? "Some prices estimated" : "At API list prices", 12, theme.TextTertiary);
                basis.ToolTip = CostNote;
                Grid.SetColumn(basis, 1);
                grid.Children.Add(basis);
            }
            return grid;
        }

        // ---------- provider sections ----------

        private sealed class SectionParts
        {
            public Reveal Reveal;
            public RotateTransform Chevron;
            public Provider Provider;
        }

        private bool Expandable(Provider p) => p.AccountMode != "" || p.Groups.Any(g => g.Rows.Count > 0) && p.Kind != "balance";

        private FrameworkElement Section(Provider p)
        {
            var open = selected == p.Id;
            var parts = new SectionParts { Provider = p };
            sections[p.Id] = parts;
            var expandable = Expandable(p);

            var summary = new StackPanel();
            summary.Children.Add(SectionHead(p, expandable, open, parts));
            var meters = new StackPanel { Margin = new Thickness(Indent, 3, 0, 0) };
            foreach (var row in MetersOf(p))
            {
                meters.Children.Add(MeterRow(row, p.FreshStale));
            }
            if (meters.Children.Count > 0)
            {
                summary.Children.Add(meters);
            }
            var status = StatusLine(p);
            if (status != null)
            {
                summary.Children.Add(status);
            }

            var hit = new Border
            {
                Child = summary,
                Padding = new Thickness(10, 10, 10, 10),
                Margin = new Thickness(0, 0, 0, 7),
                CornerRadius = new CornerRadius(11),
                Background = Brush(theme.ControlFill),
            };
            if (expandable)
            {
                hit.Cursor = Cursors.Hand;
                AutomationName(hit, p.Name + (open ? ", details shown" : ", show details"));
                hit.MouseEnter += (s, e) => hit.Background = Brush(theme.ControlHover);
                hit.MouseLeave += (s, e) => hit.Background = Brush(theme.ControlFill);
                OnClick(hit, () => Toggle(p.Id));
            }

            var stack = new StackPanel();
            stack.Children.Add(hit);
            parts.Reveal = new Reveal { Progress = open ? 1 : 0 };
            if (open)
            {
                parts.Reveal.Child = Expanded(p);
            }
            stack.Children.Add(parts.Reveal);
            return stack;
        }

        /// <summary>The limits shown while closed: the tray's choice, else every timed row.</summary>
        private IEnumerable<Row> MetersOf(Provider p)
        {
            if (p.Meters.Count > 0)
            {
                return p.Meters;
            }
            if (p.Kind == "balance")
            {
                return Enumerable.Empty<Row>();
            }
            return p.Groups.SelectMany(g => g.Rows).Where(r => r.Pct != null).Take(3);
        }

        private FrameworkElement SectionHead(Provider p, bool expandable, bool open, SectionParts parts)
        {
            var grid = new Grid { MinHeight = 24 };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(Indent) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var icon = Icon(p.Icon, p.Mark, 16);
            icon.HorizontalAlignment = HorizontalAlignment.Left;
            grid.Children.Add(icon);

            var name = new TextBlock { VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
            name.Inlines.Add(Run(p.Name, 14, theme.Text, FontWeights.SemiBold));
            if (p.Plan != "")
            {
                name.Inlines.Add(Run("  " + p.Plan, 12, theme.TextTertiary));
            }
            Grid.SetColumn(name, 1);
            grid.Children.Add(name);

            var right = new TextBlock { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 0, 0) };
            Typography.SetNumeralAlignment(right, FontNumeralAlignment.Tabular);
            if (data.Cost && p.Spend?.Today != null)
            {
                right.Inlines.Add(Run(Format.Money(p.Spend.Today.Cost), 13, theme.Text, FontWeights.SemiBold));
                right.Inlines.Add(Run(" today", 12, theme.TextSecondary));
            }
            else if (p.AccountMode == "connection")
            {
                right.Inlines.Add(Run("View balance", 12, theme.TextSecondary));
            }
            else if (p.Balance != null)
            {
                right.Inlines.Add(Run(p.Balance.Value, 13, theme.Text, FontWeights.SemiBold));
                right.Inlines.Add(Run(p.Balance.Label == "Credit balance" ? " credit" : " " + p.Balance.Label.ToLowerInvariant(), 12, theme.TextSecondary));
            }
            Grid.SetColumn(right, 2);
            grid.Children.Add(right);

            if (expandable)
            {
                parts.Chevron = new RotateTransform(open ? 180 : 0);
                var chevron = new TextBlock
                {
                    Text = "", FontFamily = IconFont, FontSize = 10, Foreground = Brush(theme.TextTertiary),
                    VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(10, 1, 0, 0),
                    RenderTransformOrigin = new Point(0.5, 0.5), RenderTransform = parts.Chevron,
                };
                Grid.SetColumn(chevron, 3);
                grid.Children.Add(chevron);
            }
            return grid;
        }

        private double Shown(double pct) => Units == "left" ? 100 - pct : pct;

        private FrameworkElement MeterRow(Row row, bool stale)
        {
            var grid = new Grid { Height = 22 };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(LabelColumn) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(42) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(54) });
            grid.Children.Add(Text(ShortLabel(row), 12, theme.TextSecondary));
            if (row.Pct == null)
            {
                var value = Text(row.Value, 12, theme.Text, FontWeights.SemiBold, tabular: true);
                value.HorizontalAlignment = HorizontalAlignment.Right;
                Grid.SetColumn(value, 1);
                Grid.SetColumnSpan(value, 3);
                grid.Children.Add(value);
                return grid;
            }
            var pct = row.Pct.Value;
            var meter = new Meter(theme, Shown(pct), theme.ForState(row.State), Units == "left" ? null : row.Elapsed)
            {
                Margin = new Thickness(6, 0, 10, 0), VerticalAlignment = VerticalAlignment.Center,
            };
            Grid.SetColumn(meter, 1);
            grid.Children.Add(meter);
            var number = Text(Math.Round(Shown(pct)).ToString(CultureInfo.InvariantCulture) + "%", 12, theme.Text, FontWeights.SemiBold, tabular: true);
            number.HorizontalAlignment = HorizontalAlignment.Right;
            Grid.SetColumn(number, 2);
            grid.Children.Add(number);
            var reset = Text("", 12, theme.TextSecondary, tabular: true);
            reset.HorizontalAlignment = HorizontalAlignment.Right;
            Grid.SetColumn(reset, 3);
            grid.Children.Add(reset);
            tickers.Add(() => reset.Text = Clock.Left(row.ResetAt));
            if (stale)
            {
                grid.Opacity = 0.55;
            }
            return grid;
        }

        private static string ShortLabel(Row row)
        {
            if (row.Short != "") return row.Short;
            var text = row.Label.EndsWith(" limit") ? row.Label.Substring(0, row.Label.Length - " limit".Length) : row.Label;
            return text.EndsWith(" weekly") ? text.Substring(0, text.Length - " weekly".Length) + " wk" : text;
        }

        /// <summary>One line, only when something needs attention: a stale reading, a
        /// limit reached or almost reached, or one on course to run out early.</summary>
        private FrameworkElement StatusLine(Provider p)
        {
            string glyph, text;
            Color color;
            if (p.FreshKind != "")
            {
                var at = p.FreshAt ?? Clock.Now();
                var why = p.FreshKind != "cached" ? "From the " + p.FreshKind + " app" : p.FreshReason != "" ? p.FreshReason : "Cached";
                glyph = "";
                color = theme.TextSecondary;
                text = why + " · " + Clock.Age(at) + " old";
                if (!p.FreshStale && p.FreshKind == "cached" && p.FreshReason == "")
                {
                    return null;
                }
            }
            else
            {
                var c = p.Caption;
                if (c == null || (c.Tone != "warn" && c.Tone != "high"))
                {
                    return null;
                }
                switch (c.Kind)
                {
                    case "full":
                        glyph = "";
                        color = theme.CriticalText;
                        text = c.Label + " limit reached · resets " + Clock.In(c.At);
                        break;
                    case "runout":
                        glyph = "";
                        color = theme.CautionText;
                        text = c.Label + " runs out " + Clock.Soon(c.At) + " at this pace";
                        break;
                    case "reset":
                        glyph = "";
                        color = theme.CriticalText;
                        text = c.Label + " almost used up · resets " + Clock.In(c.At);
                        break;
                    default:
                        return null;
                }
            }
            var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(Indent, 4, 0, 0) };
            row.Children.Add(new TextBlock
            {
                Text = glyph, FontFamily = IconFont, FontSize = 12, Foreground = Brush(color),
                VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 1, 6, 0),
            });
            var label = Text(text, 12, theme.TextSecondary);
            row.Children.Add(label);
            if (p.FreshKind == "" && p.Caption != null)
            {
                var caption = p.Caption;
                tickers.Add(() =>
                {
                    if (caption.Kind == "full") label.Text = caption.Label + " limit reached · resets " + Clock.In(caption.At);
                    else if (caption.Kind == "reset") label.Text = caption.Label + " almost used up · resets " + Clock.In(caption.At);
                });
            }
            return row;
        }

        // ---------- an open section ----------

        private void Toggle(string id)
        {
            var closing = selected != "" && sections.TryGetValue(selected, out var old) ? old : null;
            selected = selected == id ? "" : id;
            if (closing != null)
            {
                Collapse(closing);
            }
            if (selected != "" && sections.TryGetValue(selected, out var next))
            {
                Expand(next);
            }
        }

        private void Expand(SectionParts parts)
        {
            var body = Expanded(parts.Provider);
            parts.Reveal.Child = body;
            parts.Chevron?.BeginAnimation(RotateTransform.AngleProperty, Anim(parts.Chevron.Angle, 180, 200, 0, Decelerate));
            if (!Motion)
            {
                parts.Reveal.Progress = 1;
                return;
            }
            busyUntil = DateTime.UtcNow.AddMilliseconds(300);
            body.Opacity = 0;
            body.BeginAnimation(UIElement.OpacityProperty, Anim(0, 1, 170, 70, Decelerate));
            parts.Reveal.BeginAnimation(Reveal.ProgressProperty, Anim(parts.Reveal.Progress, 1, 280, 0, Decelerate));
        }

        private void Collapse(SectionParts parts)
        {
            parts.Chevron?.BeginAnimation(RotateTransform.AngleProperty, Anim(parts.Chevron.Angle, 0, 200, 0, Decelerate));
            if (!Motion || parts.Reveal.Child == null)
            {
                parts.Reveal.BeginAnimation(Reveal.ProgressProperty, null);
                parts.Reveal.Progress = 0;
                parts.Reveal.Child = null;
                return;
            }
            busyUntil = DateTime.UtcNow.AddMilliseconds(260);
            var body = parts.Reveal.Child;
            body.BeginAnimation(UIElement.OpacityProperty, Anim(body.Opacity, 0, 110, 0, Standard));
            var shrink = Anim(parts.Reveal.Progress, 0, 220, 0, Standard);
            shrink.Completed += (s, e) =>
            {
                if (selected != parts.Provider.Id && parts.Reveal.Child == body)
                {
                    parts.Reveal.Child = null;
                }
            };
            parts.Reveal.BeginAnimation(Reveal.ProgressProperty, shrink);
        }

        /// <summary>
        /// The open section: the last day's activity, then a two-column sheet of the
        /// figures behind the meters: burn rate, spend per span, models, projects, and
        /// how every limit is pacing.
        /// </summary>
        private FrameworkElement Expanded(Provider p)
        {
            var stack = new StackPanel { Margin = new Thickness(Indent, 2, 0, 10), Background = Brushes.Transparent };
            if (p.AccountMode != "")
            {
                if (p.AccountMode == "spend")
                {
                    var billing = new Grid();
                    billing.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(KeyColumn) });
                    billing.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                    foreach (var row in p.Groups.SelectMany(g => g.Rows)) Pair(billing, row.Label, Value(row.Value));
                    stack.Children.Add(billing);
                }
                var note = Text(p.AccountMode == "spend"
                    ? "Calendar-month spend in UTC. Reports can lag by 5 minutes or more and exclude Priority Tier."
                    : "Key connected. Add an admin key in Settings to read organization spending.", 12, theme.TextSecondary);
                note.TextWrapping = TextWrapping.Wrap;
                note.TextTrimming = TextTrimming.None;
                note.Margin = new Thickness(0, 4, 0, 6);
                stack.Children.Add(note);
                var creditNote = Text("Included Max and Team credits renew on your billing date. View their balance and expiry under Promotional credits in Console.", 12, theme.TextSecondary);
                creditNote.TextWrapping = TextWrapping.Wrap;
                creditNote.TextTrimming = TextTrimming.None;
                creditNote.Margin = new Thickness(0, 0, 0, 8);
                stack.Children.Add(creditNote);
                stack.Children.Add(Button("Open billing", "View prepaid credits in Claude Console", () => ActionClicked?.Invoke("anthropic-billing")));
                return stack;
            }
            var spend = data.Cost ? p.Spend : null;
            if (spend != null && spend.Hourly.Count > 1 && spend.Hourly.Any(v => v > 0))
            {
                var top = new Grid { Margin = new Thickness(0, 4, 0, 6) };
                top.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                top.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                top.Children.Add(Text("Last 24 hours at API prices", 12, theme.TextSecondary));
                var peak = Text("peak " + Format.Money(spend.Hourly.Max()) + "/h", 12, theme.TextTertiary, tabular: true);
                Grid.SetColumn(peak, 1);
                top.Children.Add(peak);
                stack.Children.Add(top);
                stack.Children.Add(new Activity(theme, spend.Hourly) { Height = 40 });
                var axis = new Grid { Margin = new Thickness(0, 3, 0, 8) };
                axis.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                axis.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                axis.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                var start = Text("24 h ago", 11, theme.TextTertiary);
                var middle = Text("12 h", 11, theme.TextTertiary);
                middle.HorizontalAlignment = HorizontalAlignment.Center;
                var end = Text("now", 11, theme.TextTertiary);
                end.HorizontalAlignment = HorizontalAlignment.Right;
                Grid.SetColumn(middle, 1);
                Grid.SetColumn(end, 2);
                axis.Children.Add(start);
                axis.Children.Add(middle);
                axis.Children.Add(end);
                stack.Children.Add(axis);
            }

            var sheet = new Grid();
            sheet.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(KeyColumn) });
            sheet.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            if (spend != null)
            {
                if (spend.CostPerHour != null && spend.CostPerHour > 0)
                {
                    var rate = Value(Format.Money(spend.CostPerHour.Value) + "/h");
                    if (spend.TokensPerMin != null)
                    {
                        rate.Inlines.Add(Run(" · " + Format.Tokens(spend.TokensPerMin.Value) + " tokens/min", 12, theme.TextSecondary));
                    }
                    Pair(sheet, "Burn rate", rate);
                }
                else
                {
                    Pair(sheet, "Burn rate", Value("Idle", secondary: true));
                }
                if (spend.Window != null)
                {
                    var label = spend.WindowLabel != "" ? spend.WindowLabel + " window" : "This window";
                    Pair(sheet, label, Spent(spend.Window, cacheShare: false));
                }
                if (spend.Today != null) Pair(sheet, "Today", Spent(spend.Today, cacheShare: true));
                if (spend.Week != null) Pair(sheet, "7 days", Spent(spend.Week, cacheShare: false));
                if (spend.Month != null) Pair(sheet, "30 days", Spent(spend.Month, cacheShare: false));
                if (spend.Models.Count > 0)
                {
                    var models = Rich();
                    AddShares(models, spend.Models.Take(2), percent: true);
                    Pair(sheet, "Models, 7 d", models);
                }
                if (spend.Projects.Count > 0)
                {
                    var projects = Rich();
                    AddShares(projects, spend.Projects.Take(2), percent: false);
                    Pair(sheet, "Projects, 7 d", projects);
                }
            }
            stack.Children.Add(sheet);
            var limits = Limits(p);
            if (limits != null)
            {
                stack.Children.Add(limits);
            }
            if (spend != null && spend.Estimated)
            {
                var note = Text("Some models priced by their family's rate.", 11, theme.TextTertiary);
                note.Margin = new Thickness(0, 4, 0, 0);
                stack.Children.Add(note);
            }
            return stack;
        }

        private static TextBlock Rich()
        {
            var block = new TextBlock { TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Top };
            Typography.SetNumeralAlignment(block, FontNumeralAlignment.Tabular);
            return block;
        }

        private TextBlock Value(string text, bool secondary = false)
        {
            var block = Rich();
            block.Inlines.Add(Run(text, 12, secondary ? theme.TextSecondary : theme.Text, secondary ? FontWeights.Normal : FontWeights.SemiBold));
            return block;
        }

        private TextBlock Spent(Totals t, bool cacheShare)
        {
            var block = Value(Format.Money(t.Cost));
            var detail = " · " + Format.Tokens(t.Tokens) + " tokens";
            var input = t.Input + t.CacheRead + t.CacheWrite;
            if (cacheShare && input > 0 && t.CacheRead > 0)
            {
                detail += " · " + Format.Percent(t.CacheRead / input) + " from cache";
            }
            block.Inlines.Add(Run(detail, 12, theme.TextSecondary));
            return block;
        }

        private void AddShares(TextBlock block, IEnumerable<Share> shares, bool percent)
        {
            var first = true;
            foreach (var share in shares)
            {
                if (!first) block.Inlines.Add(Run(" · ", 12, theme.TextTertiary));
                block.Inlines.Add(Run(share.Name, 12, theme.Text));
                block.Inlines.Add(Run(" " + (percent ? Format.Percent(share.Part) : Format.Money(share.Cost, compact: true)), 12, theme.TextSecondary));
                first = false;
            }
        }

        /// <summary>
        /// Every limit as one aligned row: how much is used, how fast it is going
        /// lately (Fleet's own readings), how that compares with an even pace, and
        /// when it resets.
        /// </summary>
        private FrameworkElement Limits(Provider p)
        {
            var rows = p.Groups.SelectMany(g => g.Rows.Select(r => new { Group = g.Title, Row = r })).ToList();
            if (rows.Count == 0)
            {
                return null;
            }
            var table = new Grid { Margin = new Thickness(0, p.Spend != null && data.Cost ? 10 : 2, 0, 0) };
            foreach (var width in new[] { 54.0, 34, 40, -1, 72 })
            {
                table.ColumnDefinitions.Add(new ColumnDefinition { Width = width < 0 ? new GridLength(1, GridUnitType.Star) : new GridLength(width) });
            }
            var head = new[] { "Limit", "Used", "Rate", "Pace", "Resets" };
            table.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            for (var c = 0; c < head.Length; c++)
            {
                var cell = Text(head[c], 11, theme.TextTertiary);
                cell.Margin = new Thickness(c == 3 ? 8 : 0, 0, 0, 3);
                cell.HorizontalAlignment = c == 1 || c == 2 || c == 4 ? HorizontalAlignment.Right : HorizontalAlignment.Left;
                Grid.SetColumn(cell, c);
                table.Children.Add(cell);
            }
            foreach (var item in rows)
            {
                var row = item.Row;
                var r = table.RowDefinitions.Count;
                table.RowDefinitions.Add(new RowDefinition { Height = new GridLength(20) });
                var label = (item.Group != "" ? item.Group + " " : "") + ShortLabel(row).ToLowerInvariant();
                label = label.Length > 0 ? char.ToUpperInvariant(label[0]) + label.Substring(1) : "Usage";
                Cell(table, r, 0, Text(label, 12, theme.TextSecondary), HorizontalAlignment.Left);
                if (row.Pct == null)
                {
                    var value = Text(row.Value, 12, theme.Text, FontWeights.SemiBold, tabular: true);
                    Grid.SetColumnSpan(value, 4);
                    Cell(table, r, 1, value, HorizontalAlignment.Right);
                    continue;
                }
                Cell(table, r, 1, Text(Math.Round(Shown(row.Pct.Value)).ToString(CultureInfo.InvariantCulture) + "%", 12, theme.Text, FontWeights.SemiBold, tabular: true), HorizontalAlignment.Right);
                var rate = row.Rate != null && row.Rate >= 0.5 ? Math.Round(row.Rate.Value).ToString(CultureInfo.InvariantCulture) + "%/h" : "—";
                Cell(table, r, 2, Text(rate, 12, theme.TextSecondary, tabular: true), HorizontalAlignment.Right);
                var pace = Pace(row, out var tone);
                var paceCell = Text(pace, 12, tone);
                paceCell.Margin = new Thickness(8, 0, 0, 0);
                Cell(table, r, 3, paceCell, HorizontalAlignment.Left);
                var reset = Text("", 12, theme.TextSecondary, tabular: true);
                tickers.Add(() => reset.Text = row.ResetAt == null ? "" : Clock.Day(row.ResetAt));
                Cell(table, r, 4, reset, HorizontalAlignment.Right);
            }
            return table;
        }

        private static void Cell(Grid table, int row, int column, FrameworkElement element, HorizontalAlignment align)
        {
            element.HorizontalAlignment = align;
            Grid.SetRow(element, row);
            Grid.SetColumn(element, column);
            table.Children.Add(element);
        }

        /// <summary>"runs out ~11:58 PM", "26% ahead", "45% under", "on pace".</summary>
        private string Pace(Row row, out Color tone)
        {
            tone = theme.TextSecondary;
            if (row.State == "full")
            {
                tone = theme.CriticalText;
                return "limit reached";
            }
            if (row.RunoutAt != null)
            {
                tone = theme.CautionText;
                return "runs out " + Clock.Soon(row.RunoutAt);
            }
            if (row.Pct == 0)
            {
                return "unused";
            }
            if (row.Reserve == null)
            {
                return "—";
            }
            var reserve = row.Reserve.Value;
            if (reserve <= -0.5) return Math.Round(-reserve).ToString(CultureInfo.InvariantCulture) + "% ahead";
            if (reserve >= 0.5) return Math.Round(reserve).ToString(CultureInfo.InvariantCulture) + "% under";
            return "on pace";
        }

        private void Pair(Grid sheet, string key, TextBlock value)
        {
            var row = sheet.RowDefinitions.Count;
            sheet.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var label = Text(key, 12, theme.TextSecondary);
            label.VerticalAlignment = VerticalAlignment.Top;
            label.Margin = new Thickness(0, 2, 8, 2);
            Grid.SetRow(label, row);
            sheet.Children.Add(label);
            value.Margin = new Thickness(0, 2, 0, 2);
            Grid.SetRow(value, row);
            Grid.SetColumn(value, 1);
            sheet.Children.Add(value);
        }

        // ---------- logins that need a hand ----------

        private FrameworkElement InactiveRow(Inactive item)
        {
            var grid = new Grid { MinHeight = 36, Margin = new Thickness(0, 2, 0, 2) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(Indent) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var icon = Icon(item.Icon, item.Mark, 16);
            icon.Opacity = 0.6;
            icon.HorizontalAlignment = HorizontalAlignment.Left;
            grid.Children.Add(icon);
            var text = new TextBlock { VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
            text.Inlines.Add(Run(item.Name, 14, theme.TextSecondary, FontWeights.SemiBold));
            text.Inlines.Add(Run("  " + item.Label, 12, item.Action != "" ? theme.CautionText : theme.TextTertiary));
            text.ToolTip = item.Tooltip;
            Grid.SetColumn(text, 1);
            grid.Children.Add(text);
            if (item.Action != "")
            {
                var button = Button(item.Action == "claude-sign-in" ? "Sign in" : item.Action == "api-settings" ? "Settings" : "Renew", item.Tooltip,
                    () => { if (item.Action == "api-settings") ShowSettings(); else ActionClicked?.Invoke(item.Action); });
                Grid.SetColumn(button, 2);
                grid.Children.Add(button);
            }
            return grid;
        }

        // ---------- settings ----------

        private UIElement SettingsPage()
        {
            var stack = new StackPanel { Margin = new Thickness(0, 0, 0, 4) };
            stack.Children.Add(SectionLabel("Show", first: true));
            stack.Children.Add(SettingRow("Percentages", null, Segmented(new[] { ("used", "Used"), ("left", "Left") }, Units, value =>
            {
                units = value;
                SettingChanged?.Invoke("units", value);
                Rebuild();
            })));
            stack.Children.Add(SwitchRow("Cost at API prices",
                "What your Claude Code and Codex tokens would cost on the API, read from their local logs.",
                data.Cost, on =>
                {
                    data.Cost = on;
                    SettingChanged?.Invoke("cost", on);
                    Rebuild();
                }));

            stack.Children.Add(SectionLabel("Claude API", first: false));
            var apiNote = Text("Included Max and Team credits are shown in Console. An admin key can read organization spend.", 12, theme.TextSecondary);
            apiNote.TextWrapping = TextWrapping.Wrap;
            apiNote.TextTrimming = TextTrimming.None;
            apiNote.Margin = new Thickness(0, 0, 0, 6);
            stack.Children.Add(apiNote);
            stack.Children.Add(ApiKeyRow("anthropic", "API key"));
            stack.Children.Add(ApiKeyRow("anthropic-admin", "Admin key"));
            stack.Children.Add(ApiKeyRow("anthropic-backup", "Backup API key"));
            stack.Children.Add(ApiKeyRow("anthropic-backup-admin", "Backup admin key"));
            var billingLink = Button("Open billing", "View included credits and expiry in Console; no saved key required",
                () => ActionClicked?.Invoke("anthropic-billing"));
            billingLink.Margin = new Thickness(0, 6, 0, 0);
            stack.Children.Add(billingLink);
            if (apiFeedback != "")
            {
                var feedback = Text(apiFeedback, 12, theme.TextSecondary);
                feedback.TextWrapping = TextWrapping.Wrap;
                feedback.TextTrimming = TextTrimming.None;
                feedback.Margin = new Thickness(0, 4, 0, 4);
                stack.Children.Add(feedback);
            }

            stack.Children.Add(SectionLabel("Tray icon", first: false));
            stack.Children.Add(Segmented(new[] { ("bars", "Bars"), ("ring", "Ring"), ("number", "Number") }, data.TrayIcon, value =>
            {
                data.TrayIcon = value;
                SettingChanged?.Invoke("tray_icon", value);
                Rebuild();
            }));

            stack.Children.Add(SectionLabel("Alerts", first: false));
            stack.Children.Add(SwitchRow("Notify me",
                "At 80% and 95%, when a limit will run out before it resets, and when it resets.",
                data.Alerts, on =>
                {
                    data.Alerts = on;
                    SettingChanged?.Invoke("alerts", on);
                    Rebuild();
                }));

            if (data.Known.Count > 0)
            {
                stack.Children.Add(SectionLabel("Providers", first: false));
                foreach (var known in data.Known)
                {
                    var entry = known;
                    stack.Children.Add(SwitchRow(entry.Name, null, !entry.Hidden, on =>
                    {
                        entry.Hidden = !on;
                        SettingChanged?.Invoke("hidden", data.Known.Where(k => k.Hidden).Select(k => k.Id).ToArray());
                        Rebuild();
                    }));
                }
            }
            stack.Children.Add(Divider());
            var version = Text("Fleet " + typeof(PanelView).Assembly.GetName().Version.ToString(3), 12, theme.TextTertiary);
            version.Margin = new Thickness(0, 7, 0, 0);
            stack.Children.Add(version);
            return stack;
        }

        private UIElement ApiKeyRow(string id, string title)
        {
            data.ApiAccounts.TryGetValue(id, out var source);
            var enabled = !data.DisabledApiAccounts.Contains(id);
            var stack = new StackPanel();
            var actions = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            if (source != "environment")
            {
                var connect = Button(source == "windows" ? "Change" : "Connect", "Save a key in Windows Credential Manager", () =>
                {
                    apiInput?.Clear();
                    apiEditing = apiEditing == id ? "" : id;
                    apiFeedback = "";
                    Rebuild();
                });
                connect.IsEnabled = enabled;
                connect.Opacity = enabled ? 1 : 0.45;
                actions.Children.Add(connect);
                if (source == "windows")
                {
                    var remove = Button("Remove", "Remove Fleet's saved key from Windows Credential Manager", () =>
                    {
                        var error = CredentialChanged?.Invoke(id, null) ?? "Key storage is unavailable.";
                        if (error == "") data.ApiAccounts[id] = "";
                        apiFeedback = error == "" ? "Saved key removed." : error;
                        apiEditing = "";
                        Rebuild();
                    });
                    remove.Margin = new Thickness(6, 0, 0, 0);
                    actions.Children.Add(remove);
                }
            }
            stack.Children.Add(SettingRow(title, !enabled ? "Disabled in providers.json" : source == "environment" ? "From environment · managed outside Fleet"
                : source == "windows" ? "Saved on this PC" : "No key saved", actions));
            if (apiEditing != id) return stack;
            var input = new PasswordBox
            {
                FontFamily = TextFont, FontSize = 14, MaxLength = 1024,
                Padding = new Thickness(8, 6, 8, 6), Margin = new Thickness(0, 5, 0, 6),
                Foreground = Brush(theme.Text), Background = Brush(theme.ControlFill),
                BorderBrush = Brush(theme.ControlStroke), BorderThickness = new Thickness(1),
                CaretBrush = Brush(theme.Text),
            };
            apiInput = input;
            input.Loaded += (s, e) => input.Focus();
            AutomationName(input, title + ", hidden key");
            stack.Children.Add(input);
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 8) };
            Action save = () =>
            {
                string error;
                try { error = CredentialChanged?.Invoke(id, input.Password) ?? "Key storage is unavailable."; }
                finally { input.Clear(); }
                if (error == "")
                {
                    data.ApiAccounts[id] = "windows";
                    apiEditing = "";
                }
                apiFeedback = error == "" ? "Key saved. Checking connection…" : error;
                Rebuild();
            };
            buttons.Children.Add(Button("Save key", "Save securely and refresh usage", save));
            var cancel = Button("Cancel", "Discard this key", () => { input.Clear(); apiEditing = ""; Rebuild(); });
            cancel.Margin = new Thickness(6, 0, 0, 0);
            buttons.Children.Add(cancel);
            input.KeyDown += (s, e) => { if (e.Key == Key.Enter) { save(); e.Handled = true; } };
            stack.Children.Add(buttons);
            return stack;
        }

        private UIElement SectionLabel(string text, bool first)
        {
            var label = Text(text, 12, theme.TextSecondary, FontWeights.SemiBold);
            label.Margin = new Thickness(0, first ? 2 : 14, 0, 6);
            return label;
        }

        private UIElement SettingRow(string title, string note, UIElement control)
        {
            var grid = new Grid { MinHeight = 36, Margin = new Thickness(0, 0, 0, 2) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var text = new StackPanel { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 12, 0) };
            text.Children.Add(Text(title, 14, theme.Text));
            if (note != null)
            {
                var hint = Text(note, 12, theme.TextSecondary);
                hint.TextWrapping = TextWrapping.Wrap;
                hint.TextTrimming = TextTrimming.None;
                hint.Margin = new Thickness(0, 1, 0, 0);
                text.Children.Add(hint);
            }
            grid.Children.Add(text);
            Grid.SetColumn(control, 1);
            grid.Children.Add(control);
            return grid;
        }

        private FrameworkElement Segmented((string Value, string Text)[] options, string current, Action<string> pick)
        {
            var track = new Border
            {
                CornerRadius = new CornerRadius(6),
                Background = Brush(theme.Hover),
                BorderBrush = Brush(theme.Divider),
                BorderThickness = new Thickness(1),
                Padding = new Thickness(2),
                MinWidth = 150,
                VerticalAlignment = VerticalAlignment.Center,
            };
            var row = new UniformGrid { Rows = 1, Columns = options.Length };
            foreach (var option in options)
            {
                var on = option.Value == current;
                var label = Text(option.Text, 13, on ? theme.Text : theme.TextSecondary, on ? FontWeights.SemiBold : FontWeights.Normal);
                label.HorizontalAlignment = HorizontalAlignment.Center;
                var segment = new Border
                {
                    CornerRadius = new CornerRadius(4),
                    Background = on ? Brush(theme.Light ? Colors.White : Theme.WithAlpha(Colors.White, 0.09)) : Brushes.Transparent,
                    BorderBrush = on ? Brush(theme.ControlStroke) : Brushes.Transparent,
                    BorderThickness = new Thickness(on ? 1 : 0),
                    Padding = new Thickness(12, 4, 12, 5),
                    Margin = new Thickness(on ? 0 : 1),
                    Child = label,
                    Cursor = on ? Cursors.Arrow : Cursors.Hand,
                };
                AutomationName(segment, option.Text);
                if (!on)
                {
                    var value = option.Value;
                    segment.MouseEnter += (s, e) => segment.Background = Brush(theme.Hover);
                    segment.MouseLeave += (s, e) => segment.Background = Brushes.Transparent;
                    OnClick(segment, () => pick(value));
                }
                row.Children.Add(segment);
            }
            track.Child = row;
            return track;
        }

        private UIElement SwitchRow(string title, string note, bool on, Action<bool> set)
        {
            // Windows 11's toggle: an outlined pill with a small knob when off, the
            // accent with a white knob when on.
            var knob = new Ellipse
            {
                Width = 12, Height = 12,
                Fill = Brush(on ? theme.OnAccent : theme.StrongFill),
                HorizontalAlignment = on ? HorizontalAlignment.Right : HorizontalAlignment.Left,
                VerticalAlignment = VerticalAlignment.Center,
            };
            var toggle = new Border
            {
                Width = 40, Height = 20, CornerRadius = new CornerRadius(10),
                Background = on ? Brush(theme.Accent) : Brushes.Transparent,
                BorderBrush = Brush(on ? theme.Accent : theme.StrongStroke),
                BorderThickness = new Thickness(1),
                Padding = new Thickness(3, 0, 3, 0),
                VerticalAlignment = VerticalAlignment.Center,
                Child = knob,
            };
            var row = (Grid)SettingRow(title, note, toggle);
            row.Background = Brushes.Transparent;
            row.Cursor = Cursors.Hand;
            AutomationName(row, title + (on ? ", on" : ", off"));
            OnClick(row, () => set(!on));
            return row;
        }

        // ---------- building blocks ----------

        /// <summary>A slim usage bar with a notch where an even pace would be.</summary>
        private sealed class Meter : FrameworkElement
        {
            private readonly Theme theme;
            private readonly double value;
            private readonly Color fill;
            private readonly double? pace;

            public Meter(Theme theme, double value, Color fill, double? pace)
            {
                this.theme = theme;
                this.value = Math.Max(0, Math.Min(100, value));
                this.fill = fill;
                this.pace = pace;
                Height = 10;
                SnapsToDevicePixels = true;
            }

            protected override void OnRender(DrawingContext dc)
            {
                var w = ActualWidth;
                var y = Math.Round(ActualHeight / 2) - 2;
                dc.DrawRoundedRectangle(Brush(theme.Track), null, new Rect(0, y, w, 4), 2, 2);
                if (value > 0)
                {
                    var length = Math.Max(4, Math.Round(w * value / 100));
                    dc.DrawRoundedRectangle(Brush(fill), null, new Rect(0, y, length, 4), 2, 2);
                }
                if (pace != null && pace > 0.02 && pace < 0.98)
                {
                    var x = Math.Round(w * pace.Value);
                    dc.DrawRectangle(Brush(Theme.WithAlpha(theme.Text, theme.Light ? 0.55 : 0.75)), null, new Rect(x, y - 2, 1, 8));
                }
            }
        }

        /// <summary>A day of hourly bars, oldest first; the current hour in full colour.</summary>
        private sealed class Activity : FrameworkElement
        {
            private readonly Theme theme;
            private readonly double[] values;

            public Activity(Theme theme, IList<double> values)
            {
                this.theme = theme;
                this.values = values.ToArray();
                SnapsToDevicePixels = true;
            }

            protected override void OnRender(DrawingContext dc)
            {
                var w = ActualWidth;
                var h = ActualHeight;
                var n = values.Length;
                dc.DrawRectangle(Brush(theme.Divider), null, new Rect(0, h - 1, w, 1));
                var max = values.Max();
                if (n == 0 || max <= 0)
                {
                    return;
                }
                const double gap = 2;
                var bar = (w - gap * (n - 1)) / n;
                var dim = Brush(Theme.WithAlpha(theme.Accent, 0.55));
                var full = Brush(theme.Accent);
                for (var i = 0; i < n; i++)
                {
                    if (values[i] <= 0)
                    {
                        continue;
                    }
                    var height = Math.Max(2, Math.Round((h - 2) * values[i] / max));
                    var x = Math.Round(i * (bar + gap));
                    var right = Math.Round(i * (bar + gap) + bar);
                    var radius = Math.Min(1.5, (right - x) / 2);
                    dc.DrawRoundedRectangle(i == n - 1 ? full : dim, null, new Rect(x, h - 1 - height, Math.Max(1, right - x), height), radius, radius);
                }
            }
        }

        /// <summary>Shows a share of its child's height, clipping the rest: a section
        /// opens by animating that share, with no layout cost beyond its own subtree.</summary>
        private sealed class Reveal : Decorator
        {
            public static readonly DependencyProperty ProgressProperty = DependencyProperty.Register(
                "Progress", typeof(double), typeof(Reveal), new FrameworkPropertyMetadata(0.0, FrameworkPropertyMetadataOptions.AffectsMeasure));

            public Reveal() { ClipToBounds = true; }

            public double Progress
            {
                get => (double)GetValue(ProgressProperty);
                set => SetValue(ProgressProperty, value);
            }

            protected override Size MeasureOverride(Size constraint)
            {
                if (Child == null)
                {
                    return new Size(0, 0);
                }
                Child.Measure(new Size(constraint.Width, double.PositiveInfinity));
                return new Size(Child.DesiredSize.Width, Child.DesiredSize.Height * Math.Max(0, Math.Min(1, Progress)));
            }

            protected override Size ArrangeOverride(Size size)
            {
                Child?.Arrange(new Rect(0, 0, size.Width, Child.DesiredSize.Height));
                return size;
            }
        }

        /// <summary>Holds its child at a set size (the window's) and asks for none itself:
        /// the backdrop must never feed its size back into the card's.</summary>
        private sealed class Pinned : Decorator
        {
            public Size Extent;

            protected override Size MeasureOverride(Size constraint)
            {
                Child?.Measure(Extent);
                return new Size(0, 0);
            }

            protected override Size ArrangeOverride(Size size)
            {
                Child?.Arrange(new Rect(Extent));
                return size;
            }
        }

        /// <summary>A CSS-style cubic Bézier timing curve, solved by bisection.</summary>
        private sealed class Bezier : EasingFunctionBase
        {
            private readonly double x1, y1, x2, y2;

            public Bezier(double x1, double y1, double x2, double y2)
            {
                this.x1 = x1;
                this.y1 = y1;
                this.x2 = x2;
                this.y2 = y2;
                EasingMode = EasingMode.EaseIn;
            }

            private static double Curve(double s, double a, double b) =>
                3 * (1 - s) * (1 - s) * s * a + 3 * (1 - s) * s * s * b + s * s * s;

            protected override double EaseInCore(double t)
            {
                double lo = 0, hi = 1, s = t;
                for (var i = 0; i < 24; i++)
                {
                    s = (lo + hi) / 2;
                    if (Curve(s, x1, x2) < t) lo = s;
                    else hi = s;
                }
                return Curve(s, y1, y2);
            }

            protected override Freezable CreateInstanceCore() => new Bezier(x1, y1, x2, y2);
        }

        private Border IconButton(string glyph, string name, Action click)
        {
            var text = new TextBlock
            {
                Text = glyph, FontFamily = IconFont, FontSize = 16, Foreground = Brush(theme.Text),
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center,
            };
            var button = new Border
            {
                Width = 32, Height = 32, CornerRadius = new CornerRadius(4), Background = Brushes.Transparent,
                Cursor = Cursors.Hand, Child = text, ToolTip = name, Focusable = true,
            };
            button.KeyDown += (s, e) => { if (e.Key == Key.Enter || e.Key == Key.Space) { click(); e.Handled = true; } };
            button.GotKeyboardFocus += (s, e) => button.Background = Brush(theme.Hover);
            button.LostKeyboardFocus += (s, e) => button.Background = Brushes.Transparent;
            AutomationName(button, name);
            ToolTipService.SetInitialShowDelay(button, 600);
            button.MouseEnter += (s, e) => button.Background = Brush(theme.Hover);
            button.MouseLeave += (s, e) => button.Background = Brushes.Transparent;
            button.PreviewMouseLeftButtonDown += (s, e) => button.Background = Brush(theme.Pressed);
            OnClick(button, () => click());
            AutomationName(button, name);
            return button;
        }

        /// <summary>Windows 11's standard button: a light fill and a hairline that is darker at the bottom.</summary>
        private Border Button(string text, string tip, Action click)
        {
            var label = Text(text, 13, theme.Text);
            label.HorizontalAlignment = HorizontalAlignment.Center;
            var stroke = new LinearGradientBrush(theme.ControlStroke, theme.ControlStrokeBottom, 90);
            stroke.Freeze();
            var button = new Border
            {
                Child = label,
                Padding = new Thickness(12, 4, 12, 6), CornerRadius = new CornerRadius(4), MinWidth = 72,
                Background = Brush(theme.ControlFill), BorderBrush = stroke, BorderThickness = new Thickness(1),
                Cursor = Cursors.Hand, VerticalAlignment = VerticalAlignment.Center, ToolTip = tip,
                Focusable = true,
            };
            button.MouseEnter += (s, e) => button.Background = Brush(theme.ControlHover);
            button.MouseLeave += (s, e) =>
            {
                button.Background = Brush(theme.ControlFill);
                label.Foreground = Brush(theme.Text);
            };
            button.PreviewMouseLeftButtonDown += (s, e) =>
            {
                button.Background = Brush(theme.ControlPressed);
                label.Foreground = Brush(theme.TextSecondary);
            };
            button.PreviewMouseLeftButtonUp += (s, e) =>
            {
                button.Background = Brush(button.IsMouseOver ? theme.ControlHover : theme.ControlFill);
                label.Foreground = Brush(theme.Text);
            };
            OnClick(button, click);
            button.KeyDown += (s, e) =>
            {
                if (e.Key == Key.Enter || e.Key == Key.Space) { click(); e.Handled = true; }
            };
            button.GotKeyboardFocus += (s, e) => button.BorderBrush = Brush(theme.Accent);
            button.LostKeyboardFocus += (s, e) => button.BorderBrush = stroke;
            AutomationName(button, text);
            return button;
        }

        private TextBlock Text(string text, double size, Color color, FontWeight? weight = null, bool tabular = false)
        {
            var block = new TextBlock
            {
                Text = text, FontFamily = FontFor(size), FontSize = size, FontWeight = weight ?? FontWeights.Normal,
                Foreground = Brush(color), VerticalAlignment = VerticalAlignment.Center,
                TextTrimming = TextTrimming.CharacterEllipsis,
            };
            if (tabular)
            {
                Typography.SetNumeralAlignment(block, FontNumeralAlignment.Tabular);
            }
            return block;
        }

        private static Run Run(string text, double size, Color color, FontWeight? weight = null) => new Run(text)
        {
            FontFamily = FontFor(size), FontSize = size, Foreground = Brush(color), FontWeight = weight ?? FontWeights.Normal,
        };

        /// <summary>Segoe UI Variable's optical sizes, as Windows picks them: Display for
        /// large figures, Small below 12 px, Text for everything between.</summary>
        private static FontFamily FontFor(double size) => size >= 20 ? DisplayFont : size < 12 ? SmallFont : TextFont;

        /// <summary>
        /// A provider's mark as a glyph in the text colour. The bundled marks are dark
        /// logos on white tiles or coloured shapes; drawn as masks they sit on the glass
        /// like text, in either theme, instead of as white stickers.
        /// </summary>
        private FrameworkElement Icon(string path, string mark, double size)
        {
            var mask = LoadMask(path);
            if (mask != null)
            {
                var brush = new ImageBrush(mask) { Stretch = Stretch.Uniform };
                brush.Freeze();
                var glyph = new Rectangle { Width = size, Height = size, Fill = Brush(theme.Text), OpacityMask = brush, VerticalAlignment = VerticalAlignment.Center };
                RenderOptions.SetBitmapScalingMode(glyph, BitmapScalingMode.HighQuality);
                return glyph;
            }
            var tile = new Grid { Width = size, Height = size, VerticalAlignment = VerticalAlignment.Center };
            tile.Children.Add(new Border
            {
                CornerRadius = new CornerRadius(4), BorderThickness = new Thickness(1.2),
                BorderBrush = Brush(Theme.WithAlpha(theme.Text, 0.75)),
            });
            tile.Children.Add(new TextBlock
            {
                Text = mark, FontFamily = SmallFont, FontSize = size * 0.42, FontWeight = FontWeights.Bold,
                Foreground = Brush(theme.Text), HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            });
            return tile;
        }

        private ImageSource LoadMask(string path)
        {
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
            {
                return null;
            }
            if (masks.TryGetValue(path, out var cached))
            {
                return cached;
            }
            ImageSource result = null;
            try
            {
                var decoded = new BitmapImage();
                decoded.BeginInit();
                decoded.CacheOption = BitmapCacheOption.OnLoad;
                decoded.UriSource = new Uri(path);
                decoded.DecodePixelWidth = 96;
                decoded.EndInit();
                var source = new FormatConvertedBitmap(decoded, PixelFormats.Bgra32, null, 0);
                int w = source.PixelWidth, h = source.PixelHeight;
                var pixels = new byte[w * h * 4];
                source.CopyPixels(pixels, w * 4, 0);
                // A logo on an opaque light tile: the dark ink is the mark. A shape on
                // transparency: the shape is.
                int opaque = 0;
                double light = 0;
                for (var i = 0; i < w * h; i++)
                {
                    if (pixels[i * 4 + 3] > 200)
                    {
                        opaque++;
                        light += 0.114 * pixels[i * 4] + 0.587 * pixels[i * 4 + 1] + 0.299 * pixels[i * 4 + 2];
                    }
                }
                var ink = opaque > w * h * 0.7 && light / Math.Max(1, opaque) > 150;
                var alpha = new byte[w * h];
                int minX = w, minY = h, maxX = -1, maxY = -1;
                for (var y = 0; y < h; y++)
                {
                    for (var x = 0; x < w; x++)
                    {
                        var i = y * w + x;
                        double a = pixels[i * 4 + 3];
                        if (ink)
                        {
                            var lum = 0.114 * pixels[i * 4] + 0.587 * pixels[i * 4 + 1] + 0.299 * pixels[i * 4 + 2];
                            a = a / 255.0 * Math.Max(0, Math.Min(255, (235 - lum) * 255 / 190));
                        }
                        alpha[i] = (byte)a;
                        if (a > 40)
                        {
                            minX = Math.Min(minX, x);
                            maxX = Math.Max(maxX, x);
                            minY = Math.Min(minY, y);
                            maxY = Math.Max(maxY, y);
                        }
                    }
                }
                if (maxX >= minX && maxY >= minY)
                {
                    // Trim to the mark and centre it in a square, so every logo fills its
                    // slot the same way whatever padding its file carried.
                    var side = Math.Max(maxX - minX + 1, maxY - minY + 1);
                    var square = side + 2;
                    var output = new byte[square * square * 4];
                    var ox = (square - (maxX - minX + 1)) / 2;
                    var oy = (square - (maxY - minY + 1)) / 2;
                    for (var y = minY; y <= maxY; y++)
                    {
                        for (var x = minX; x <= maxX; x++)
                        {
                            var o = ((y - minY + oy) * square + (x - minX + ox)) * 4;
                            output[o] = output[o + 1] = output[o + 2] = 255;
                            output[o + 3] = alpha[y * w + x];
                        }
                    }
                    var bitmap = BitmapSource.Create(square, square, 96, 96, PixelFormats.Bgra32, null, output, square * 4);
                    bitmap.Freeze();
                    result = bitmap;
                }
            }
            catch (Exception)
            {
                result = null;
            }
            masks[path] = result;
            return result;
        }

        /// <summary>A click is a press and a release on the same element.</summary>
        private static void OnClick(UIElement element, Action action)
        {
            var pressed = false;
            element.MouseLeftButtonDown += (s, e) =>
            {
                pressed = true;
                element.CaptureMouse();
                e.Handled = true;
            };
            element.LostMouseCapture += (s, e) => pressed = false;
            element.MouseLeftButtonUp += (s, e) =>
            {
                var inside = element.IsMouseOver || element.InputHitTest(e.GetPosition(element)) != null;
                var was = pressed;
                pressed = false;
                element.ReleaseMouseCapture();
                e.Handled = true;
                if (was && inside)
                {
                    action();
                }
            };
        }

        private static void AutomationName(DependencyObject element, string name) =>
            System.Windows.Automation.AutomationProperties.SetName(element, name);

        private static SolidColorBrush Brush(Color color)
        {
            lock (brushes)
            {
                if (brushes.TryGetValue(color, out var existing)) return existing;
                var brush = new SolidColorBrush(color);
                brush.Freeze();
                if (brushes.Count < 128) brushes[color] = brush;
                return brush;
            }
        }
    }
}
