"""Dashboard history is useful independently of a current account connection."""
import json
import unittest

import panel_model


def usage():
    total = {"cost": 4.25, "tokens": 13200, "input": 5000, "output": 8200}
    return {"complete": True, "claude": {
        "today": total, "week": total, "month": total,
        "daily": [{"day": "2026-10-25", "cost": 4.25, "tokens": 13200}],
        "models": [{"name": "Model " + str(i), "cost": i, "tokens": i * 100} for i in range(8)],
        "projects": [{"name": "Project " + str(i), "cost": i} for i in range(8)],
        "unpriced": 2,
    }, "coverage": {"files": 41, "pending_files": 0, "skipped_zst": 3,
                     "updated": 1000, "since": 100, "raw_response": "never export me"}}


class DashboardHistoryTests(unittest.TestCase):
    def model(self, data=None, **kwargs):
        return panel_model.build(data or [], 1000, now=1000, usage=usage(), **kwargs)

    def test_history_survives_a_signed_out_account(self):
        model = self.model([{"agent": "Claude", "err": "no Claude Code access token", "metrics": []}])
        self.assertEqual(model["providers"], [])
        self.assertEqual(model["history"][0]["name"], "Claude Code")
        self.assertEqual(model["history"][0]["spend"]["today"]["tokens"], 13200)

    def test_daily_history_and_more_than_three_models_and_projects_are_available(self):
        spend = self.model()["history"][0]["spend"]
        self.assertEqual(spend["daily"][0]["tokens"], 13200)
        self.assertEqual((len(spend["models"]), len(spend["projects"])), (8, 8))
        self.assertEqual(spend["models"][7]["tokens"], 700)
        self.assertEqual(spend["unpriced"], 2)

    def test_cost_disabled_and_hidden_providers_stay_hidden(self):
        model = self.model(settings={"cost": False})
        self.assertEqual(model["history"], [])
        self.assertIsNone(model["coverage"])
        self.assertEqual(self.model(settings={"hidden": ["claude"]})["history"], [])

    def test_coverage_is_allowlisted_without_raw_payload_fields(self):
        model = self.model()
        self.assertEqual(model["coverage"]["skipped_zst"], 3)
        self.assertNotIn("raw_response", model["coverage"])
        self.assertNotIn("never export me", json.dumps(model))

    def test_compact_api_rows_remain_available_to_the_dashboard(self):
        model = self.model([{"agent": "Custom API", "api_id": "custom", "compact": True,
                             "err": "", "metrics": [
                                 {"label": "Daily", "pct": 21}, {"label": "Monthly", "pct": 64}]}])
        provider = model["providers"][0]
        self.assertEqual(sum(len(g["rows"]) for g in provider["groups"]), 1)
        self.assertEqual(sum(len(g["rows"]) for g in provider["all_groups"]), 2)

    def test_partial_indexing_does_not_present_incomplete_totals_as_ready(self):
        partial = usage()
        partial["complete"] = False
        model = panel_model.build([], 1000, now=1000, usage=partial)
        self.assertEqual(model["history"], [])
        self.assertEqual(model["summary"]["state"], "indexing")


if __name__ == "__main__":
    unittest.main()
