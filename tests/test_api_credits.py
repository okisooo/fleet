"""ChatGPT's API credits on the Codex card, and the cross-account reset list.

Offline: every HTTP call is a fixture, and no credential store is read.
"""
import json
import os
import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import apikeys
import fleet
import panel_model

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).timestamp()
RENEWS = datetime(2026, 12, 4, 21, 16, 1, tzinfo=timezone.utc)
STARTED = datetime(2026, 11, 4, 21, 16, 1, tzinfo=timezone.utc).timestamp()
KEY = "fixture-admin-key"


def account_check(**entitlement):
    ent = {"has_active_subscription": True, "is_active_subscription_gratis": False,
           "subscription_plan": "chatgptprolite", "billing_period": "monthly",
           "renews_at": RENEWS.isoformat(), "expires_at": "2026-12-05T03:16:01+00:00"}
    ent.update(entitlement)
    return {"accounts": {"acct-1": {"account": {"plan_type": "prolite"}, "entitlement": ent}}}


def costs(*values, more=None):
    page = {"object": "page", "data": [{"object": "bucket", "results": [
        {"object": "organization.costs.result", "amount": {"value": v, "currency": "usd"}} for v in values]}],
        "has_more": more is not None}
    if more:
        page["next_page"] = more
    return page


class BillingCycleTests(unittest.TestCase):
    def setUp(self):
        fleet._CHATGPT_CYCLE.update(account=None, at=0.0, cycle=None)

    def test_the_cycle_ends_at_renewal_and_began_a_calendar_month_earlier(self):
        cycle = fleet._parse_chatgpt_cycle(account_check(), "acct-1")
        self.assertEqual(cycle, {"start": STARTED, "end": RENEWS.timestamp()})

    def test_a_month_end_renewal_starts_on_the_previous_month_end(self):
        march = datetime(2027, 3, 31, 9, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(fleet._month_before(march), datetime(2027, 2, 28, 9, 0, tzinfo=timezone.utc).timestamp())
        january = datetime(2027, 1, 15, tzinfo=timezone.utc).timestamp()
        self.assertEqual(fleet._month_before(january), datetime(2026, 12, 15, tzinfo=timezone.utc).timestamp())

    def test_no_cycle_for_trials_complimentary_or_yearly_plans(self):
        for ent in ({"has_active_subscription": False}, {"is_active_subscription_gratis": True},
                    {"billing_period": "yearly"}, {"renews_at": None, "expires_at": None}):
            self.assertIsNone(fleet._parse_chatgpt_cycle(account_check(**ent), "acct-1"), ent)
        self.assertIsNone(fleet._parse_chatgpt_cycle({"accounts": []}, "acct-1"))
        self.assertIsNone(fleet._parse_chatgpt_cycle(None))

    def test_a_cancelled_plan_keeps_its_credits_until_the_paid_period_ends(self):
        cycle = fleet._parse_chatgpt_cycle(account_check(renews_at=None), "acct-1")
        self.assertEqual(cycle["end"], datetime(2026, 12, 5, 3, 16, 1, tzinfo=timezone.utc).timestamp())

    def test_the_cycle_is_read_once_and_again_after_it_ends(self):
        with mock.patch.object(fleet, "_jget", return_value=(200, {}, account_check())) as get:
            first = fleet._chatgpt_cycle("tok", {}, "acct-1", now=NOW)
            self.assertEqual(fleet._chatgpt_cycle("tok", {}, "acct-1", now=NOW + 3600), first)
            self.assertEqual(get.call_count, 1)
            fleet._chatgpt_cycle("tok", {}, "acct-1", now=RENEWS.timestamp() + 60)
            self.assertEqual(get.call_count, 2)

    def test_a_failed_read_keeps_the_known_cycle(self):
        with mock.patch.object(fleet, "_jget", return_value=(200, {}, account_check())):
            first = fleet._chatgpt_cycle("tok", {}, "acct-1", now=NOW)
        with mock.patch.object(fleet, "_jget", return_value=(503, {}, {})):
            self.assertEqual(fleet._chatgpt_cycle("tok", {}, "acct-1", now=NOW + 7 * 3600), first)

    def test_a_failed_first_read_is_retried_in_fifteen_minutes_not_six_hours(self):
        with mock.patch.object(fleet, "_jget", return_value=(0, {}, {})) as get:
            self.assertIsNone(fleet._chatgpt_cycle("tok", {}, "acct-1", now=NOW))
            self.assertIsNone(fleet._chatgpt_cycle("tok", {}, "acct-1", now=NOW + 600))
            self.assertEqual(get.call_count, 1)
        with mock.patch.object(fleet, "_jget", return_value=(200, {}, account_check())):
            self.assertIsNotNone(fleet._chatgpt_cycle("tok", {}, "acct-1", now=NOW + 960))


class ApiCreditTests(unittest.TestCase):
    def setUp(self):
        fleet._API_CREDIT_SPEND.update(key=None, at=0.0, spend=None, err="")
        fleet._CHATGPT_CYCLE.update(account=None, at=0.0, cycle=None)
        self.cycle = {"start": STARTED, "end": RENEWS.timestamp()}

    def test_spend_since_the_cycle_start_is_summed_across_pages(self):
        pages = [(200, {}, costs(12.5, 0.25, more="p2")), (200, {}, costs(3.0))]
        with mock.patch.object(fleet, "_jget", side_effect=pages) as get:
            spend, err = fleet._openai_spend_since(KEY, STARTED, now=NOW)
        self.assertEqual((spend, err), (15.75, ""))
        first = parse_qs(urlparse(get.call_args_list[0].args[0]).query)
        self.assertEqual(first["start_time"], [str(int(STARTED))])
        self.assertEqual(get.call_args_list[0].args[1], KEY)
        self.assertEqual(parse_qs(urlparse(get.call_args_list[1].args[0]).query)["page"], ["p2"])

    def test_spend_is_cached_and_a_failure_keeps_the_last_total(self):
        with mock.patch.object(fleet, "_jget", return_value=(200, {}, costs(40))) as get:
            fleet._openai_spend_since(KEY, STARTED, now=NOW)
            fleet._openai_spend_since(KEY, STARTED, now=NOW + 60)
        self.assertEqual(get.call_count, 1)
        with mock.patch.object(fleet, "_jget", return_value=(429, {}, {})):
            self.assertEqual(fleet._openai_spend_since(KEY, STARTED, now=NOW + 3600),
                             (40.0, "OpenAI is rate limiting cost reports"))

    def test_a_rejected_key_or_odd_report_gives_no_total(self):
        with mock.patch.object(fleet, "_jget", return_value=(401, {}, {"error": {}})):
            self.assertEqual(fleet._openai_spend_since(KEY, STARTED, now=NOW), (None, "OpenAI rejected the admin key"))
        fleet._API_CREDIT_SPEND.update(key=None)
        odd = costs(1)
        odd["data"][0]["results"][0]["amount"]["currency"] = "eur"
        with mock.patch.object(fleet, "_jget", return_value=(200, {}, odd)):
            self.assertEqual(fleet._openai_spend_since(KEY, STARTED, now=NOW), (None, "unreadable cost report"))

    def test_the_row_shows_what_is_left_and_when_it_renews(self):
        metric = fleet._api_credits_metric("prolite", self.cycle, spend=37.2, keyed=True)
        self.assertEqual(metric["label"], "API credits")
        self.assertEqual(metric["pct"], 37.2)
        self.assertEqual(metric["extra"], "$62.80 of $100 left")
        self.assertEqual(metric["resets"], RENEWS.timestamp())
        self.assertEqual(metric["window_seconds"], round(RENEWS.timestamp() - STARTED))
        self.assertIn("since", metric["note"])

    def test_spend_past_the_grant_counts_the_grant_as_used_up(self):
        metric = fleet._api_credits_metric("prolite", self.cycle, spend=180.0, keyed=True)
        self.assertEqual((metric["pct"], metric["extra"]), (100.0, "$0.00 of $100 left"))

    def test_without_an_admin_key_the_row_still_shows_the_renewal(self):
        metric = fleet._api_credits_metric("prolite", self.cycle)
        self.assertIsNone(metric["pct"])
        self.assertEqual(metric["extra"], "$100 a month")
        self.assertEqual(metric["resets"], RENEWS.timestamp())
        self.assertIn("admin key", metric["note"])

    def test_plans_without_a_known_grant_make_no_extra_requests(self):
        with mock.patch.object(fleet, "_jget") as get:
            self.assertIsNone(fleet._codex_api_credits("plus", "tok", {}, "acct-1"))
            self.assertIsNone(fleet._codex_api_credits("pro", "tok", {}, "acct-1"))
        get.assert_not_called()

    def test_codex_card_reads_the_cycle_then_the_costs_with_the_saved_key(self):
        def fake(url, token, *rest, **kw):
            if "accounts/check" in url:
                self.assertEqual(token, "tok")
                return 200, {}, account_check()
            self.assertEqual(token, KEY)
            return 200, {}, costs(20)
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(apikeys, "_get_registry_env", return_value=None), \
                mock.patch.object(apikeys.api_credentials, "read", side_effect=lambda pid: KEY if pid == "openai-admin" else None), \
                mock.patch.object(fleet, "_jget", side_effect=fake):
            metric = fleet._codex_api_credits("prolite", "tok", {}, "acct-1")
        self.assertEqual(metric["extra"], "$80.00 of $100 left")

    def test_the_saved_admin_key_is_reported_to_settings_without_its_value(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(apikeys, "_get_registry_env", return_value=None), \
                mock.patch.object(apikeys.api_credentials, "read", side_effect=lambda pid: KEY if pid == "openai-admin" else None):
            accounts = apikeys.account_status()
            self.assertEqual([s["id"] for s in apikeys.enabled_specs({})], [])
        self.assertIn({"id": "openai-admin", "source": "windows", "enabled": True}, accounts)
        self.assertNotIn(KEY, json.dumps(accounts))
        model = panel_model.build([], NOW, now=NOW, accounts=accounts)
        self.assertIn({"id": "openai-admin", "source": "windows", "enabled": True}, model["api_accounts"])


def metric(label, pct=None, resets=None, extra="", **more):
    return dict({"label": label, "pct": pct, "resets": resets, "extra": extra}, **more)


class PanelCreditTests(unittest.TestCase):
    def codex(self, credits):
        model = panel_model.build([{"agent": "Codex", "plan": "prolite", "err": "", "metrics": [
            metric("Weekly", 47.0, NOW + 5 * 86400), credits]}], NOW, now=NOW)
        json.dumps(model)
        return model, model["providers"][0]

    def test_the_credits_row_is_a_meter_on_the_closed_card_but_never_the_ring(self):
        cycle = {"start": STARTED, "end": RENEWS.timestamp()}
        model, codex = self.codex(fleet._api_credits_metric("prolite", cycle, spend=37.2, keyed=True))
        self.assertEqual([(m["short"], m["value"]) for m in codex["meters"]],
                         [("Weekly", "47%"), ("API credits", "$62.80 of $100 left")])
        credits = codex["meters"][1]
        self.assertEqual((credits["pct"], credits["reset_at"]), (37.2, RENEWS.timestamp()))
        self.assertEqual((credits["amount"], credits["amount_total"]), (37.2, 100.0))
        self.assertNotIn("amount", codex["meters"][0])
        self.assertEqual(credits["window"], round(RENEWS.timestamp() - STARTED))
        self.assertIsNotNone(credits["elapsed"])
        self.assertEqual([r["label"] for r in codex["rings"]], ["Weekly"])
        self.assertIn("spend since", codex["note"])

    def test_without_a_key_the_closed_card_shows_the_grant_as_text(self):
        cycle = {"start": STARTED, "end": RENEWS.timestamp()}
        _, codex = self.codex(fleet._api_credits_metric("prolite", cycle))
        self.assertEqual([(m["short"], m["pct"], m["value"]) for m in codex["meters"]][1],
                         ("API credits", None, "$100 a month"))
        self.assertNotIn("amount", codex["meters"][1])
        self.assertIn("admin key", codex["note"])


class UpcomingResetTests(unittest.TestCase):
    def test_every_account_is_listed_soonest_first_with_shared_resets_merged(self):
        model = panel_model.build([
            {"agent": "Claude", "family": "claude", "plan": "Max 20x", "err": "", "metrics": [
                metric("Session", 3.0, NOW + 3 * 3600), metric("Weekly", 100.0, NOW + 2 * 86400 + 0.2),
                metric("Fable", 100.0, NOW + 2 * 86400 + 0.4)]},
            {"agent": "Codex", "plan": "prolite", "err": "", "metrics": [
                metric("Weekly", 47.0, NOW + 5 * 86400),
                metric("Resets", None, NOW + 15 * 86400, "2 available · until Dec 2"),
                metric("API credits", None, RENEWS.timestamp(), "$100 a month", window_seconds=2592000)]},
            {"agent": "Antigrav", "plan": "", "err": "", "metrics": [
                metric("Gemini", 3.0, NOW + 3600, group="Gemini", window="5h")]},
        ], NOW, now=NOW)
        lines = [(r["name"], ", ".join(r["labels"]), r["kind"], r["state"]) for r in model["resets"]]
        self.assertEqual(lines, [
            ("Antigravity", "Gemini 5-hour", "limit", "calm"),
            ("Claude", "5-hour", "limit", "calm"),
            ("Claude", "Weekly, Fable wk", "limit", "full"),
            ("Codex", "Weekly", "limit", "calm"),
            ("Codex", "2 free resets expire", "expiry", "calm"),
            ("Codex", "API credits renew", "credits", "calm"),
        ])
        self.assertEqual(model["resets"][-1]["value"], "$100 a month")
        self.assertEqual([r["at"] for r in model["resets"]], sorted(r["at"] for r in model["resets"]))

    def test_a_used_up_limit_gets_its_own_line_beside_one_that_is_not(self):
        model = panel_model.build([
            {"agent": "Claude OXO", "family": "claude", "plan": "Max 5x", "err": "", "metrics": [
                metric("Weekly", 73.0, NOW + 3 * 86400), metric("Fable", 100.0, NOW + 3 * 86400 + 0.3)]},
        ], NOW, now=NOW)
        self.assertEqual([(", ".join(r["labels"]), r["state"]) for r in model["resets"]],
                         [("Weekly", "calm"), ("Fable wk", "full")])

    def test_one_waiting_reset_reads_in_the_singular(self):
        self.assertEqual(panel_model._expiring("1 available · until Dec 2"), "1 free reset expires")
        self.assertEqual(panel_model._expiring("5-hour ready"), "Free resets expire")

    def test_past_resets_and_hidden_providers_are_left_out(self):
        model = panel_model.build([
            {"agent": "Codex", "plan": "pro", "err": "", "metrics": [metric("Weekly", 10.0, NOW - 60)]},
            {"agent": "Grok", "plan": "", "err": "", "metrics": [metric("Weekly", 10.0, NOW + 60)]},
        ], NOW, now=NOW, settings={"hidden": ["grok"]})
        self.assertEqual(model["resets"], [])


if __name__ == "__main__":
    unittest.main()
