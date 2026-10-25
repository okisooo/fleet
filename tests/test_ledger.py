"""The token ledger: log parsing, dedupe, incremental reads, rollups and limit history."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import ledger

NOW = datetime(2026, 7, 15, 12, 30).timestamp()  # local time, whatever the machine's zone
M = 1_000_000
RESET = NOW + 3 * 86400


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def claude(msg, ts, model="claude-opus-5-5", fresh=0, output=0, w5m=0, w1h=0, read=0,
           cwd="C:\\work\\alpha", speed="standard", split=True, request=None):
    usage = {"input_tokens": fresh, "cache_creation_input_tokens": w5m + w1h, "cache_read_input_tokens": read,
             "output_tokens": output, "service_tier": "standard", "speed": speed}
    if split:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": w5m, "ephemeral_1h_input_tokens": w1h}
    return {"parentUuid": None, "isSidechain": False, "type": "assistant", "timestamp": _iso(ts), "cwd": cwd,
            "requestId": request or f"req_{msg}", "uuid": f"uuid-{msg}",
            "message": {"model": model, "id": msg, "type": "message", "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}], "usage": usage}}


def codex_meta(ts, session="s1", cwd="D:\\GitHub\\site"):
    return {"timestamp": _iso(ts), "type": "session_meta", "payload": {"id": session, "cwd": cwd}}


def codex_turn(ts, model):
    return {"timestamp": _iso(ts), "type": "turn_context", "payload": {"model": model, "cwd": "C:\\elsewhere"}}


def codex_tier(ts, tier):
    return {"timestamp": _iso(ts), "type": "event_msg",
            "payload": {"type": "thread_settings_applied", "thread_settings": {"service_tier": tier}}}


def _usage(sent, cached, output, reasoning=0):
    return {"input_tokens": sent, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
            "output_tokens": output, "reasoning_output_tokens": reasoning, "total_tokens": sent + output}


def codex_count(ts, last, total, pct=None, window=10080, reset=RESET):
    limits = {"limit_id": "codex", "primary": None, "secondary": None, "plan_type": "pro"}
    if pct is not None:
        limits["primary"] = {"used_percent": pct, "window_minutes": window, "resets_at": reset}
    info = {"total_token_usage": _usage(*total), "last_token_usage": _usage(*last)} if last else None
    return {"timestamp": _iso(ts), "type": "event_msg",
            "payload": {"type": "token_count", "info": info, "rate_limits": limits}}


def _line(entry):
    return json.dumps(entry, separators=(",", ":")) + "\n"


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self.claude_dir = root / "claude" / "projects"
        self.sessions = root / "codex" / "sessions"
        self.archive = root / "codex" / "archived_sessions"
        self.ledger = ledger.Ledger(root / "ledger.sqlite3", [self.claude_dir], [self.sessions, self.archive])

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, path, entries, mode="w"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, mode, encoding="utf-8", newline="\n") as handle:
            handle.writelines(_line(entry) for entry in entries)
        return path

    def session(self, name="C--work-alpha/one.jsonl"):
        return self.claude_dir / name

    def rollout(self, name="rollout-2026-07-15T10-00-00-s1.jsonl"):
        return self.sessions / "2026" / "07" / "15" / name

    def snap(self, **kwargs):
        self.ledger.update(now=NOW)
        snap = self.ledger.snapshot(now=NOW, **kwargs)
        json.dumps(snap)  # the tray hands it to the panel as JSON
        return snap


class ClaudeLogTests(LedgerCase):
    def test_a_message_logged_in_two_files_counts_once(self):
        self.write(self.session("C--work-alpha/one.jsonl"), [claude("m1", NOW - 600, fresh=10, output=100)])
        self.write(self.session("C--work-alpha/two.jsonl"), [claude("m1", NOW - 600, fresh=10, output=100),
                                                              claude("m2", NOW - 300, fresh=5, output=50)])
        month = self.snap()["claude"]["month"]
        self.assertEqual((month["messages"], month["input"], month["output"]), (2, 15, 150))

    def test_sub_agent_logs_are_found_recursively(self):
        self.write(self.session("C--work-alpha/one.jsonl"), [claude("m1", NOW - 600, output=100)])
        self.write(self.session("C--work-alpha/one/subagents/agent-a1.jsonl"), [claude("m2", NOW - 500, output=40)])
        self.assertEqual(self.snap()["claude"]["month"]["output"], 140)

    def test_cache_writes_are_priced_by_how_long_they_live(self):
        self.write(self.session(), [claude("m1", NOW - 600, w1h=M), claude("m2", NOW - 500, w5m=M),
                                    # No breakdown: a 5-minute write.
                                    claude("m3", NOW - 400, w5m=M, split=False)])
        month = self.snap()["claude"]["month"]
        self.assertAlmostEqual(month["cost"], 8 + 5 + 5)
        self.assertEqual(month["cache_write"], 3 * M)

    def test_fast_mode_costs_double_and_the_1m_suffix_is_the_same_model(self):
        self.write(self.session(), [claude("m1", NOW - 600, model="claude-opus-5-5[1m]", output=M, speed="fast"),
                                    claude("m2", NOW - 500, model="claude-opus-5-5", output=M)])
        provider = self.snap()["claude"]
        self.assertAlmostEqual(provider["month"]["cost"], 40 + 20)
        self.assertEqual([(m["id"], m["name"], m["cost"], m["share"]) for m in provider["models"]],
                         [("claude-opus-5-5", "Opus 5.5", 60, 1.0)])

    def test_unknown_models_are_estimated_by_family_or_left_unpriced(self):
        self.write(self.session(), [claude("m1", NOW - 600, model="claude-opus-9", output=M),
                                    claude("m2", NOW - 500, model="mystery-1", output=M),
                                    claude("m3", NOW - 400, model="<synthetic>", output=M)])
        provider = self.snap()["claude"]
        self.assertAlmostEqual(provider["month"]["cost"], 20)
        self.assertEqual(provider["month"]["output"], 2 * M)
        self.assertTrue(provider["estimated"])
        self.assertEqual(provider["unpriced"], 1)

    def test_a_streamed_message_keeps_its_final_output_count(self):
        path = self.write(self.session(), [claude("m1", NOW - 600, fresh=2, output=5)])
        self.ledger.update(now=NOW)
        self.write(path, [claude("m1", NOW - 598, fresh=2, output=146), claude("m1", NOW - 598, fresh=2, output=146)],
                   mode="a")
        self.write(self.session("C--work-alpha/resumed.jsonl"), [claude("m1", NOW - 598, fresh=2, output=146)])
        month = self.snap()["claude"]["month"]
        self.assertEqual((month["messages"], month["input"], month["output"]), (1, 2, 146))

    def test_a_second_update_reads_only_the_appended_bytes(self):
        path = self.write(self.session(), [claude("m1", NOW - 600, output=100)])
        first = self.ledger.update(now=NOW)
        self.assertEqual((first["files"], first["bytes"], first["events"], first["complete"]),
                         (1, path.stat().st_size, 1, True))
        added = _line(claude("m2", NOW - 300, output=50))
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(added + '{"type":"assistant","partial')  # a line still being written
        second = self.ledger.update(now=NOW)
        self.assertEqual((second["bytes"], second["events"]), (len(added.encode()), 1))
        self.assertEqual(self.ledger.update(now=NOW)["files"], 0)
        self.assertEqual(self.ledger.snapshot(now=NOW)["claude"]["month"]["messages"], 2)

    def test_a_rewritten_file_is_read_again_without_counting_twice(self):
        path = self.write(self.session(), [claude("m1", NOW - 600, output=100), claude("m2", NOW - 500, output=10)])
        os.utime(path, (NOW - 60, NOW - 60))
        self.ledger.update(now=NOW)
        # Changed but no bigger: not an append.
        self.write(path, [claude("m1", NOW - 600, output=100), claude("m3", NOW - 500, output=10)])
        os.utime(path, (NOW - 30, NOW - 30))
        stats = self.ledger.update(now=NOW)
        self.assertEqual((stats["bytes"], stats["events"]), (path.stat().st_size, 1))
        # Bigger, but the bytes before the cursor changed.
        self.write(path, [claude("m1", NOW - 600, output=100), claude("m4", NOW - 400, output=12345)])
        stats = self.ledger.update(now=NOW)
        self.assertEqual((stats["bytes"], stats["events"]), (path.stat().st_size, 1))
        # Shrunk.
        self.write(path, [claude("m1", NOW - 600, output=100)])
        self.assertEqual(self.ledger.update(now=NOW)["events"], 0)
        self.assertEqual(self.ledger.snapshot(now=NOW)["claude"]["month"]["output"], 100 + 10 + 10 + 12345)

    def test_a_touched_file_is_not_mistaken_for_new_lines(self):
        path = self.write(self.session(), [claude("m1", NOW - 600, output=100)])
        self.ledger.update(now=NOW)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(_line(claude("m2", NOW - 300, output=50)))
        self.assertEqual(self.ledger.update(now=NOW)["events"], 1)
        os.utime(path, (NOW - 5, NOW - 5))
        self.assertEqual(self.ledger.update(now=NOW)["events"], 0)
        self.assertEqual(self.ledger.snapshot(now=NOW)["claude"]["month"]["messages"], 2)

    def test_scratch_workspaces_share_one_project_name(self):
        scratch = "C:\\Users\\me\\AppData\\Roaming\\Claude\\scratch-workspaces\\e7\\scratch-2026-07-15-a1"
        self.write(self.session(), [claude("m1", NOW - 600, output=M, cwd=scratch),
                                    claude("m2", NOW - 500, output=M, cwd=scratch.replace("a1", "b2")),
                                    claude("m3", NOW - 400, output=M // 2, cwd="D:\\GitHub\\fleet\\")])
        self.assertEqual(self.snap()["claude"]["projects"],
                         [{"name": "Claude scratch", "cost": 40}, {"name": "fleet", "cost": 10}])


class CodexLogTests(LedgerCase):
    def test_deltas_skip_repeated_counts_and_follow_model_switches(self):
        self.write(self.rollout(), [
            codex_meta(NOW - 900), codex_turn(NOW - 890, "gpt-6.1-sol"),
            codex_count(NOW - 880, (1000, 800, 50, 20), (1000, 800, 50, 20)),
            codex_count(NOW - 870, (1000, 800, 50, 20), (1000, 800, 50, 20)),  # same total: a repeat
            codex_turn(NOW - 860, "gpt-6-luna"),
            codex_count(NOW - 850, (2000, 0, 100, 0), (3000, 800, 150, 20)),
            codex_count(NOW - 840, None, None, pct=12.0),  # rate limits only
        ])
        provider = self.snap()["codex"]
        month = provider["month"]
        self.assertEqual((month["messages"], month["input"], month["cache_read"], month["output"]),
                         (2, 200 + 2000, 800, 150))
        sol = (200 * 2 + 800 * 0.10 + 50 * 10) / M
        luna = (2000 * 0.10 + 100 * 0.50) / M
        self.assertEqual([m["id"] for m in provider["models"]], ["gpt-6.1-sol", "gpt-6-luna"])
        self.assertAlmostEqual(provider["month"]["cost"], round(sol + luna, 4))
        self.assertEqual(provider["projects"][0]["name"], "site")

    def test_a_request_over_272k_input_uses_long_context_rates(self):
        self.write(self.rollout(), [
            codex_meta(NOW - 900), codex_turn(NOW - 890, "gpt-6.1-sol"),
            codex_count(NOW - 880, (272_000, 0, 0), (272_000, 0, 0)),
            codex_count(NOW - 870, (300_000, 0, 0), (572_000, 0, 0)),
        ])
        self.assertAlmostEqual(self.snap()["codex"]["month"]["cost"], 0.272 * 2 + 0.3 * 4)

    def test_the_priority_tier_is_billed_at_priority_rates(self):
        self.write(self.rollout(), [
            codex_meta(NOW - 900), codex_turn(NOW - 890, "gpt-6.1-sol"), codex_tier(NOW - 885, "priority"),
            codex_count(NOW - 880, (0, 0, M), (0, 0, M)), codex_tier(NOW - 875, "default"),
            codex_count(NOW - 870, (0, 0, M), (0, 0, 2 * M)),
        ])
        self.assertAlmostEqual(self.snap()["codex"]["month"]["cost"], 20 + 10)

    def test_the_model_carries_over_to_lines_appended_later(self):
        path = self.write(self.rollout(), [codex_meta(NOW - 900), codex_turn(NOW - 890, "gpt-6-luna"),
                                           codex_count(NOW - 880, (0, 0, M), (0, 0, M))])
        self.ledger.update(now=NOW)
        self.write(path, [codex_count(NOW - 870, (0, 0, M), (0, 0, 2 * M))], mode="a")
        provider = self.snap()["codex"]
        self.assertEqual([(m["id"], m["cost"]) for m in provider["models"]], [("gpt-6-luna", 1.0)])
        self.assertEqual(provider["projects"], [{"name": "site", "cost": 1.0}])

    def test_an_archived_session_is_not_counted_again(self):
        path = self.write(self.rollout(), [codex_meta(NOW - 900), codex_turn(NOW - 890, "gpt-6-sol"),
                                           codex_count(NOW - 880, (M, 0, 0), (M, 0, 0))])
        self.ledger.update(now=NOW)
        self.archive.mkdir(parents=True)
        os.replace(path, self.archive / path.name)
        self.assertEqual(self.snap()["codex"]["month"]["messages"], 1)

    def test_rate_limits_in_the_log_become_limit_samples(self):
        self.write(self.rollout(), [
            codex_meta(NOW - 3600),
            codex_count(NOW - 3000, None, None, pct=10.0),
            codex_count(NOW - 2940, None, None, pct=10.0),  # unchanged within 10 minutes
            codex_count(NOW - 2880, None, None, pct=12.0),
            codex_count(NOW - 2280, None, None, pct=12.0),  # unchanged, but 10 minutes on
            codex_count(NOW - 2200, None, None, pct=40.0, window=300, reset=NOW + 3600),
        ])
        self.ledger.update(now=NOW)
        self.assertEqual(self.ledger.limit_series("codex", "Weekly limit", NOW - 4000, NOW),
                         [(NOW - 3000, 10.0), (NOW - 2880, 12.0), (NOW - 2280, 12.0)])
        self.assertEqual(self.ledger.limit_series("codex", "5-hour limit", NOW - 4000, NOW), [(NOW - 2200, 40.0)])


class RollupTests(LedgerCase):
    def test_today_and_the_daily_bars_split_at_local_midnight(self):
        midnight = datetime(2026, 7, 15).timestamp()
        self.write(self.session(), [claude("m1", midnight - 60, output=100), claude("m2", midnight + 60, output=7)])
        provider = self.snap()["claude"]
        self.assertEqual(provider["today"]["output"], 7)
        self.assertEqual(len(provider["daily"]), 14)
        self.assertEqual([(d["day"], d["tokens"]) for d in provider["daily"][-2:]],
                         [("2026-07-14", 100), ("2026-07-15", 7)])

    def test_week_and_month_are_rolling_windows(self):
        day = 86400
        self.write(self.session(), [claude("m1", NOW - 6.9 * day, output=1), claude("m2", NOW - 7.1 * day, output=10),
                                    claude("m3", NOW - 29 * day, output=100),
                                    claude("m4", NOW - 31 * day, output=1000)])
        provider = self.snap()["claude"]
        self.assertEqual((provider["week"]["output"], provider["month"]["output"]), (1, 111))

    def test_burn_rate_covers_the_last_30_minutes(self):
        self.write(self.session(), [claude("m1", NOW - 40 * 60, output=M), claude("m2", NOW - 20 * 60, output=M)])
        rate = self.snap()["claude"]["rate"]
        self.assertEqual(rate, {"cost_per_hour": 60.0, "tokens_per_min": 50000.0, "minutes": 20.0})

    def test_burn_rate_is_none_when_idle(self):
        self.write(self.session(), [claude("m1", NOW - 3600, output=M)])
        self.assertIsNone(self.snap()["claude"]["rate"])

    def test_spans_total_from_the_callers_start(self):
        self.write(self.session(), [claude("m1", NOW - 3 * 3600, output=1), claude("m2", NOW - 3600, output=10)])
        provider = self.snap(spans={"claude": {"5-hour limit": NOW - 2 * 3600}})["claude"]
        self.assertEqual(provider["spans"]["5-hour limit"]["output"], 10)

    def test_hourly_bars_end_with_the_current_local_hour(self):
        self.write(self.session(), [claude("m1", NOW - 10 * 60, output=M), claude("m2", NOW - 50 * 60, output=M),
                                    claude("m3", NOW - 25 * 3600, output=M)])
        hourly = self.snap()["claude"]["hourly"]
        self.assertEqual(len(hourly), 24)
        self.assertEqual((hourly[-1], hourly[-2], sum(hourly)), (20, 20, 40))

    def test_providers_and_total_add_up(self):
        self.write(self.session(), [claude("m1", NOW - 600, output=M)])
        self.write(self.rollout(), [codex_meta(NOW - 900), codex_turn(NOW - 890, "gpt-6.1-sol"),
                                    codex_count(NOW - 880, (0, 0, M), (0, 0, M))])
        snap = self.snap()
        self.assertEqual([key for key in snap if key in ("claude", "codex")], ["claude", "codex"])
        self.assertAlmostEqual(snap["total"]["today"]["cost"], 20 + 10)
        self.assertTrue(snap["complete"])


class IndexingTests(LedgerCase):
    def test_old_files_are_skipped_and_compressed_logs_are_counted(self):
        old = self.write(self.session(), [claude("m1", NOW - 36 * 86400, output=1)])
        os.utime(old, (NOW - 36 * 86400, NOW - 36 * 86400))
        self.write(self.rollout("rollout-2026-07-15T09-00-00-s0.jsonl.zst"), [])
        stats = self.ledger.update(now=NOW)
        self.assertEqual((stats["files"], stats["skipped_zst"], stats["complete"]), (0, 1, True))
        self.assertNotIn("claude", self.ledger.snapshot(now=NOW))

    def test_lines_longer_than_a_read_chunk_are_reassembled(self):
        self.write(self.session(), [claude(f"m{i}", NOW - 600 + i, output=10 ** i) for i in range(4)])
        self.write(self.rollout(), [codex_meta(NOW - 900), codex_turn(NOW - 890, "gpt-6.1-sol"),
                                    codex_count(NOW - 880, (0, 0, M), (0, 0, M))])
        with mock.patch.object(ledger, "CHUNK", 64):
            snap = self.snap()
        self.assertEqual(snap["claude"]["month"]["output"], 1111)
        self.assertAlmostEqual(snap["codex"]["month"]["cost"], 10)

    def test_a_small_budget_leaves_files_pending_newest_first(self):
        for i, age in enumerate((300, 100, 200)):
            path = self.write(self.session(f"C--work-alpha/{i}.jsonl"), [claude(f"m{i}", NOW - 600, output=1)])
            os.utime(path, (NOW - age, NOW - age))
        stats = self.ledger.update(budget=0, now=NOW)
        self.assertEqual((stats["files"], stats["pending_files"], stats["complete"]), (0, 3, False))
        self.assertFalse(self.ledger.snapshot(now=NOW)["complete"])
        order = []
        with mock.patch.object(ledger.Ledger, "_read", lambda self, db, item, *rest: order.append(item[1]) or True):
            self.ledger.update(now=NOW)
        self.assertEqual([Path(p).name for p in order], ["1.jsonl", "2.jsonl", "0.jsonl"])
        self.assertTrue(self.ledger.update(now=NOW)["complete"])


class LimitHistoryTests(LedgerCase):
    def record(self, pct, ts, reset=RESET, label="5-hour limit"):
        self.ledger.record_limits("claude", [{"label": label, "pct": pct, "reset_at": reset}], ts)

    def test_samples_are_kept_when_they_change_or_every_ten_minutes(self):
        self.record(10, NOW - 3600)
        self.record(10, NOW - 3300)
        self.record(10, NOW - 3000)
        self.record(16, NOW - 1800)
        self.record(22, NOW)
        self.assertEqual(self.ledger.limit_series("claude", "5-hour limit", NOW - 7200, NOW),
                         [(NOW - 3600, 10), (NOW - 3000, 10), (NOW - 1800, 16), (NOW, 22)])
        self.assertAlmostEqual(self.ledger.limit_rate("claude", "5-hour limit", NOW), 12.0)
        self.assertAlmostEqual(self.ledger.limit_rate("claude", "5-hour limit", NOW, minutes=30), 12.0)

    def test_the_rate_only_uses_the_current_window(self):
        self.record(90, NOW - 1800)
        self.record(95, NOW - 600)
        self.record(1, NOW, reset=RESET + 5 * 3600)
        self.assertIsNone(self.ledger.limit_rate("claude", "5-hour limit", NOW))
        self.record(4, NOW + 1200, reset=RESET + 5 * 3600 + 1)  # same window, jittered reset time
        self.assertAlmostEqual(self.ledger.limit_rate("claude", "5-hour limit", NOW + 1200), 9.0)

    def test_too_little_data_has_no_rate(self):
        self.assertIsNone(self.ledger.limit_rate("claude", "Weekly limit", NOW))
        self.record(10, NOW - 120, label="Weekly limit")
        self.record(11, NOW, label="Weekly limit")
        self.assertIsNone(self.ledger.limit_rate("claude", "Weekly limit", NOW))


if __name__ == "__main__":
    unittest.main()
