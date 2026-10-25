using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Web.Script.Serialization;

namespace FleetPanel
{
    /// <summary>panel.json, written by the tray (panel_model.py). The panel only lays it out.</summary>
    internal sealed class PanelData
    {
        public double Ts;
        public int Live;
        public int Cached;
        public List<Provider> Providers = new List<Provider>();
        public List<Inactive> Inactive = new List<Inactive>();
        public List<Known> Known = new List<Known>();
        public Dictionary<string, string> ApiAccounts = new Dictionary<string, string>();
        public HashSet<string> DisabledApiAccounts = new HashSet<string>();
        public string Units = "used", TrayIcon = "bars";
        public bool Alerts = true, Cost = true;
        public Summary Summary;

        public static PanelData Load(string path)
        {
            var text = File.ReadAllText(path);
            var root = new JavaScriptSerializer { MaxJsonLength = 8 * 1024 * 1024 }.DeserializeObject(text)
                as Dictionary<string, object>;
            var data = new PanelData();
            if (root == null)
            {
                return data;
            }
            data.Ts = Num(root, "ts") ?? 0;
            data.Live = (int)(Num(root, "live") ?? 0);
            data.Cached = (int)(Num(root, "cached") ?? 0);
            if (root.TryGetValue("settings", out var s) && s is Dictionary<string, object> settings)
            {
                data.Units = Str(settings, "units") == "left" ? "left" : "used";
                var icon = Str(settings, "tray_icon");
                data.TrayIcon = icon != "" ? icon : "bars";
                data.Alerts = !(settings.TryGetValue("alerts", out var a) && a is bool on && !on);
                data.Cost = !(settings.TryGetValue("cost", out var k) && k is bool shown && !shown);
            }
            if (root.TryGetValue("summary", out var sm) && sm is Dictionary<string, object> summary)
            {
                data.Summary = new Summary
                {
                    Today = Num(summary, "today"), Week = Num(summary, "week"), Month = Num(summary, "month"),
                    TokensToday = Num(summary, "tokens_today"), State = Str(summary, "state"),
                    Estimated = summary.TryGetValue("estimated", out var es) && es is bool est && est,
                };
            }
            foreach (var item in List(root, "providers"))
            {
                var p = new Provider
                {
                    Id = Str(item, "id"), Name = Str(item, "name"), Icon = Str(item, "icon"), Mark = Str(item, "mark"),
                    Plan = Str(item, "plan"), Detail = Str(item, "detail"), Kind = Str(item, "kind"),
                    Headline = Num(item, "headline"),
                };
                if (item.TryGetValue("account", out var ac) && ac is Dictionary<string, object> account)
                    p.AccountMode = Str(account, "mode");
                if (item.TryGetValue("freshness", out var f) && f is Dictionary<string, object> fresh)
                {
                    p.FreshKind = Str(fresh, "kind");
                    p.FreshReason = Str(fresh, "reason");
                    p.FreshAt = Num(fresh, "at");
                    p.FreshStale = fresh.TryGetValue("stale", out var st) && st is bool stale && stale;
                }
                if (item.TryGetValue("caption", out var c) && c is Dictionary<string, object> caption)
                {
                    p.Caption = new Caption
                    {
                        Kind = Str(caption, "kind"), Label = Str(caption, "label"), Text = Str(caption, "text"),
                        Tone = Str(caption, "tone"), At = Num(caption, "at"),
                    };
                }
                if (item.TryGetValue("balance", out var b) && b is Dictionary<string, object> balance)
                {
                    p.Balance = new Balance { Value = Str(balance, "value"), Label = Str(balance, "label") };
                }
                foreach (var r in List(item, "rings"))
                {
                    p.Rings.Add(new Ring
                    {
                        Label = Str(r, "label"), Pct = Num(r, "pct") ?? 0, ResetAt = Num(r, "reset_at"),
                        Window = Num(r, "window"), Elapsed = Num(r, "elapsed"), State = Str(r, "state"),
                    });
                }
                foreach (var g in List(item, "groups"))
                {
                    var group = new Group { Title = Str(g, "title") };
                    foreach (var r in List(g, "rows"))
                    {
                        group.Rows.Add(ParseRow(r));
                    }
                    p.Groups.Add(group);
                }
                foreach (var r in List(item, "meters"))
                {
                    p.Meters.Add(ParseRow(r));
                }
                if (item.TryGetValue("spend", out var sp) && sp is Dictionary<string, object> spend)
                {
                    p.Spend = ParseSpend(spend);
                }
                data.Providers.Add(p);
            }
            foreach (var item in List(root, "inactive"))
            {
                data.Inactive.Add(new Inactive
                {
                    Name = Str(item, "name"), Icon = Str(item, "icon"), Mark = Str(item, "mark"),
                    Label = Str(item, "label"), Tooltip = Str(item, "tooltip"), Action = Str(item, "action"),
                });
            }
            foreach (var item in List(root, "known"))
            {
                data.Known.Add(new Known
                {
                    Id = Str(item, "id"), Name = Str(item, "name"),
                    Hidden = item.TryGetValue("hidden", out var h) && h is bool hidden && hidden,
                });
            }
            foreach (var item in List(root, "api_accounts"))
            {
                data.ApiAccounts[Str(item, "id")] = Str(item, "source");
                if (item.TryGetValue("enabled", out var en) && en is bool enabled && !enabled)
                    data.DisabledApiAccounts.Add(Str(item, "id"));
            }
            return data;
        }

        private static Row ParseRow(Dictionary<string, object> r) => new Row
        {
            Label = Str(r, "label"), Short = Str(r, "short"), Pct = Num(r, "pct"), Value = Str(r, "value"),
            ResetAt = Num(r, "reset_at"), Severity = Str(r, "severity"),
            Window = Num(r, "window"), Elapsed = Num(r, "elapsed"), Reserve = Num(r, "reserve"),
            RunoutAt = Num(r, "runout_at"), State = Str(r, "state"), Rate = Num(r, "rate"),
        };

        private static Spend ParseSpend(Dictionary<string, object> map)
        {
            var spend = new Spend
            {
                Today = Totals(map, "today"), Window = Totals(map, "window"), Week = Totals(map, "week"),
                Month = Totals(map, "month"), WindowLabel = Str(map, "window_label"),
                Estimated = map.TryGetValue("estimated", out var e) && e is bool estimated && estimated,
            };
            if (map.TryGetValue("rate", out var r) && r is Dictionary<string, object> rate)
            {
                spend.CostPerHour = Num(rate, "cost_per_hour");
                spend.TokensPerMin = Num(rate, "tokens_per_min");
            }
            if (map.TryGetValue("hourly", out var h) && h is IEnumerable hours && !(h is string))
            {
                foreach (var v in hours)
                {
                    try { spend.Hourly.Add(Convert.ToDouble(v ?? 0, CultureInfo.InvariantCulture)); }
                    catch (Exception) { spend.Hourly.Add(0); }
                }
            }
            foreach (var m in List(map, "models"))
            {
                spend.Models.Add(new Share { Name = Str(m, "name"), Cost = Num(m, "cost") ?? 0, Part = Num(m, "share") ?? 0 });
            }
            foreach (var m in List(map, "projects"))
            {
                spend.Projects.Add(new Share { Name = Str(m, "name"), Cost = Num(m, "cost") ?? 0, Part = Num(m, "share") ?? 0 });
            }
            return spend;
        }

        private static Totals Totals(Dictionary<string, object> map, string key)
        {
            if (!map.TryGetValue(key, out var value) || !(value is Dictionary<string, object> t))
            {
                return null;
            }
            return new Totals
            {
                Cost = Num(t, "cost") ?? 0, Tokens = Num(t, "tokens") ?? 0, Input = Num(t, "input") ?? 0,
                Output = Num(t, "output") ?? 0, CacheRead = Num(t, "cache_read") ?? 0,
                CacheWrite = Num(t, "cache_write") ?? 0, Messages = Num(t, "messages") ?? 0,
            };
        }

        private static IEnumerable<Dictionary<string, object>> List(Dictionary<string, object> map, string key)
        {
            if (map.TryGetValue(key, out var value) && value is IEnumerable items && !(value is string))
            {
                foreach (var item in items)
                {
                    if (item is Dictionary<string, object> entry)
                    {
                        yield return entry;
                    }
                }
            }
        }

        private static string Str(Dictionary<string, object> map, string key) =>
            map.TryGetValue(key, out var value) && value != null ? Convert.ToString(value, CultureInfo.InvariantCulture) : "";

        private static double? Num(Dictionary<string, object> map, string key)
        {
            if (!map.TryGetValue(key, out var value) || value == null)
            {
                return null;
            }
            try
            {
                return Convert.ToDouble(value, CultureInfo.InvariantCulture);
            }
            catch (FormatException)
            {
                return null;
            }
            catch (InvalidCastException)
            {
                return null;
            }
        }
    }

    internal sealed class Provider
    {
        public string Id = "", Name = "", Icon = "", Mark = "", Plan = "", Detail = "", FreshKind = "", FreshReason = "", Kind = "";
        public double? FreshAt, Headline;
        public bool FreshStale;
        public string AccountMode = "";
        public Caption Caption;
        public Balance Balance;
        public List<Ring> Rings = new List<Ring>();
        public List<Group> Groups = new List<Group>();
        /// <summary>The limits shown while the section is closed; every row is also in Groups.</summary>
        public List<Row> Meters = new List<Row>();
        public Spend Spend;
    }

    /// <summary>What the tokens in local agent logs would cost at API list prices.</summary>
    internal sealed class Spend
    {
        public Totals Today, Window, Week, Month;
        public string WindowLabel = "";
        public double? CostPerHour, TokensPerMin;
        public bool Estimated;
        public List<double> Hourly = new List<double>();
        public List<Share> Models = new List<Share>();
        public List<Share> Projects = new List<Share>();
    }

    internal sealed class Totals
    {
        public double Cost, Tokens, Input, Output, CacheRead, CacheWrite, Messages;
    }

    internal sealed class Share
    {
        public string Name = "";
        public double Cost, Part;
    }

    internal sealed class Summary
    {
        public double? Today, Week, Month, TokensToday;
        public bool Estimated;
        public string State = "";
    }

    internal sealed class Ring
    {
        public string Label = "", State = "";
        public double Pct;
        public double? ResetAt, Window, Elapsed;
    }

    internal sealed class Caption
    {
        public string Kind = "", Label = "", Text = "", Tone = "";
        public double? At;
    }

    internal sealed class Balance
    {
        public string Value = "", Label = "";
    }

    internal sealed class Group
    {
        public string Title = "";
        public List<Row> Rows = new List<Row>();
    }

    internal sealed class Row
    {
        public string Label = "", Short = "", Value = "", Severity = "", State = "";
        public double? Pct, ResetAt, Window, Elapsed, Reserve, RunoutAt;
        /// <summary>Percentage points per hour lately, from Fleet's own readings.</summary>
        public double? Rate;
    }

    internal sealed class Inactive
    {
        public string Name = "", Icon = "", Mark = "", Label = "", Tooltip = "", Action = "";
    }

    internal sealed class Known
    {
        public string Id = "", Name = "";
        public bool Hidden;
    }

    internal static class Clock
    {
        public static double Now() => (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;

        private static DateTime Local(double epoch) =>
            new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(epoch).ToLocalTime();

        /// <summary>"in 41 min", "in 4 h 5 min", "in 1 d 11 h": a countdown in plain words.</summary>
        public static string In(double? epoch)
        {
            if (epoch == null)
            {
                return "";
            }
            var seconds = (long)Math.Max(0, epoch.Value - Now());
            if (seconds < 60) return "now";
            if (seconds < 3600) return "in " + Math.Max(1, seconds / 60) + " min";
            if (seconds < 86400)
            {
                var minutes = seconds / 60 % 60;
                return "in " + seconds / 3600 + " h" + (minutes > 0 ? " " + minutes + " min" : "");
            }
            var hours = seconds / 3600 % 24;
            return "in " + seconds / 86400 + " d" + (hours > 0 ? " " + hours + " h" : "");
        }

        /// <summary>When something happens: "~0:40" within twelve hours, else "in 4 d 15 h".</summary>
        public static string Soon(double? epoch) =>
            epoch == null ? "" : epoch.Value - Now() < 12 * 3600 ? "~" + At(epoch) : In(epoch);

        /// <summary>The local clock time, in the user's own format ("0:20", "12:20 AM").</summary>
        public static string At(double? epoch) =>
            // A non-breaking space keeps "12:43 AM" together when a caption wraps.
            epoch == null ? "" : Local(epoch.Value).ToString("t", CultureInfo.CurrentCulture).Replace(' ', ' ');

        /// <summary>"Resets in 1 d 11 h, Thu 8:00": the countdown with the day and time.</summary>
        public static string ResetText(double? epoch)
        {
            if (epoch == null)
            {
                return "";
            }
            var at = Local(epoch.Value);
            if (at <= DateTime.Now) return "Resetting now";
            var day = at.Date == DateTime.Now.Date ? "" : at.Date == DateTime.Now.Date.AddDays(1)
                ? "tomorrow " : at.ToString("ddd ", CultureInfo.CurrentCulture);
            return "Resets " + In(epoch) + " (" + day + At(epoch) + ")";
        }

        /// <summary>"12 seconds ago", "4 minutes ago".</summary>
        public static string Ago(double epoch)
        {
            var seconds = (int)Math.Max(0, Now() - epoch);
            if (seconds < 60) return seconds + (seconds == 1 ? " second ago" : " seconds ago");
            var minutes = seconds / 60;
            if (minutes < 60) return minutes + (minutes == 1 ? " minute ago" : " minutes ago");
            var hours = minutes / 60;
            return hours + (hours == 1 ? " hour ago" : " hours ago");
        }

        /// <summary>"12s", "4m", "2h": how old a reading is.</summary>
        public static string Age(double epoch)
        {
            var seconds = Math.Max(0, Now() - epoch);
            if (seconds < 60) return (int)seconds + "s";
            if (seconds < 3600) return (int)(seconds / 60) + "m";
            if (seconds < 172800) return (int)(seconds / 3600) + "h";
            return (int)(seconds / 86400) + "d";
        }

        /// <summary>"41m", "4h 10m", "5d 7h": a countdown narrow enough for a column.</summary>
        public static string Left(double? epoch)
        {
            if (epoch == null)
            {
                return "";
            }
            var seconds = (long)Math.Max(0, epoch.Value - Now());
            if (seconds < 60) return "now";
            if (seconds < 3600) return seconds / 60 + "m";
            if (seconds < 86400)
            {
                var minutes = seconds / 60 % 60;
                return seconds / 3600 + "h" + (minutes > 0 ? " " + minutes + "m" : "");
            }
            var hours = seconds / 3600 % 24;
            return seconds / 86400 + "d" + (hours > 0 ? " " + hours + "h" : "");
        }

        /// <summary>"1:19 AM" within the day, "Fri 8:00 AM" later in the week.</summary>
        public static string Day(double? epoch)
        {
            if (epoch == null)
            {
                return "";
            }
            var at = Local(epoch.Value);
            // Within a day the clock time says it; past that, the weekday too.
            if (at - DateTime.Now < TimeSpan.FromHours(20)) return At(epoch);
            return at.ToString("ddd ", CultureInfo.CurrentCulture) + At(epoch);
        }
    }

    /// <summary>Money and token counts the way the flyout prints them.</summary>
    internal static class Format
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        /// <summary>"$0.42", "$86.20", "$412", "$10,565", "$123k"; compact drops cents from $10 up.</summary>
        public static string Money(double usd, bool compact = false)
        {
            if (usd < 0.005) return "$0";
            if (usd < (compact ? 10 : 100)) return "$" + usd.ToString("0.00", Inv);
            if (usd < 100000) return "$" + usd.ToString("#,0", Inv);
            if (usd < 1000000) return "$" + (usd / 1000).ToString("0", Inv) + "k";
            return "$" + (usd / 1000000).ToString("0.0", Inv) + "M";
        }

        /// <summary>"980", "41k", "12.4M", "1.2B".</summary>
        public static string Tokens(double count)
        {
            if (count < 1000) return Math.Round(count).ToString(Inv);
            if (count < 1000000) return (count / 1000).ToString(count < 10000 ? "0.0" : "0", Inv) + "k";
            if (count < 1000000000) return (count / 1000000).ToString(count < 100000000 ? "0.0" : "0", Inv) + "M";
            return (count / 1000000000).ToString("0.0", Inv) + "B";
        }

        public static string Percent(double share) => Math.Round(share * 100).ToString(Inv) + "%";
    }
}
