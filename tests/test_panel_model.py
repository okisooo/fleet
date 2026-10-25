import json
import unittest

import fleet
import panel_model


def _metric(label, pct=None, resets=None, extra=""):
    return {"label": label, "pct": pct, "resets": resets, "extra": extra}


class PanelModelTests(unittest.TestCase):
    def build(self, data, ts=1000.0):
        model = panel_model.build(data, ts, now=1000.0)
        json.dumps(model)  # the native panel reads it as plain JSON
        return model

    def test_antigravity_quotas_are_grouped_by_model_family(self):
        model = self.build([{
            "agent": "Antigrav", "plan": "Google AI Pro", "err": "", "metrics": [
                _metric("Weekly", 0.7), _metric("Session", 0.0),
                _metric("Claude Wk", 54.0), _metric("Claude", 92.0),
            ],
        }])

        provider = model["providers"][0]
        self.assertEqual(provider["name"], "Antigravity")
        self.assertEqual([group["title"] for group in provider["groups"]], ["Gemini", "Claude"])
        claude = provider["groups"][1]["rows"]
        self.assertEqual([(row["label"], row["value"], row["severity"]) for row in claude],
                         [("Weekly limit", "54%", "calm"), ("5-hour limit", "92%", "high")])

    def test_resets_become_epoch_seconds_whatever_the_provider_sent(self):
        model = self.build([{
            "agent": "Codex", "err": "", "metrics": [
                _metric("Weekly", 18, 1791580293),
                _metric("Session", 3, "2026-10-04T02:04:15Z"),
                _metric("Daily", 1, 1791580293000),
            ],
        }])

        resets = [row["reset_at"] for row in model["providers"][0]["groups"][0]["rows"]]
        self.assertEqual(resets[0], 1791580293)
        self.assertEqual(resets[1], fleet._epoch("2026-10-04T02:04:15Z"))
        self.assertEqual(resets[2], 1791580293)

    def test_an_unused_grok_week_is_live_and_its_off_pay_go_is_dropped(self):
        model = self.build([{
            "agent": "Grok", "plan": "X Premium", "err": "", "metrics": [
                _metric("Weekly", 0.0, "2026-10-04T23:49:28+00:00"), _metric("Pay-go", extra="off"),
            ],
        }])

        rows = model["providers"][0]["groups"][0]["rows"]
        self.assertEqual([(row["label"], row["value"]) for row in rows], [("Weekly limit", "0%")])
        self.assertEqual(model["inactive"], [])

    def test_an_off_only_provider_is_listed_as_inactive(self):
        model = self.build([{"agent": "Grok", "err": "", "known": True,
                             "metrics": [_metric("Pay-go", extra="off")]}])

        self.assertEqual(model["providers"], [])
        self.assertEqual(model["inactive"][0]["label"], "Off")

    def test_zero_money_is_hidden_but_zero_usage_is_not(self):
        model = self.build([{
            "agent": "Codex", "err": "", "metrics": [
                _metric("Weekly", 0.0), _metric("Credits", extra="$0.00"),
            ],
        }])

        rows = model["providers"][0]["groups"][0]["rows"]
        self.assertEqual([row["label"] for row in rows], ["Weekly limit"])

    def test_a_signed_out_claude_offers_sign_in(self):
        model = self.build([{"agent": "Claude", "err": fleet.CLAUDE_SIGNED_OUT,
                             "known": True, "configured": False, "metrics": []}])

        entry = model["inactive"][0]
        self.assertEqual((entry["name"], entry["label"], entry["action"]),
                         ("Claude", "Signed out", "claude-sign-in"))
        self.assertIn("claude auth login", entry["tooltip"])

    def test_an_inactive_provider_gets_a_short_plain_status(self):
        # A long reason once squeezed "Claude" down to "Cl" in the flyout.
        model = self.build([
            {"agent": "Grok", "err": "login expired 6d ago (run grok)", "metrics": []},
            {"agent": "Codex", "err": "rate limited", "metrics": []},
            {"agent": "Moonshot", "err": "http 503 · retry later", "metrics": []},
        ])

        self.assertEqual([entry["label"] for entry in model["inactive"]],
                         ["Sign-in expired", "Rate limited", "Http 503"])
        self.assertEqual(model["inactive"][0]["action"], "grok-renew")
        self.assertIn("grok", model["inactive"][0]["tooltip"])

    # ---------- rings, pace and captions (schema 2) ----------

    def _claude(self, session, weekly, session_left, weekly_left, extra=()):
        return {"agent": "Claude", "plan": "max", "err": "", "metrics": [
            _metric("Session", session, 1000.0 + session_left),
            _metric("Weekly", weekly, 1000.0 + weekly_left), *extra]}

    def test_claude_rings_are_weekly_outside_and_5_hour_inside(self):
        model = self.build([self._claude(21, 75, 4 * 3600, 30 * 3600, [_metric("Fable", 0, 1000.0 + 30 * 3600)])])

        provider = model["providers"][0]
        self.assertEqual(provider["kind"], "rings")
        self.assertEqual([(r["label"], r["pct"]) for r in provider["rings"]], [("Weekly", 75.0), ("5-hour", 21.0)])
        self.assertEqual(provider["headline"], 75.0)

    def test_a_limit_running_ahead_of_its_window_forecasts_when_it_runs_out(self):
        # 21% used 50 minutes into a 5-hour window: empty in about 3 h 58 min.
        model = self.build([self._claude(21, 75, 4 * 3600 + 600, 30 * 3600)])

        session = model["providers"][0]["groups"][0]["rows"][0]
        self.assertEqual(session["state"], "ahead")
        self.assertAlmostEqual(session["runout_at"], 1000 + 50 * 60 / 21 * 79, delta=5)
        self.assertLess(session["reserve"], 0)
        caption = model["providers"][0]["caption"]
        self.assertEqual((caption["kind"], caption["label"], caption["tone"]), ("runout", "5-hour", "warn"))

    def test_a_limit_under_pace_says_when_it_resets_and_keeps_its_reserve(self):
        model = self.build([self._claude(10, 60, 2 * 3600, 30 * 3600)])

        weekly = model["providers"][0]["groups"][0]["rows"][1]
        self.assertEqual(weekly["state"], "calm")
        self.assertIsNone(weekly["runout_at"])
        self.assertAlmostEqual(weekly["reserve"], (1 - 30 / 168) * 100 - 60, places=0)
        caption = model["providers"][0]["caption"]
        self.assertEqual((caption["kind"], caption["label"], caption["at"]), ("reset", "Weekly", 1000.0 + 30 * 3600))

    def test_no_forecast_this_early_in_a_window(self):
        model = self.build([self._claude(30, 1, 5 * 3600 - 60, 7 * 86400 - 60)])

        session = model["providers"][0]["groups"][0]["rows"][0]
        self.assertIsNone(session["runout_at"])
        self.assertIsNone(session["reserve"])

    def test_a_used_up_limit_says_so_until_it_resets(self):
        model = self.build([self._claude(100, 80, 1800, 30 * 3600)])

        caption = model["providers"][0]["caption"]
        self.assertEqual((caption["kind"], caption["label"], caption["tone"]), ("full", "5-hour", "high"))

    def test_a_balance_only_provider_is_a_balance_tile(self):
        model = self.build([{"agent": "OpenRouter", "api_id": "openrouter", "compact": True, "err": "",
                             "metrics": [_metric("Balance", None, None, "$4.75")]}])

        provider = model["providers"][0]
        self.assertEqual((provider["kind"], provider["rings"], provider["balance"]),
                         ("balance", [], {"value": "$4.75", "label": "Credit balance"}))

    def test_antigravity_rings_follow_its_busiest_family(self):
        model = self.build([{"agent": "Antigrav", "plan": "Google AI Pro", "err": "", "metrics": [
            _metric("Weekly", 1, 1000.0 + 86400), _metric("Session", 0, 1000.0 + 3600),
            _metric("Claude Wk", 0, 1000.0 + 86400), _metric("Claude", 0, 1000.0 + 3600)]}])

        provider = model["providers"][0]
        self.assertEqual([r["pct"] for r in provider["rings"]], [1.0, 0.0])
        self.assertEqual(provider["caption"], {"kind": "text", "text": "Claude unused", "tone": "calm"})

    def test_hidden_providers_stay_listed_for_settings_but_not_shown(self):
        model = panel_model.build([self._claude(10, 20, 3600, 86400),
                                   {"agent": "Codex", "err": "", "metrics": [_metric("Weekly", 5)]}],
                                  1000.0, now=1000.0, settings={"hidden": ["codex"], "units": "left"})

        self.assertEqual([p["name"] for p in model["providers"]], ["Claude"])
        self.assertEqual([(k["name"], k["hidden"]) for k in model["known"]], [("Claude", False), ("Codex", True)])
        self.assertEqual(model["settings"], {"units": "left"})

    def test_a_weekly_limit_barely_ahead_of_its_week_is_not_a_warning(self):
        # 20% used 31 hours into the week: at this rate it would run out 12 hours
        # before the reset. True, but far too early in the window to shout about.
        model = self.build([{"agent": "Codex", "err": "", "metrics": [_metric("Weekly", 20, 1000.0 + 137 * 3600)]}])

        weekly = model["providers"][0]["groups"][0]["rows"][0]
        self.assertIsNone(weekly["runout_at"])
        self.assertEqual(weekly["state"], "calm")

    def test_a_reading_is_only_stale_after_a_quarter_of_an_hour(self):
        recent = panel_model.build([{"agent": "Codex", "err": "cached 5m ago · rate limited", "cached_at": 700.0,
                                     "metrics": [_metric("Weekly", 5)]}], 1000.0, now=1000.0)
        old = panel_model.build([{"agent": "Codex", "err": "cached 20m ago · rate limited", "cached_at": 700.0,
                                  "metrics": [_metric("Weekly", 5)]}], 1000.0, now=1000.0 + 1200)

        self.assertFalse(recent["providers"][0]["freshness"]["stale"])
        self.assertTrue(old["providers"][0]["freshness"]["stale"])

    def test_never_seen_providers_are_omitted(self):
        model = self.build([{"agent": "Claude", "err": "no Claude Code credentials",
                             "known": False, "configured": False, "metrics": []}])

        self.assertEqual((model["providers"], model["inactive"]), ([], []))

    def test_cached_and_desktop_readings_carry_their_real_age(self):
        model = self.build([
            {"agent": "Codex", "err": "offline", "cached_at": 700.0, "metrics": [_metric("Weekly", 5)]},
            {"agent": "Claude", "err": "", "source": "desktop", "cached_at": 800.0,
             "metrics": [_metric("Session", 12)]},
            {"agent": "Grok", "err": "", "metrics": [_metric("Weekly", 1)]},
        ])

        freshness = [provider["freshness"] for provider in model["providers"]]
        self.assertEqual(freshness, [{"kind": "cached", "at": 700.0, "reason": "Offline", "stale": False},
                                     {"kind": "desktop", "at": 800.0, "stale": False}, None])
        self.assertEqual((model["live"], model["cached"]), (1, 2))

    def test_a_stale_reading_says_why_in_plain_words(self):
        model = self.build([
            {"agent": "Grok", "err": "cached 11h ago · login expired 11h ago (run grok)",
             "cached_at": 700.0, "metrics": [_metric("Weekly", 1)]},
            {"agent": "Codex", "err": "cached 2m ago · http 429", "cached_at": 800.0,
             "metrics": [_metric("Weekly", 5)]},
            {"agent": "Claude", "err": "cached 1m ago · http 500", "cached_at": 900.0,
             "metrics": [_metric("Session", 12)]},
        ])

        reasons = [provider["freshness"].get("reason") for provider in model["providers"]]
        self.assertEqual(reasons, ["Rate limited", None])
        # Numbers behind a dead login are not shown; the fix is offered instead.
        self.assertEqual([(i["name"], i["action"]) for i in model["inactive"]], [("Grok", "grok-renew")])

    def test_grok_soft_cache_is_a_healthy_reading(self):
        model = self.build([{"agent": "Grok", "err": "soft cache", "cached_at": 700.0,
                             "metrics": [_metric("Weekly", 1)]}])

        provider = model["providers"][0]
        self.assertEqual((provider["freshness"], provider["detail"]), (None, ""))
        self.assertEqual((model["live"], model["cached"]), (1, 0))

    def test_a_compact_api_provider_shows_its_worst_window(self):
        model = self.build([{"agent": "Moonshot", "api_id": "moonshot", "compact": True, "err": "",
                             "metrics": [_metric("Daily", 10), _metric("Monthly", 70)]}])

        provider = model["providers"][0]
        self.assertEqual(provider["mark"], "MO")
        self.assertEqual([row["label"] for row in provider["groups"][0]["rows"]], ["Monthly limit"])

    def test_rows_carry_plain_labels_and_their_window_length(self):
        model = self.build([
            {"agent": "Claude", "err": "", "metrics": [
                _metric("Session", 33), _metric("Weekly", 11), _metric("Fable", 0)]},
            {"agent": "Antigrav", "err": "", "metrics": [_metric("Weekly", 1), _metric("Session", 0)]},
        ])

        claude = [(row["label"], row["window"]) for row in model["providers"][0]["groups"][0]["rows"]]
        self.assertEqual(claude, [("5-hour limit", 18000), ("Weekly limit", 604800),
                                  ("Fable weekly limit", 604800)])
        gemini = model["providers"][1]["groups"][0]["rows"]
        self.assertEqual([(row["label"], row["window"]) for row in gemini],
                         [("Weekly limit", 604800), ("5-hour limit", 18000)])

    def test_balance_rows_have_no_meter(self):
        model = self.build([{"agent": "OpenRouter", "api_id": "openrouter", "compact": True,
                             "err": "", "metrics": [_metric("Balance", extra="$4.87")]}])

        row = model["providers"][0]["groups"][0]["rows"][0]
        self.assertEqual((row["pct"], row["value"], row["severity"]), (None, "$4.87", "none"))


def _totals(cost, tokens=1000):
    return {"cost": cost, "tokens": tokens, "input": 10, "output": 5, "cache_read": 900,
            "cache_write": 85, "messages": 3}


def _usage(complete=True):
    claude = {"today": _totals(86.2), "week": _totals(412.7), "month": _totals(1284.1),
              "spans": {"5-hour": _totals(61.2)}, "rate": {"cost_per_hour": 14.1, "tokens_per_min": 38000, "minutes": 30},
              "hourly": [0.0] * 23 + [9.5], "daily": [],
              "models": [{"id": "claude-opus-5-5", "name": "Opus 5.5", "cost": 380.0, "share": 0.92}],
              "projects": [{"name": "agent-usage", "cost": 210.0}], "estimated": False}
    codex = dict(claude, spans={}, estimated=True, today=_totals(11.0))
    return {"claude": claude, "codex": codex, "complete": complete,
            "total": {"today": _totals(97.2), "week": _totals(500.0), "month": _totals(1500.0)}}


class DashboardModelTests(unittest.TestCase):
    """Schema 3: the closed-section meters, API-equivalent spend and burn rates."""

    def build(self, data, **kwargs):
        model = panel_model.build(data, 1000.0, now=1000.0, **kwargs)
        json.dumps(model)
        return model

    def test_a_closed_section_shows_the_all_model_limits_shortest_first(self):
        model = self.build([{"agent": "Claude", "err": "", "metrics": [
            _metric("Weekly", 47, 1000 + 86400), _metric("Session", 30, 1000 + 3600), _metric("Fable", 9, 1000 + 86400)]}])

        meters = model["providers"][0]["meters"]
        self.assertEqual([(m["short"], m["pct"]) for m in meters], [("5-hour", 30), ("Weekly", 47)])

    def test_a_scoped_limit_joins_the_meters_only_while_it_is_the_fullest(self):
        model = self.build([{"agent": "Claude", "err": "", "metrics": [
            _metric("Session", 30), _metric("Weekly", 47), _metric("Fable", 88)]}])

        meters = model["providers"][0]["meters"]
        self.assertEqual([m["short"] for m in meters], ["5-hour", "Weekly", "Fable wk"])

    def test_each_model_family_shows_its_busiest_window_under_its_name(self):
        model = self.build([{"agent": "Antigrav", "err": "", "metrics": [
            _metric("Weekly", 3), _metric("Session", 12), _metric("Claude Wk", 40), _metric("Claude", 5)]}])

        meters = model["providers"][0]["meters"]
        self.assertEqual([(m["short"], m["label"], m["pct"]) for m in meters],
                         [("Gemini", "5-hour limit", 12), ("Claude", "Weekly limit", 40)])

    def test_the_spend_window_follows_the_shortest_limit(self):
        data = [{"agent": "Claude", "err": "", "metrics": [
            _metric("Session", 30, 1000 + 3600), _metric("Weekly", 47, 1000 + 86400)]},
            {"agent": "Grok", "err": "", "metrics": [_metric("Weekly", 1, 5000)]}]

        self.assertEqual(panel_model.window_spans(data), {"claude": {"5-hour": 1000 + 3600 - 5 * 3600}})

    def test_spend_and_totals_come_from_the_ledger_once_it_has_read_everything(self):
        data = [{"agent": "Claude", "plan": "max", "err": "", "metrics": [
            _metric("Session", 30, 1000 + 3600), _metric("Weekly", 47, 1000 + 86400)]},
            {"agent": "Codex", "err": "", "metrics": [_metric("Weekly", 10, 1000 + 86400)]}]

        model = self.build(data, usage=_usage())

        claude, codex = model["providers"]
        self.assertEqual(claude["spend"]["today"]["cost"], 86.2)
        self.assertEqual((claude["spend"]["window_label"], claude["spend"]["window"]["cost"]), ("5-hour", 61.2))
        self.assertEqual(claude["spend"]["rate"]["cost_per_hour"], 14.1)
        self.assertEqual(len(claude["spend"]["hourly"]), 24)
        self.assertEqual((codex["spend"]["window_label"], codex["spend"]["window"]), ("Weekly", None))
        summary = model["summary"]
        self.assertEqual((summary["state"], summary["today"], summary["week"], summary["month"]),
                         ("ready", 97.2, 500.0, 1500.0))
        self.assertTrue(summary["estimated"])

    def test_partial_figures_wait_while_the_ledger_is_still_reading(self):
        data = [{"agent": "Claude", "err": "", "metrics": [_metric("Session", 30, 4600)]}]

        model = self.build(data, usage=_usage(complete=False))

        self.assertIsNone(model["providers"][0]["spend"])
        self.assertEqual(model["summary"], {"state": "indexing"})

    def test_turning_cost_off_drops_every_figure(self):
        data = [{"agent": "Claude", "err": "", "metrics": [_metric("Session", 30, 4600)]}]

        model = self.build(data, usage=_usage(), settings={"cost": False})

        self.assertIsNone(model["providers"][0]["spend"])
        self.assertEqual(model["summary"], {"state": "off"})
        self.assertIs(model["settings"]["cost"], False)

    def test_chatgpt_project_folders_read_as_one_project(self):
        usage = _usage()
        usage["codex"]["projects"] = [{"name": "g-p-6acf944087388191a8c6905996299c19", "cost": 5.0},
                                      {"name": "bing", "cost": 7.0},
                                      {"name": "g-p-0123456789abcdef0123", "cost": 4.0}]
        model = self.build([{"agent": "Codex", "err": "", "metrics": [_metric("Weekly", 10, 90000)]}], usage=usage)

        projects = model["providers"][0]["spend"]["projects"]
        self.assertEqual([(p["name"], p["cost"]) for p in projects], [("ChatGPT project", 9.0), ("bing", 7.0)])

    def test_providers_without_logs_have_no_spend(self):
        model = self.build([{"agent": "Grok", "err": "", "metrics": [_metric("Weekly", 1, 5000)]}], usage=_usage())

        self.assertIsNone(model["providers"][0]["spend"])

    def test_live_rows_carry_their_recent_rate_and_cached_ones_do_not(self):
        live = [{"agent": "Claude", "err": "", "metrics": [_metric("Session", 30, 4600), _metric("Weekly", 47, 90000)]}]
        rates = {"claude": {"5-hour limit": 17.64, "Weekly limit": 0.0}}

        rows = self.build(live, rates=rates)["providers"][0]["groups"][0]["rows"]
        self.assertEqual([row["rate"] for row in rows], [17.6, None])

        cached = [dict(live[0], cached_at=400.0, err="retry in 5m")]
        rows = self.build(cached, rates=rates)["providers"][0]["groups"][0]["rows"]
        self.assertEqual([row["rate"] for row in rows], [None, None])


if __name__ == "__main__":
    unittest.main()
