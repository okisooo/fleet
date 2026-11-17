using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;

namespace FleetPanel
{
    // "Resets": each account's waiting reset tokens on a pinned line, then every
    // limit window, reset-token expiry and API-credit renewal across accounts,
    // soonest first, each with its exact local time and a live countdown. The
    // tray decides the lines (panel_model._upcoming, _token_summary); this lays
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
            var tokens = data.Tokens.Count > 0;
            if (lines.Count == 0 && !tokens)
            {
                return null;
            }
            // The pinned reset-token line takes one of the closed lines, and stands
            // for the tokens' own expiry lines until the card is opened.
            var room = ResetsClosed - (tokens ? 1 : 0);
            var limits = tokens ? lines.Where(l => l.Kind != "expiry").ToList() : lines;
            var keep = new HashSet<ResetLine>(limits.Take(Math.Min(1, room)));
            foreach (var line in limits.Where(l => l.State == "full").Concat(limits))
            {
                if (keep.Count >= room) break;
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
            var title = Text("Resets", 14, theme.Text, FontWeights.SemiBold);
            Grid.SetColumn(title, 1);
            head.Children.Add(title);

            var list = new StackPanel { Margin = new Thickness(0, 4, 0, 0) };
            if (tokens)
            {
                list.Children.Add(TokensRow());
            }
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
            AutomationName(card, "Resets");
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
                AutomationName(card, resetsOpen ? "Resets, all shown" : "Resets, show " + more + " more");
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
                    tip = line.Name + " · " + Clock.Full(labels.Length > 0 ? labels : "Reset tokens expire", at)
                        + ".\nRedeem it in " + RedeemIn(line.Name) + " before then.";
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

        /// <summary>
        /// The pinned line: how many reset tokens each account has waiting, and when
        /// the next one expires, or that one could be redeemed right now.
        /// </summary>
        private FrameworkElement TokensRow()
        {
            var grid = new Grid { Height = 22, Background = Brushes.Transparent };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(Indent) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(50) });
            grid.Children.Add(new TextBlock
            {
                // Segoe Fluent Icons "UpdateRestore": a reset arrow.
                Text = "", FontFamily = IconFont, FontSize = 13, Foreground = Brush(theme.Text),
                VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Left,
                Margin = new Thickness(1, 1, 0, 0),
            });

            var text = new TextBlock { VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
            text.Inlines.Add(Run("Reset tokens", 12, theme.Text, FontWeights.SemiBold));
            var first = true;
            foreach (var account in data.Tokens)
            {
                text.Inlines.Add(Run(first ? "  " : " · ", 12, theme.TextTertiary));
                text.Inlines.Add(Run(account.Name + " ", 12, theme.TextSecondary));
                text.Inlines.Add(Run(account.Count.ToString(CultureInfo.InvariantCulture), 12, theme.Text, FontWeights.SemiBold));
                first = false;
            }
            Grid.SetColumn(text, 1);
            grid.Children.Add(text);

            var usable = data.Tokens.Sum(t => t.Usable);
            var next = data.Tokens.Select(t => t.Next).Min();
            var when = Text(usable > 0 ? "usable now" : Clock.When(next), 12,
                usable > 0 ? theme.Accent : theme.TextSecondary, usable > 0 ? FontWeights.SemiBold : (FontWeight?)null, tabular: true);
            when.HorizontalAlignment = HorizontalAlignment.Right;
            when.Margin = new Thickness(8, 0, 0, 0);
            Grid.SetColumn(when, 2);
            grid.Children.Add(when);
            var left = Text("", 12, theme.Text, FontWeights.SemiBold, tabular: true);
            left.HorizontalAlignment = HorizontalAlignment.Right;
            Grid.SetColumn(left, 3);
            grid.Children.Add(left);
            if (usable == 0 && next != null)
            {
                tickers.Add(() => left.Text = Clock.Left(next));
            }

            var tip = new List<string>();
            foreach (var account in data.Tokens)
            {
                tip.Add(account.Name + ": " + account.Count + (account.Count == 1 ? " reset token" : " reset tokens")
                    + (account.Usable == 0 ? "" : account.Usable >= account.Count ? ", usable now" : ", " + account.Usable + " usable now"));
                foreach (var token in account.Tokens)
                {
                    tip.Add("    " + Clock.Full(token.Count == 1 ? "1 expires" : token.Count + " expire", token.Expires));
                }
                tip.Add("    Redeem in " + RedeemIn(account.Name) + ".");
            }
            grid.ToolTip = string.Join("\n", tip);
            ToolTipService.SetInitialShowDelay(grid, 400);
            AutomationName(grid, "Reset tokens, " + string.Join(", ", data.Tokens.Select(t => t.Name + " " + t.Count))
                + (usable > 0 ? ", usable now" : next != null ? ", next expires " + Clock.When(next) : ""));
            return grid;
        }

        /// <summary>Where an account's reset tokens are redeemed; Fleet only reads them.</summary>
        private static string RedeemIn(string account) =>
            account.StartsWith("Codex", StringComparison.Ordinal) ? "Codex with /usage" : "Claude Code";
    }
}
