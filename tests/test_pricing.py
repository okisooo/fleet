import unittest

import pricing

M = 1_000_000


class PricingTests(unittest.TestCase):
    def assertCost(self, result, usd, estimated=False):
        self.assertAlmostEqual(result[0], usd, places=6)
        self.assertEqual(result[1], estimated)

    def test_opus_5_5_list_prices_per_token_class(self):
        self.assertCost(pricing.cost("claude-opus-5-5", input=M), 4)
        self.assertCost(pricing.cost("claude-opus-5-5", output=M), 20)
        self.assertCost(pricing.cost("claude-opus-5-5", cache_write_5m=M), 5)
        self.assertCost(pricing.cost("claude-opus-5-5", cache_write_1h=M), 8)
        self.assertCost(pricing.cost("claude-opus-5-5", cache_read=M), 0.20)

    def test_fast_mode_doubles_opus_and_leaves_sonnet_alone(self):
        self.assertCost(pricing.cost("claude-opus-5-5", input=M, output=M, fast=True), 8 + 40)
        self.assertCost(pricing.cost("claude-opus-5-5|fast", input=M, output=M), 8 + 40)
        self.assertCost(pricing.cost("claude-sonnet-5-5", input=M, output=M, fast=True), 2 + 10)

    def test_ids_are_normalised(self):
        for raw in ("claude-opus-5-5[1m]", "Claude-Opus-5-5-20260301", "us.anthropic.claude-opus-5-5-20260301-v1:0",
                    "global.anthropic.claude-opus-5-5", "anthropic/claude-opus-5-5", "claude-opus-5-5@20260301"):
            self.assertEqual(pricing.normalize(raw), "claude-opus-5-5", raw)
        self.assertEqual(pricing.normalize("gpt-5.5-2026-04-23"), "gpt-5.5")

    def test_haiku_5_5_bills_long_prompts_at_its_upper_tier(self):
        self.assertEqual(pricing.key("claude-haiku-5-5", prompt=100_000), "claude-haiku-5-5")
        self.assertEqual(pricing.key("claude-haiku-5-5", prompt=100_001), "claude-haiku-5-5|long")
        self.assertCost(pricing.cost("claude-haiku-5-5|long", input=M, cache_read=M), 0.50 + 0.05)
        # Other Claude models have no long-context surcharge.
        self.assertEqual(pricing.key("claude-opus-5-5[1m]", prompt=900_000), "claude-opus-5-5")

    def test_gpt_6_1_sol_matches_published_prices(self):
        self.assertCost(pricing.cost("gpt-6.1-sol", input=M, cache_read=M, output=M), 2 + 0.10 + 10)
        self.assertEqual(pricing.key("gpt-6.1-sol", prompt=272_001), "gpt-6.1-sol|long")
        self.assertCost(pricing.cost("gpt-6.1-sol|long", input=M), 4)

    def test_codex_priority_tier_uses_the_published_multiplier(self):
        self.assertEqual(pricing.key("gpt-6.1-sol", fast=True, prompt=300_000), "gpt-6.1-sol|fast|long")
        self.assertCost(pricing.cost("gpt-6.1-sol|fast|long", input=M, output=M), 8 + 30)
        # No published priority rate: the usual 2x, flagged as an estimate.
        self.assertCost(pricing.cost("gpt-daybreak-blue-latest|fast", input=M), 8, estimated=True)

    def test_unknown_models_fall_back_to_their_family_or_stay_unpriced(self):
        self.assertCost(pricing.cost("claude-opus-9", input=M), 4, estimated=True)
        self.assertCost(pricing.cost("claude-3-opus-20240229", output=M), 20, estimated=True)
        self.assertCost(pricing.cost("gpt-7-sol", input=M), 2, estimated=True)
        self.assertCost(pricing.cost("gpt-6.1-luna", input=M), 0.10, estimated=True)
        self.assertEqual(pricing.cost("mystery-model", input=M), (None, False))
        self.assertEqual(pricing.cost("gpt-4o-mini", input=M), (None, False))

    def test_labels_are_short_display_names(self):
        self.assertEqual(pricing.label("claude-opus-5-5[1m]"), "Opus 5.5")
        self.assertEqual(pricing.label("claude-sonnet-5-5|fast"), "Sonnet 5.5")
        self.assertEqual(pricing.label("claude-fable-5-1"), "Fable 5.1")
        self.assertEqual(pricing.label("claude-mythos-5-1"), "Mythos 5.1")
        self.assertEqual(pricing.label("claude-haiku-5-5"), "Haiku 5.5")
        self.assertEqual(pricing.label("claude-opus-5"), "Opus 5")
        self.assertEqual(pricing.label("gpt-6.1-sol"), "GPT-6.1 Sol")
        self.assertEqual(pricing.label("gpt-5.6-terra|long"), "GPT-5.6 Terra")
        self.assertEqual(pricing.label("gpt-5.5"), "GPT-5.5")
        self.assertEqual(pricing.label("gpt-daybreak-blue-latest"), "GPT Daybreak Blue")
        self.assertEqual(pricing.label("mystery-model"), "mystery-model")


if __name__ == "__main__":
    unittest.main()
