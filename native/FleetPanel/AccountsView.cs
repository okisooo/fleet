using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;

namespace FleetPanel
{
    // The usage page, most important first. One card holds every account as a
    // compact row: the limit that stops it first, how much of it is left, the
    // exact local time it resets and a live countdown, plus a line only when
    // something needs attention. A click opens the whole deal for that account.
    // The tray picks the limit and the alerts (panel_model._glance); this lays
    // them out. Spend and balances sit in a card of their own below the resets.
    internal sealed partial class PanelView
    {
        /// <summary>Wide enough for "Mon 12:26 PM" and "Dec 13 7:48 AM".</summary>
        private const double TimeColumn = 92;
        private const double RowPad = 8;

        private Border ListCard(UIElement child, string name)
        {
            var card = new Border
            {
                Child = child,
                Padding = new Thickness(4),
                Margin = new Thickness(0, 0, 0, 8),
                CornerRadius = new CornerRadius(11),
                Background = Brush(theme.ControlFill),
            };
            AutomationName(card, name);
            return card;
        }

        private Border ListDivider() => new Border
        {
            Height = 1, Margin = new Thickness(RowPad + Indent, 0, RowPad, 0), Background = Brush(theme.Divider), IsHitTestVisible = false,
        };

        /// <summary>Every account with limits, then the logins that need a hand.</summary>
        private FrameworkElement AccountsCard(List<Provider> accounts)
        {
            if (accounts.Count == 0 && data.Inactive.Count == 0)
            {
                return null;
            }
            var list = new StackPanel();
            foreach (var p in accounts)
            {
                if (list.Children.Count > 0) list.Children.Add(ListDivider());
                list.Children.Add(Section(p, AccountSummary(p)));
            }
            foreach (var item in data.Inactive)
            {
                if (list.Children.Count > 0) list.Children.Add(ListDivider());
                var row = InactiveRow(item);
                row.Margin = new Thickness(RowPad, 3, RowPad, 3);
                list.Children.Add(row);
            }
            return ListCard(list, "Accounts");
        }

        /// <summary>
        /// A closed account: its name and the limit that binds, the reset's exact
        /// local time; under them the meter and figure, and the countdown. Used up,
        /// the meter gives way to "Limit reached" in red. Then at most two lines that
        /// need attention: a stale reading, a model's limit used up, a run-out.
        /// </summary>
        private FrameworkElement AccountSummary(Provider p)
        {
            var glance = p.Glance;
            var row = glance.Row;
            var blocked = glance.Blocked;
            var grid = new Grid { Background = Brushes.Transparent };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(Indent) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(TimeColumn) });
            grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(22) });
            grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(20) });

            var icon = Icon(p.Icon, p.Mark, 16);
            icon.HorizontalAlignment = HorizontalAlignment.Left;
            grid.Children.Add(icon);

            var name = new TextBlock { VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
            name.Inlines.Add(Run(p.Name, 14, theme.Text, FontWeights.SemiBold));
            name.Inlines.Add(Run("  " + row.Short, 12, theme.TextSecondary));
            Grid.SetColumn(name, 1);
            grid.Children.Add(name);

            var reset = ResetVerb(row);
            var when = Text(Clock.When(row.ResetAt), 12, blocked ? theme.CriticalText : theme.Text, blocked ? FontWeights.SemiBold : (FontWeight?)null, tabular: true);
            when.HorizontalAlignment = HorizontalAlignment.Right;
            Grid.SetColumn(when, 2);
            grid.Children.Add(when);

            if (blocked)
            {
                var line = new TextBlock { VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
                line.Inlines.Add(Glyph("", theme.CriticalText));
                line.Inlines.Add(Run(" Limit reached", 12, theme.CriticalText));
                Grid.SetRow(line, 1);
                Grid.SetColumn(line, 1);
                grid.Children.Add(line);
            }
            else
            {
                var bar = new Grid { VerticalAlignment = VerticalAlignment.Center };
                bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(62) });
                var meter = new Meter(theme, Shown(row.Pct ?? 0), theme.ForState(row.State), Units == "left" ? null : row.Elapsed)
                {
                    Margin = new Thickness(0, 0, 8, 0), VerticalAlignment = VerticalAlignment.Center,
                };
                bar.Children.Add(meter);
                // "16% left": the unit says which way the bar runs.
                var figure = new TextBlock { VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Right };
                Typography.SetNumeralAlignment(figure, FontNumeralAlignment.Tabular);
                figure.Inlines.Add(Run(Figure(row), 12, theme.Text, FontWeights.SemiBold));
                figure.Inlines.Add(Run(" " + Units, 12, theme.TextSecondary));
                Grid.SetColumn(figure, 1);
                bar.Children.Add(figure);
                Grid.SetRow(bar, 1);
                Grid.SetColumn(bar, 1);
                grid.Children.Add(bar);
                if (p.FreshStale)
                {
                    bar.Opacity = 0.55;
                }
            }

            var left = Text("", 12, blocked ? theme.CriticalText : theme.TextSecondary, blocked ? FontWeights.SemiBold : (FontWeight?)null, tabular: true);
            left.HorizontalAlignment = HorizontalAlignment.Right;
            Grid.SetRow(left, 1);
            Grid.SetColumn(left, 2);
            grid.Children.Add(left);
            var at = row.ResetAt;
            tickers.Add(() => left.Text = Clock.Left(at));
            if (at != null)
            {
                var tip = Clock.Full(reset, at) + (blocked ? ". Until then this account can't be used." : ".");
                when.ToolTip = tip;
                left.ToolTip = tip;
                ToolTipService.SetInitialShowDelay(when, 400);
                ToolTipService.SetInitialShowDelay(left, 400);
            }

            foreach (var alert in AlertLines(p))
            {
                var r = grid.RowDefinitions.Count;
                grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(19) });
                Grid.SetRow(alert, r);
                Grid.SetColumn(alert, 1);
                Grid.SetColumnSpan(alert, 2);
                grid.Children.Add(alert);
            }
            AutomationName(grid, p.Name + ", " + row.Short + ", " + (blocked ? "limit reached" : Figure(row) + " " + Units)
                + (at != null ? ", resets " + Clock.When(at) : ""));
            return grid;
        }

        /// <summary>Where to redeem, short enough to share a line.</summary>
        private static string RedeemShort(string account) =>
            account.StartsWith("Codex", StringComparison.Ordinal) ? "/usage in Codex" : "in Claude Code";

        private Run Glyph(string glyph, Color color) => new Run(glyph)
        {
            FontFamily = IconFont, FontSize = 11, Foreground = Brush(color), BaselineAlignment = BaselineAlignment.Center,
        };

        /// <summary>
        /// The lines under an account, only when something needs attention: a reading
        /// that is old or borrowed, a model's limit used up or nearly so, a limit on
        /// course to run out before it resets.
        /// </summary>
        private IEnumerable<FrameworkElement> AlertLines(Provider p)
        {
            var lines = new List<(string Glyph, Color Tone, string Text, string Tip)>();
            if (p.FreshKind != "" && (p.FreshStale || p.FreshKind != "cached" || p.FreshReason != ""))
            {
                var why = p.FreshKind != "cached" ? "From the " + p.FreshKind + " app" : p.FreshReason != "" ? p.FreshReason : "Cached";
                lines.Add(("", theme.TextSecondary, why + " · " + Clock.Age(p.FreshAt ?? Clock.Now()) + " old", p.Detail));
            }
            if (TokenUsable(p))
            {
                // The one thing that ends the wait now: worth the first line.
                lines.Add(("", theme.Accent, "Reset token usable now · " + RedeemShort(p.Name),
                    "The provider would accept one of your reset tokens now. Redeem it in " + RedeemIn(p.Name) + "; Fleet only reads them."));
            }
            var binding = p.Glance.Row.Short;
            foreach (var alert in p.Glance.Alerts)
            {
                switch (alert.Kind)
                {
                    case "full":
                        lines.Add(("", theme.CriticalText, alert.Label + " limit reached · resets " + Clock.When(alert.At),
                            Clock.Full(alert.Label + " resets", alert.At)));
                        break;
                    case "high":
                        lines.Add(("", theme.CriticalText, alert.Label + " almost used up · resets " + Clock.When(alert.At),
                            Clock.Full(alert.Label + " resets", alert.At)));
                        break;
                    case "runout":
                        lines.Add(("", theme.CautionText,
                            (alert.Label == binding ? "Runs out around " : alert.Label + " runs out around ") + Clock.Day(alert.At) + " at this pace",
                            "At this pace " + LowerFirst(alert.Label) + " is used up " + Clock.In(alert.At) + ", "
                            + Clock.Full("on", alert.At) + ", before it resets."));
                        break;
                }
            }
            foreach (var line in lines.Take(2))
            {
                var text = new TextBlock { VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
                text.Inlines.Add(Glyph(line.Glyph, line.Tone));
                var accent = line.Tone == theme.Accent;
                text.Inlines.Add(Run(" " + line.Text, 12, accent ? theme.Accent : theme.TextSecondary, accent ? FontWeights.SemiBold : (FontWeight?)null));
                if (!string.IsNullOrEmpty(line.Tip))
                {
                    text.ToolTip = line.Tip;
                    ToolTipService.SetInitialShowDelay(text, 400);
                }
                yield return text;
            }
        }

        /// <summary>
        /// What the tokens came to at API prices, and the accounts that are money
        /// rather than limits (a credit balance, a month's API spend).
        /// </summary>
        private FrameworkElement MoneyCard(List<Provider> others)
        {
            var list = new StackPanel();
            var value = ValueRow();
            if (value != null)
            {
                list.Children.Add(value);
            }
            foreach (var p in others)
            {
                if (list.Children.Count > 0) list.Children.Add(ListDivider());
                list.Children.Add(Section(p, MoneySummary(p)));
            }
            return list.Children.Count == 0 ? null : ListCard(list, "Spend and balances");
        }

        /// <summary>"API value  $206 today", with 7 and 30 days beside it; a click opens the dashboard.</summary>
        private FrameworkElement ValueRow()
        {
            var summary = data.Summary;
            if (!data.Cost || summary == null || summary.State == "off")
            {
                return null;
            }
            var grid = new Grid { MinHeight = 24, Background = Brushes.Transparent };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(Indent) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.Children.Add(new TextBlock
            {
                // Segoe Fluent Icons "Money".
                Text = "", FontFamily = IconFont, FontSize = 15, Foreground = Brush(theme.Text),
                VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Left,
            });
            var name = Text("API value", 14, theme.Text, FontWeights.SemiBold);
            Grid.SetColumn(name, 1);
            grid.Children.Add(name);
            var figures = new TextBlock { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 0, 0) };
            Typography.SetNumeralAlignment(figures, FontNumeralAlignment.Tabular);
            if (summary.Today == null)
            {
                figures.Inlines.Add(Run("Reading local logs…", 12, theme.TextSecondary));
            }
            else
            {
                figures.Inlines.Add(Run(Format.Money(summary.Today.Value), 13, theme.Text, FontWeights.SemiBold));
                figures.Inlines.Add(Run(" today", 12, theme.TextSecondary));
                if (summary.Week != null)
                {
                    figures.Inlines.Add(Run("  ·  7 d ", 12, theme.TextTertiary));
                    figures.Inlines.Add(Run(Format.Money(summary.Week.Value, compact: true), 12, theme.TextSecondary));
                }
                if (summary.Month != null)
                {
                    figures.Inlines.Add(Run("  ·  30 d ", 12, theme.TextTertiary));
                    figures.Inlines.Add(Run(Format.Money(summary.Month.Value, compact: true), 12, theme.TextSecondary));
                }
            }
            Grid.SetColumn(figures, 2);
            grid.Children.Add(figures);
            var hit = new Border { Child = grid, Padding = new Thickness(RowPad, 7, RowPad, 7), CornerRadius = new CornerRadius(8), Background = Brushes.Transparent };
            hit.ToolTip = CostNote + "\nOpen the dashboard for spend by day, model and project.";
            ToolTipService.SetInitialShowDelay(hit, 400);
            Hoverable(hit, ShowDashboard);
            AutomationName(hit, "API value, open the dashboard");
            return hit;
        }

        /// <summary>A balance or spend account on one line: its mark, name and figure.</summary>
        private FrameworkElement MoneySummary(Provider p)
        {
            var grid = new Grid { MinHeight = 24, Background = Brushes.Transparent };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(Indent) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var icon = Icon(p.Icon, p.Mark, 16);
            icon.HorizontalAlignment = HorizontalAlignment.Left;
            grid.Children.Add(icon);
            var name = Text(p.Name, 14, theme.Text, FontWeights.SemiBold);
            Grid.SetColumn(name, 1);
            grid.Children.Add(name);
            var right = new TextBlock { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 0, 0) };
            Typography.SetNumeralAlignment(right, FontNumeralAlignment.Tabular);
            if (p.AccountMode == "connection")
            {
                right.Inlines.Add(Run("View balance", 12, theme.TextSecondary));
            }
            else if (p.Balance != null)
            {
                right.Inlines.Add(Run(p.Balance.Value, 13, theme.Text, FontWeights.SemiBold));
                right.Inlines.Add(Run(p.Balance.Label == "Credit balance" ? " credit" : " " + p.Balance.Label.ToLowerInvariant(), 12, theme.TextSecondary));
            }
            else
            {
                var first = p.Groups.SelectMany(g => g.Rows).FirstOrDefault(r => r.Value != "");
                if (first != null) right.Inlines.Add(Run(first.Value, 13, theme.Text, FontWeights.SemiBold));
            }
            Grid.SetColumn(right, 2);
            grid.Children.Add(right);
            if (p.FreshStale)
            {
                right.Opacity = 0.55;
            }
            return grid;
        }

        private void Hoverable(Border hit, Action click)
        {
            hit.Cursor = System.Windows.Input.Cursors.Hand;
            hit.MouseEnter += (s, e) => hit.Background = Brush(theme.Hover);
            hit.MouseLeave += (s, e) => hit.Background = Brushes.Transparent;
            OnClick(hit, click);
        }
    }
}
