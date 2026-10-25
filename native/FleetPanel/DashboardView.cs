using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace FleetPanel
{
    // The detailed page inside the same fixed-width tray flyout.
    internal sealed partial class PanelView
    {
        private FrameworkElement DashboardPage()
        {
            var body = new StackPanel { Margin = new Thickness(0, 4, 0, 4) };
            var status = Text("", 12, theme.TextSecondary);
            status.Margin = new Thickness(0, 0, 0, 12);
            tickers.Add(() => status.Text = data.Live + " live · " + data.Cached + " cached · Updated " + Clock.Ago(data.Ts));
            body.Children.Add(status);
            var stale = DashNote("Readings are older than two minutes. Fleet may be offline or still collecting. Try Refresh.");
            tickers.Add(() => stale.Visibility = data.Ts > 0 && Clock.Now() - data.Ts > 120 ? Visibility.Visible : Visibility.Collapsed);
            body.Children.Add(stale);

            var history = data.History.Where(p => p.Spend != null).ToList();
            if (!data.Cost || data.Summary?.State == "off")
                body.Children.Add(DashNote("Local usage estimates are turned off. Enable API-price estimates in Settings to see activity and token history."));
            else if (data.Summary?.State == "indexing")
                body.Children.Add(DashNote("Reading local agent logs… Provider limits remain available while history is indexed."));
            else if (history.Count == 0)
                body.Children.Add(DashNote("No local token history yet. Activity and API-price estimates appear after Claude Code or Codex writes usage to its local logs."));
            else
            {
                body.Children.Add(DashTitle("API value from local logs"));
                var band = new Grid { Margin = new Thickness(0, 6, 0, 12) };
                band.ColumnDefinitions.Add(new ColumnDefinition());
                band.ColumnDefinitions.Add(new ColumnDefinition());
                var totals = new[] { history.Sum(p => p.Spend.Today?.Cost ?? 0), history.Sum(p => p.Spend.Week?.Cost ?? 0), history.Sum(p => p.Spend.Month?.Cost ?? 0), history.Sum(p => p.Spend.Today?.Tokens ?? 0) };
                var labels = new[] { "Today", "Last 7 days", "Last 30 days", "Tokens today" };
                for (var i = 0; i < labels.Length; i++)
                {
                    if (i % 2 == 0) band.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                    var cell = new StackPanel { Margin = new Thickness(0, 0, 12, 10) };
                    cell.Children.Add(Text(labels[i], 12, theme.TextSecondary));
                    cell.Children.Add(Text(i == 3 ? Format.Tokens(totals[i]) : Format.Money(totals[i]), 22, theme.Text, FontWeights.SemiBold, tabular: true));
                    Grid.SetColumn(cell, i % 2);
                    Grid.SetRow(cell, i / 2);
                    band.Children.Add(cell);
                }
                body.Children.Add(band);
                var basis = Text("Claude Code and Codex tokens at API list prices. Subscriptions are not billed per token.", 12, theme.TextSecondary);
                basis.TextWrapping = TextWrapping.Wrap;
                basis.Margin = new Thickness(0, 0, 0, 20);
                body.Children.Add(basis);
                var charts = new List<FrameworkElement>();
                var days = history.SelectMany(p => p.Spend.Daily).GroupBy(d => d.Label).OrderBy(g => g.Key)
                    .Select(g => new Day { Label = g.Key, Cost = g.Sum(d => d.Cost), Tokens = g.Sum(d => d.Tokens) }).ToList();
                if (days.Count > 0)
                    charts.Add(DashChart("Daily activity · last 14 days", days, days[0].Label, days[days.Count - 1].Label));
                var hours = Enumerable.Range(0, 24).Select(i => new Day { Label = (24 - i) + " h ago", Cost = history.Sum(p => p.Spend.Hourly.ElementAtOrDefault(i)) }).ToList();
                charts.Add(DashChart("Hourly activity · last 24 hours", hours, "24 h ago", "now"));
                body.Children.Add(DashStack(charts));
            }

            body.Children.Add(DashTitle("Providers and limits"));
            if (data.Providers.Count == 0 && data.Inactive.Count == 0)
                body.Children.Add(DashNote("No providers connected yet. Sign in to a supported client or connect a Claude API key in Settings."));
            body.Children.Add(DashStack(data.Providers.Select(DashProvider).ToList()));
            foreach (var item in data.Inactive)
            {
                var row = new DockPanel { Margin = new Thickness(0, 8, 0, 8) };
                if (item.Action != "")
                {
                    var action = Button(item.Action == "api-settings" ? "Settings" : item.Action == "claude-sign-in" ? "Sign in" : "Reconnect",
                        item.Tooltip, () => ActionClicked?.Invoke(item.Action));
                    DockPanel.SetDock(action, Dock.Right); row.Children.Add(action);
                }
                var note = Text(item.Name + " · " + item.Label, 13, theme.TextSecondary);
                note.TextWrapping = TextWrapping.Wrap; note.ToolTip = item.Tooltip;
                row.Children.Add(note); body.Children.Add(row);
            }

            if (history.Count > 0)
            {
                body.Children.Add(DashTitle("Local usage detail"));
                foreach (var provider in history)
                {
                    var spend = provider.Spend;
                    var detail = new StackPanel();
                    var table = DashTable(new[] { "Period", "API value", "Tokens" }, 1.3, 1.2, 1);
                    foreach (var pair in new[] { ("Today", spend.Today), ("7 days", spend.Week), ("30 days", spend.Month) })
                        if (pair.Item2 != null)
                        {
                            var t = pair.Item2;
                            DashRow(table, pair.Item1, Format.Money(t.Cost), Format.Tokens(t.Tokens));
                        }
                    detail.Children.Add(table);
                    var tokens = DashTable(new[] { "Token type", "Today", "7 d", "30 d" }, 1.5, 1, 1, 1);
                    Func<Func<Totals, double>, string[]> values = select => new[] { spend.Today, spend.Week, spend.Month }
                        .Select(t => t == null ? "—" : Format.Tokens(select(t))).ToArray();
                    foreach (var pair in new[] { ("Input", (Func<Totals, double>)(t => t.Input)), ("Output", (Func<Totals, double>)(t => t.Output)),
                        ("Cache read", (Func<Totals, double>)(t => t.CacheRead)), ("Cache write", (Func<Totals, double>)(t => t.CacheWrite)) })
                        DashRow(tokens, new[] { pair.Item1 }.Concat(values(pair.Item2)).ToArray());
                    detail.Children.Add(tokens);
                    if (spend.CostPerHour != null)
                        detail.Children.Add(Text("Recent pace: " + Format.Money(spend.CostPerHour.Value) + "/h · " + Format.Tokens(spend.TokensPerMin ?? 0) + " tokens/min", 12, theme.TextSecondary));
                    body.Children.Add(DashCard(provider.Name + " · token breakdown", detail));
                    body.Children.Add(DashStack(new List<FrameworkElement> {
                        DashShares(provider.Name + " · top models, last 7 days", spend.Models, true),
                        DashShares(provider.Name + " · top projects, last 7 days", spend.Projects, false)
                    }));
                }
                if (history.Any(p => p.Spend.Estimated))
                    body.Children.Add(DashNote("Some models use an estimated family price. These figures can change when published prices change."));
                var unpriced = history.Sum(p => p.Spend.Unpriced);
                if (unpriced > 0)
                    body.Children.Add(DashNote(unpriced.ToString("N0", CultureInfo.InvariantCulture) + " messages have no known model price and are excluded from the API-value estimate."));
            }
            if (data.Cost && data.LogFiles != null)
            {
                var coverage = "Local log coverage: " + data.LogFiles.Value.ToString("N0", CultureInfo.InvariantCulture) + " files";
                if (data.PendingFiles > 0) coverage += " · " + data.PendingFiles + " pending";
                if (data.SkippedArchives > 0) coverage += " · " + data.SkippedArchives + " compressed Codex archives not indexed; totals are incomplete";
                var note = Text(coverage, 12, data.SkippedArchives > 0 ? theme.CautionText : theme.TextTertiary);
                note.TextWrapping = TextWrapping.Wrap; note.Margin = new Thickness(0, 16, 0, 0);
                body.Children.Add(note);
            }
            Tick();
            return body;
        }

        private FrameworkElement DashProvider(Provider p)
        {
            var body = new StackPanel();
            var status = Text("", 12, p.FreshStale ? theme.CautionText : theme.TextSecondary);
            status.TextWrapping = TextWrapping.Wrap;
            tickers.Add(() => status.Text = (p.Plan != "" ? p.Plan + " · " : "") +
                (p.FreshKind != "" ? "Cached " + Clock.Ago(p.FreshAt ?? data.Ts) + (p.FreshReason != "" ? " · " + p.FreshReason : "") : "Live reading"));
            body.Children.Add(status);
            var rows = (p.AllGroups.Count > 0 ? p.AllGroups : p.Groups).SelectMany(g => g.Rows.Select(r => new { Group = g.Title, Row = r })).ToList();
            if (rows.Count > 0)
            {
                var table = DashTable(new[] { "Limit", data.Units == "left" ? "Left" : "Used", "Resets" }, 2.2, 1, 1.4);
                foreach (var item in rows)
                {
                    var row = item.Row;
                    var label = (item.Group != "" ? item.Group + " · " : "") + row.Label;
                    var value = row.Pct != null ? Math.Round(Shown(row.Pct.Value)).ToString(CultureInfo.InvariantCulture) + "%" : row.Value;
                    var index = DashRow(table, label, value, row.ResetAt == null ? "—" : Clock.Day(row.ResetAt));
                    var reset = table.Children.OfType<TextBlock>().Last();
                    if (row.ResetAt != null) tickers.Add(() => reset.Text = Clock.Day(row.ResetAt));
                    if (row.Pct != null)
                    {
                        var used = row.Pct.Value;
                        var valueCell = table.Children.OfType<TextBlock>().First(c => Grid.GetRow(c) == index && Grid.GetColumn(c) == 1);
                        valueCell.Foreground = Brush(used >= 95 ? theme.CriticalText : used >= 80 ? theme.CautionText : theme.Text);
                        var pace = Pace(row, out var tone);
                        valueCell.ToolTip = pace;
                        foreach (var cell in table.Children.OfType<TextBlock>().Where(c => Grid.GetRow(c) == index))
                            cell.Margin = new Thickness(0, 5, 12, 11);
                        var gauge = new DashboardGauge(theme, Shown(used), used) { Height = 3, VerticalAlignment = VerticalAlignment.Bottom,
                            Margin = new Thickness(0, 0, 0, 3), ToolTip = Math.Round(used) + "% used · " + pace };
                        Grid.SetRow(gauge, index); Grid.SetColumnSpan(gauge, 3); table.Children.Add(gauge);
                    }
                }
                body.Children.Add(table);
            }
            else if (p.Balance != null) body.Children.Add(Text(p.Balance.Value + " · " + p.Balance.Label, 20, theme.Text, FontWeights.SemiBold, tabular: true));
            if (p.AccountMode == "connection")
            {
                var note = Text("Key connected. Promotional credit balance and expiry are shown in Claude Console.", 12, theme.TextSecondary);
                note.TextWrapping = TextWrapping.Wrap; body.Children.Add(note);
            }
            if (p.AccountMode == "spend")
            {
                var note = Text("Organization spending · UTC days. Reports can lag and exclude Priority Tier. This is separate from local API-value estimates.", 12, theme.TextSecondary);
                note.TextWrapping = TextWrapping.Wrap; body.Children.Add(note);
            }
            if (p.AccountMode != "")
            {
                var billing = Button("Open billing", "View promotional credits and billing in Claude Console", () => ActionClicked?.Invoke("anthropic-billing"));
                billing.HorizontalAlignment = HorizontalAlignment.Left; billing.Margin = new Thickness(0, 8, 0, 0); body.Children.Add(billing);
            }
            if (p.Detail != "" && p.FreshKind != "") body.ToolTip = p.Detail;
            return DashCard(p.Name, body, p);
        }

        private FrameworkElement DashShares(string title, IList<Share> shares, bool models)
        {
            var table = DashTable(models ? new[] { "Model", "API value", "Tokens" } : new[] { "Project", "API value" }, models ? new[] { 2.2, 1.0, 1.0 } : new[] { 2.2, 1.0 });
            foreach (var share in shares)
                if (models) DashRow(table, share.Name, Format.Money(share.Cost), Format.Tokens(share.Tokens));
                else DashRow(table, share.Name, Format.Money(share.Cost));
            return DashCard(title, shares.Count > 0 ? (FrameworkElement)table : Text("No recorded usage in this period.", 12, theme.TextSecondary));
        }

        private FrameworkElement DashChart(string title, IList<Day> days, string first, string last)
        {
            var body = new StackPanel();
            body.Children.Add(Text("API value · peak " + Format.Money(days.Max(d => d.Cost)), 12, theme.TextSecondary));
            if (days.All(d => d.Cost <= 0))
            {
                var empty = Text("No priced activity in this period.", 13, theme.TextSecondary);
                empty.Margin = new Thickness(0, 28, 0, 28); body.Children.Add(empty);
            }
            else body.Children.Add(new DashboardChart(theme, days) { Height = 90, Margin = new Thickness(0, 10, 0, 6) });
            var axis = new DockPanel();
            var end = Text(last, 11, theme.TextTertiary); DockPanel.SetDock(end, Dock.Right); axis.Children.Add(end);
            axis.Children.Add(Text(first, 11, theme.TextTertiary)); body.Children.Add(axis);
            return DashCard(title, body);
        }

        private StackPanel DashStack(IList<FrameworkElement> items)
        {
            var stack = new StackPanel();
            foreach (var item in items) stack.Children.Add(item);
            return stack;
        }

        private Border DashCard(string title, FrameworkElement child, Provider provider = null)
        {
            var body = new StackPanel();
            var heading = new DockPanel { Margin = new Thickness(0, 0, 0, 10) };
            if (provider != null)
            {
                var icon = Icon(provider.Icon, provider.Mark, 18); icon.Margin = new Thickness(0, 0, 8, 0); DockPanel.SetDock(icon, Dock.Left); heading.Children.Add(icon);
            }
            var label = Text(title, 14, theme.Text, FontWeights.SemiBold); label.TextWrapping = TextWrapping.Wrap; heading.Children.Add(label);
            body.Children.Add(heading); body.Children.Add(child);
            return new Border { Child = body, Padding = new Thickness(0, 4, 0, 12), Margin = new Thickness(0, 0, 0, 12),
                BorderBrush = Brush(theme.Divider), BorderThickness = new Thickness(0, 0, 0, 1) };
        }

        private FrameworkElement DashTitle(string title)
        {
            var label = Text(title, 14, theme.Text, FontWeights.SemiBold); label.Margin = new Thickness(0, 8, 0, 10); return label;
        }

        private FrameworkElement DashNote(string text)
        {
            var label = Text(text, 12, theme.TextSecondary); label.TextWrapping = TextWrapping.Wrap;
            return new Border { Child = label, Padding = new Thickness(14), Background = Brush(theme.Hover), CornerRadius = new CornerRadius(4), Margin = new Thickness(0, 0, 0, 16) };
        }

        private Grid DashTable(string[] headers, params double[] widths)
        {
            var table = new Grid { Margin = new Thickness(0, 8, 0, 8) };
            foreach (var width in widths) table.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(width, GridUnitType.Star) });
            DashRow(table, headers);
            foreach (var label in table.Children.OfType<TextBlock>()) { label.Foreground = Brush(theme.TextTertiary); label.FontSize = 11; }
            return table;
        }

        private int DashRow(Grid table, params string[] cells)
        {
            var row = table.RowDefinitions.Count; table.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            for (var i = 0; i < cells.Length; i++)
            {
                var label = Text(cells[i], 12, i == 0 ? theme.TextSecondary : theme.Text, tabular: i > 0);
                label.TextWrapping = TextWrapping.Wrap; label.Margin = new Thickness(0, 5, i == cells.Length - 1 ? 0 : 12, 5);
                label.TextAlignment = i == 0 ? TextAlignment.Left : TextAlignment.Right;
                Grid.SetRow(label, row); Grid.SetColumn(label, i); table.Children.Add(label);
            }
            return row;
        }

        private sealed class DashboardGauge : FrameworkElement
        {
            private readonly Theme theme;
            private readonly double shown, used;
            public DashboardGauge(Theme theme, double shown, double used) { this.theme = theme; this.shown = shown; this.used = used; }
            protected override void OnRender(DrawingContext dc)
            {
                dc.DrawRoundedRectangle(Brush(theme.Track), null, new Rect(0, 0, ActualWidth, 3), 1.5, 1.5);
                var width = ActualWidth * Math.Max(0, Math.Min(100, shown)) / 100;
                if (width > 0) dc.DrawRoundedRectangle(Brush(used >= 95 ? theme.Critical : used >= 80 ? theme.Caution : theme.Accent), null, new Rect(0, 0, width, 3), 1.5, 1.5);
            }
        }

        private sealed class DashboardChart : FrameworkElement
        {
            private readonly Theme theme;
            private readonly IList<Day> days;
            public DashboardChart(Theme theme, IList<Day> days)
            {
                this.theme = theme; this.days = days;
                AutomationName(this, "API-price activity chart; " + days.Count + " buckets. Hover a bar for its value.");
                MouseMove += (s, e) => { var i = Math.Max(0, Math.Min(days.Count - 1, (int)(e.GetPosition(this).X / Math.Max(1, ActualWidth) * days.Count))); ToolTip = days[i].Label + " · " + Format.Money(days[i].Cost); };
            }
            protected override void OnRender(DrawingContext dc)
            {
                base.OnRender(dc);
                var max = days.Max(d => d.Cost);
                if (max <= 0 || ActualWidth <= 0) return;
                dc.DrawRectangle(Brushes.Transparent, null, new Rect(RenderSize));
                for (var i = 1; i <= 3; i++) dc.DrawLine(new Pen(Brush(theme.Divider), 1), new Point(0, ActualHeight * i / 3), new Point(ActualWidth, ActualHeight * i / 3));
                var step = ActualWidth / days.Count;
                for (var i = 0; i < days.Count; i++)
                {
                    if (days[i].Cost <= 0) continue;
                    var h = Math.Max(2, (ActualHeight - 3) * days[i].Cost / max);
                    dc.DrawRoundedRectangle(Brush(i == days.Count - 1 ? theme.Accent : Theme.WithAlpha(theme.Accent, 0.6)), null,
                        new Rect(i * step + 2, ActualHeight - h, Math.Max(1, step - 4), h), 2, 2);
                }
            }
        }
    }
}
