using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;

namespace FleetPanel
{
    // "Next resets": every limit window, free-reset expiry and API-credit renewal
    // across accounts, soonest first, each with its exact local time and a live
    // countdown. The tray decides the lines (panel_model._upcoming); this lays
    // them out and ticks the clocks.
    internal sealed partial class PanelView
    {
        /// <summary>Lines the closed card shows: the very next reset, then used-up limits
        /// (what you are waiting for), then the next soonest. Three keep the flyout
        /// within a 1080p screen; the rest open in place, all in time order.</summary>
        private const int ResetsClosed = 3;
        private bool resetsOpen;

        private List<ResetLine> UpcomingResets()
        {
            var now = Clock.Now();
            return data.Resets.Where(r => r.At > now).ToList();
        }

        private FrameworkElement ResetsCard()
        {
            var lines = UpcomingResets();
            if (lines.Count == 0)
            {
                return null;
            }
            var keep = new HashSet<ResetLine>(lines.Take(1));
            foreach (var line in lines.Where(l => l.State == "full").Concat(lines))
            {
                if (keep.Count >= ResetsClosed) break;
                keep.Add(line);
            }
            var more = lines.Count - keep.Count;

            var head = new Grid { MinHeight = 24 };
            head.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(Indent) });
            head.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            head.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            head.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            head.Children.Add(new TextBlock
            {
                // Segoe Fluent Icons "Recent": a clock face.
                Text = "", FontFamily = IconFont, FontSize = 15, Foreground = Brush(theme.Text),
                VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Left,
            });
            var title = Text("Next resets", 14, theme.Text, FontWeights.SemiBold);
            Grid.SetColumn(title, 1);
            head.Children.Add(title);

            var list = new StackPanel { Margin = new Thickness(0, 4, 0, 0) };
            var folded = new List<Reveal>();
            foreach (var line in lines)
            {
                var row = ResetRow(line);
                if (keep.Contains(line))
                {
                    list.Children.Add(row);
                    continue;
                }
                var reveal = new Reveal { Progress = resetsOpen ? 1 : 0, Child = row };
                folded.Add(reveal);
                list.Children.Add(reveal);
            }
            var stack = new StackPanel();
            stack.Children.Add(head);
            stack.Children.Add(list);
            var card = new Border
            {
                Child = stack,
                Padding = new Thickness(10, 10, 10, 8),
                Margin = new Thickness(0, 0, 0, 7),
                CornerRadius = new CornerRadius(11),
                Background = Brush(theme.ControlFill),
            };
            AutomationName(card, "Next resets");
            if (more == 0)
            {
                return card;
            }

            var count = Text("", 12, theme.TextTertiary, tabular: true);
            Grid.SetColumn(count, 2);
            head.Children.Add(count);
            var turn = new RotateTransform(resetsOpen ? 180 : 0);
            var chevron = new TextBlock
            {
                Text = "", FontFamily = IconFont, FontSize = 10, Foreground = Brush(theme.TextTertiary),
                VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(10, 1, 0, 0),
                RenderTransformOrigin = new Point(0.5, 0.5), RenderTransform = turn,
            };
            Grid.SetColumn(chevron, 3);
            head.Children.Add(chevron);
            Action label = () =>
            {
                count.Text = resetsOpen ? "" : more + " more";
                AutomationName(card, resetsOpen ? "Next resets, all shown" : "Next resets, show " + more + " more");
            };
            label();
            card.Cursor = Cursors.Hand;
            card.MouseEnter += (s, e) => card.Background = Brush(theme.ControlHover);
            card.MouseLeave += (s, e) => card.Background = Brush(theme.ControlFill);
            OnClick(card, () =>
            {
                resetsOpen = !resetsOpen;
                label();
                var to = resetsOpen ? 1.0 : 0.0;
                if (!Motion)
                {
                    turn.BeginAnimation(RotateTransform.AngleProperty, null);
                    turn.Angle = resetsOpen ? 180 : 0;
                    foreach (var reveal in folded)
                    {
                        reveal.BeginAnimation(Reveal.ProgressProperty, null);
                        reveal.Progress = to;
                    }
                    return;
                }
                busyUntil = DateTime.UtcNow.AddMilliseconds(300);
                turn.BeginAnimation(RotateTransform.AngleProperty, Anim(turn.Angle, resetsOpen ? 180 : 0, 200, 0, Decelerate));
                foreach (var reveal in folded)
                {
                    reveal.BeginAnimation(Reveal.ProgressProperty,
                        Anim(reveal.Progress, to, resetsOpen ? 280 : 220, 0, resetsOpen ? Decelerate : Standard));
                    reveal.Child.BeginAnimation(UIElement.OpacityProperty, resetsOpen
                        ? Anim(0, 1, 170, 70, Decelerate) : Anim(reveal.Child.Opacity, 0, 110, 0, Standard));
                }
            });
            return card;
        }

        /// <summary>
        /// One reset: the account's mark and name, which limits reset (in red when one
        /// of them is used up), the exact local time, and a countdown that ticks.
        /// </summary>
        private FrameworkElement ResetRow(ResetLine line)
        {
            var full = line.State == "full";
            var grid = new Grid { Height = 22, Background = Brushes.Transparent };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(Indent) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(50) });

            var icon = Icon(line.Icon, line.Mark, 14);
            icon.HorizontalAlignment = HorizontalAlignment.Left;
            grid.Children.Add(icon);

            var labels = string.Join(", ", line.Labels);
            var text = new TextBlock { VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
            text.Inlines.Add(Run(line.Name, 12, theme.Text, FontWeights.SemiBold));
            text.Inlines.Add(Run("  ", 12, theme.TextSecondary));
            if (full)
            {
                text.Inlines.Add(new Run("")
                {
                    FontFamily = IconFont, FontSize = 11, Foreground = Brush(theme.CriticalText),
                    BaselineAlignment = BaselineAlignment.Center,
                });
                text.Inlines.Add(Run(" ", 12, theme.CriticalText));
            }
            text.Inlines.Add(Run(labels, 12, full ? theme.CriticalText : theme.TextSecondary));
            Grid.SetColumn(text, 1);
            grid.Children.Add(text);

            var when = Text(Clock.When(line.At), 12, full ? theme.CriticalText : theme.TextSecondary, tabular: true);
            when.HorizontalAlignment = HorizontalAlignment.Right;
            when.Margin = new Thickness(8, 0, 0, 0);
            Grid.SetColumn(when, 2);
            grid.Children.Add(when);

            var left = Text("", 12, full ? theme.CriticalText : theme.Text, FontWeights.SemiBold, tabular: true);
            left.HorizontalAlignment = HorizontalAlignment.Right;
            Grid.SetColumn(left, 3);
            grid.Children.Add(left);
            var at = line.At;
            tickers.Add(() => left.Text = Clock.Left(at));

            string tip;
            switch (line.Kind)
            {
                case "expiry":
                    tip = line.Name + " · " + line.Value + "\n" + Clock.Full("Unused resets expire", at);
                    break;
                case "credits":
                    tip = line.Name + " · API credits: " + line.Value + "\n"
                        + Clock.Full("Renew", at) + ". Unused credits don't roll over.";
                    break;
                default:
                    tip = line.Name + " · " + labels + (full ? " · limit reached" : "") + "\n" + Clock.Full("Resets", at);
                    break;
            }
            grid.ToolTip = tip;
            ToolTipService.SetInitialShowDelay(grid, 400);
            AutomationName(grid, line.Name + ", " + labels + (full ? ", limit reached" : "") + ", " + Clock.When(at));
            return grid;
        }
    }
}
