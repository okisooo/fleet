"""The tray icon, its tooltip, alerts and settings."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import alerts
import panel_model
import settings as prefs
import tray
import tray_glyph


def _metric(label, pct=None, resets=None, extra=""):
    return {"label": label, "pct": pct, "resets": resets, "extra": extra}


def _model(now=1000.0, session=41, weekly=78, session_left=3 * 3600, weekly_left=34 * 3600, **agent):
    claude = {"agent": "Claude", "plan": "max", "err": "", "metrics": [
        _metric("Session", session, now + session_left), _metric("Weekly", weekly, now + weekly_left)]}
    claude.update(agent)
    data = [claude, {"agent": "Codex", "err": "", "metrics": [_metric("Weekly", 16, now + 5 * 86400)]},
            {"agent": "OpenRouter", "api_id": "openrouter", "compact": True, "err": "",
             "metrics": [_metric("Balance", None, None, "$4.75")]}]
    return panel_model.build(data, now, now=now)


def _lit(frame, row, light_alpha=200):
    """Columns of one pixel row that are (nearly) fully drawn."""
    return [x for x in range(frame.width) if frame.getpixel((x, row))[3] >= light_alpha]


class GlyphTests(unittest.TestCase):
    def test_the_icon_follows_the_provider_that_presses_hardest(self):
        glyph = tray_glyph.spec(_model(), dict(prefs.DEFAULTS))

        self.assertEqual((glyph["provider"], glyph["top"], glyph["bottom"]), ("Claude", 41.0, 78.0))
        self.assertEqual(glyph["value"], 78.0)

    def test_a_limit_running_out_early_turns_its_bar_amber(self):
        # 30% used 20 minutes into the 5-hour window: empty well before the reset.
        glyph = tray_glyph.spec(_model(session=30, session_left=4 * 3600 + 40 * 60), dict(prefs.DEFAULTS))

        self.assertEqual((glyph["top_tone"], glyph["bottom_tone"], glyph["tone"]), ("warn", "calm", "warn"))

    def test_bars_fill_to_the_percentage_on_whole_pixels_at_16px(self):
        frame = tray_glyph.draw({"style": "bars", "top": 50, "top_tone": "calm",
                                 "bottom": 100, "bottom_tone": "calm"}, 16)

        self.assertEqual(frame.size, (16, 16))
        self.assertEqual(_lit(frame, 5), list(range(1, 8)))       # top bar: half of 14 px
        self.assertEqual(_lit(frame, 11), list(range(1, 15)))     # bottom bar: full
        self.assertEqual(_lit(frame, 8), [])                      # the gap stays clear

    def test_dark_and_light_taskbars_get_white_and_near_black_glyphs(self):
        dark = tray_glyph.draw({"style": "bars", "top": 100, "top_tone": "calm", "bottom": 100, "bottom_tone": "calm"}, 16)
        light = tray_glyph.draw({"style": "bars", "top": 100, "top_tone": "calm", "bottom": 100, "bottom_tone": "calm"}, 16, light=True)

        self.assertGreater(min(dark.getpixel((8, 5))[:3]), 240)
        self.assertLess(max(light.getpixel((8, 5))[:3]), 40)

    def test_warning_colour_marks_only_the_limit_in_trouble(self):
        frame = tray_glyph.draw({"style": "bars", "top": 100, "top_tone": "warn", "bottom": 100, "bottom_tone": "calm"}, 16)

        r, g, b, _ = frame.getpixel((8, 5))
        self.assertGreater(r, 200)
        self.assertLess(b, 80)
        self.assertGreater(min(frame.getpixel((8, 11))[:3]), 240)

    def test_stale_data_dims_the_icon(self):
        fresh = tray_glyph.draw({"style": "ring", "value": 60, "tone": "calm"}, 24)
        stale = tray_glyph.draw({"style": "ring", "value": 60, "tone": "calm", "stale": True}, 24)

        self.assertLess(max(stale.getchannel("A").getdata()), max(fresh.getchannel("A").getdata()) * 0.6)

    def test_every_size_is_drawn_at_its_own_pixels(self):
        with tempfile.TemporaryDirectory() as temp:
            path = tray_glyph.write_ico({"style": "number", "value": 78, "tone": "calm"}, Path(temp) / "i.ico")
            with Image.open(path) as ico:
                sizes = sorted(ico.info["sizes"])

        self.assertEqual(sizes, [(s, s) for s in tray_glyph.FRAME_SIZES])

    def test_left_mode_shows_what_remains(self):
        glyph = tray_glyph.spec(_model(), dict(prefs.DEFAULTS, units="left"))

        self.assertEqual((glyph["top"], glyph["bottom"], glyph["value"]), (59.0, 22.0, 22.0))

    def test_no_meter_without_usage_data(self):
        model = panel_model.build([{"agent": "OpenRouter", "api_id": "openrouter", "compact": True, "err": "",
                                    "metrics": [_metric("Balance", None, None, "$4.75")]}], 1000.0, now=1000.0)

        self.assertTrue(tray_glyph.spec(model, dict(prefs.DEFAULTS))["empty"])

    def test_the_tooltip_lists_every_provider_within_the_windows_limit(self):
        text = tray_glyph.tooltip(_model(), dict(prefs.DEFAULTS))

        self.assertEqual(text.splitlines(), ["Claude  41% 5-hour · 78% weekly", "Codex  16% weekly", "OpenRouter  $4.75"])
        self.assertLessEqual(len(tray_glyph.tooltip({"providers": [_model()["providers"][0]] * 20}, {})), 127)


class FakeIcon:
    def __init__(self):
        self.title = ""
        self.fleet_glyph = None
        self.icons = 0
        self.notes = []

    @property
    def icon(self):
        return None

    @icon.setter
    def icon(self, value):
        self.icons += 1

    def notify(self, body, title):
        self.notes.append((title, body))


class TrayApplyTests(unittest.TestCase):
    def test_the_icon_is_redrawn_only_when_what_it_shows_changes(self):
        icon = FakeIcon()
        with mock.patch.object(tray, "_MODEL", _model()), mock.patch.object(tray, "ensure_tray_registration"), \
                mock.patch.object(tray_glyph, "taskbar_is_light", return_value=False), \
                mock.patch.object(tray_glyph, "small_icon_size", return_value=20), \
                mock.patch.object(prefs, "load", return_value=dict(prefs.DEFAULTS)), \
                mock.patch.object(tray, "_settings_changed", return_value=False):
            tray.apply(icon)
            tray.apply(icon)
            self.assertEqual(icon.icons, 1)
            with mock.patch.object(tray, "_MODEL", _model(session=60)):
                tray.apply(icon)
        self.assertEqual(icon.icons, 2)
        self.assertIn("Claude", icon.title)

    def test_pending_alerts_are_shown_once(self):
        icon = FakeIcon()
        with mock.patch.object(tray, "_MODEL", _model()), mock.patch.object(tray, "ensure_tray_registration"), \
                mock.patch.object(tray, "_PENDING_ALERTS", [("Claude weekly limit at 80%", "80% used.")]), \
                mock.patch.object(tray_glyph, "taskbar_is_light", return_value=False), \
                mock.patch.object(tray_glyph, "small_icon_size", return_value=16), \
                mock.patch.object(tray, "_settings_changed", return_value=False):
            tray.apply(icon)
            tray.apply(icon)
        self.assertEqual(icon.notes, [("Claude weekly limit at 80%", "80% used.")])

    def test_the_menu_offers_settings_and_a_mute(self):
        labels = [item.text for item in tray.build_menu().items if hasattr(item, "text")]

        self.assertEqual(labels[:4], ["Open Fleet", "Refresh now", "Mute alerts for today", "Settings"])


class AlertTests(unittest.TestCase):
    def run_alerts(self, model, state=None, now=1000.0, **current):
        settings = dict(prefs.DEFAULTS)
        settings.update(current)
        return alerts.evaluate(model, settings, state or {}, now=now)

    def test_a_threshold_alerts_once_per_window(self):
        # 82% with ten hours of the week left: high, but it lasts until the reset.
        found, state = self.run_alerts(_model(weekly=82, weekly_left=10 * 3600))
        again, state = self.run_alerts(_model(weekly=84, weekly_left=10 * 3600), state)

        self.assertEqual([title for title, _ in found], ["Claude weekly limit at 80%"])
        self.assertEqual(again, [])

    def test_crossing_two_thresholds_at_once_sends_only_the_higher(self):
        found, _ = self.run_alerts(_model(weekly=97, weekly_left=3600))

        self.assertEqual([title for title, _ in found], ["Claude weekly limit at 95%"])

    def test_a_forecast_run_out_alerts_once(self):
        model = _model(session=30, session_left=4 * 3600 + 40 * 60)
        found, state = self.run_alerts(model)
        again, _ = self.run_alerts(model, state)

        self.assertEqual(len([t for t, _ in found if "runs out around" in t]), 1)
        self.assertEqual(again, [])

    def test_a_high_limit_announces_its_reset(self):
        _, state = self.run_alerts(_model(weekly=90, weekly_left=60))
        later = 1000.0 + 120
        found, _ = self.run_alerts(_model(now=later, weekly=0, weekly_left=7 * 86400), state, now=later)

        self.assertIn("Claude weekly limit has reset", [title for title, _ in found])

    def test_mute_and_off_keep_quiet_but_still_count_the_alert(self):
        muted, state = self.run_alerts(_model(weekly=85), mute_until=5000.0)
        after, _ = self.run_alerts(_model(weekly=85), state, now=6000.0)
        off, _ = self.run_alerts(_model(weekly=85), alerts=False)

        self.assertEqual((muted, after, off), ([], [], []))

    def test_old_readings_never_alert(self):
        model = _model(weekly=99, err="cached 3h ago · http 503", cached_at=10.0)

        self.assertEqual(self.run_alerts(model)[0], [])


class SettingsTests(unittest.TestCase):
    def test_defaults_survive_a_missing_or_broken_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            self.assertEqual(prefs.load(path), prefs.DEFAULTS)
            path.write_text("{not json")
            self.assertEqual(prefs.load(path), prefs.DEFAULTS)
            path.write_text(json.dumps({"units": "sideways", "tray_icon": "ring", "hidden": ["grok", 3]}))
            loaded = prefs.load(path)

        self.assertEqual((loaded["units"], loaded["tray_icon"], loaded["hidden"]), ("used", "ring", ["grok"]))

    def test_save_merges_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            prefs.save({"tray_icon": "number"}, path)
            prefs.save({"units": "left"}, path)
            loaded = prefs.load(path)

        self.assertEqual((loaded["tray_icon"], loaded["units"]), ("number", "left"))

    def test_mute_for_today_lasts_until_local_midnight(self):
        now = time.mktime((2026, 10, 14, 21, 30, 0, 0, 0, -1))
        until = prefs.end_of_today(now)

        self.assertEqual(time.localtime(until)[:5], (2026, 10, 15, 0, 0))
        self.assertTrue(prefs.muted({"mute_until": until}, now))


if __name__ == "__main__":
    unittest.main()
