import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fleet
import panel_wpf
import tray


SAMPLE_AGENTS = [
    {
        "agent": "Claude",
        "plan": "max",
        "err": "",
        "metrics": [
            {"label": "Session", "pct": 42.0, "resets": None, "extra": ""},
            {"label": "Claude Wk", "pct": 71.0, "resets": None, "extra": ""},
        ],
    },
    {
        "agent": "Codex",
        "plan": "pro",
        "err": "",
        "metrics": [{"label": "Session", "pct": 12.0, "resets": None, "extra": ""}],
    },
    {
        "agent": "Grok",
        "plan": "supergrok",
        "err": "",
        "metrics": [{"label": "Daily", "pct": 96.0, "resets": None, "extra": ""}],
    },
    {
        "agent": "Antigrav",
        "plan": "pro",
        "err": "",
        "metrics": [{"label": "Monthly", "pct": 33.0, "resets": None, "extra": ""}],
    },
]


def _varint(value):
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number, value):
    value = value.encode() if isinstance(value, str) else value
    return _varint(number << 3 | 2) + _varint(len(value)) + value


class AntigravityTokenTests(unittest.TestCase):
    def test_extracts_tokens_from_nested_base64_protobuf(self):
        access = "ya29.test-access-token"
        refresh = "1//test-refresh-token"
        token_info = (
            _field(1, access)
            + _field(2, "Bearer")
            + _field(3, refresh)
            + _field(4, b"\x08\x80\x80\x80\x01")
        )
        outer = _field(1, "oauthTokenInfoSentinelKey") + _field(
            2, base64.b64encode(token_info)
        )
        stored = base64.b64encode(outer)

        self.assertEqual(fleet._agy_extract_tokens(stored), (access, refresh))


class PanelStateTests(unittest.TestCase):
    def test_stale_state_renders_without_blocking_collection(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            expected = [{"agent": "Claude", "plan": "pro", "metrics": [], "err": "reauth"}]
            state.write_text(json.dumps({"data": expected, "ts": time.time() - 3600}))

            with mock.patch.object(tray, "STATE", state), mock.patch.object(
                tray.fleet, "collect_api", side_effect=AssertionError("must not collect")
            ):
                data, ts = tray._panel_data()

        self.assertEqual(data, expected)
        self.assertLess(ts, time.time() - 3500)


class PanelDataTests(unittest.TestCase):
    def test_a_torn_state_file_never_triggers_a_network_collect(self):
        # _panel_data runs on the panel host's WPF dispatcher thread. Falling back
        # to collect_api() on a torn read froze the UI for seconds on every click
        # that landed while poll() was rewriting state.json.
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            state.write_text('{"data": [{"agent": "Claude"}], "ts": 100.0}')
            with mock.patch.object(tray, "STATE", state), mock.patch.dict(
                tray._LAST_PANEL_DATA, {"value": None}
            ):
                data, ts = tray._panel_data()
                self.assertEqual(ts, 100.0)

                state.write_text('{"data": [{"agent": "Cla')  # half-written
                with mock.patch.object(
                    tray.fleet, "collect_api", side_effect=AssertionError("must not collect")
                ):
                    torn_data, torn_ts = tray._panel_data()

        self.assertEqual(torn_data, data)
        self.assertEqual(torn_ts, 100.0)

    def test_missing_state_returns_empty_rather_than_collecting(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(tray, "STATE", Path(temp) / "absent.json"), mock.patch.dict(
                tray._LAST_PANEL_DATA, {"value": None}
            ), mock.patch.object(
                tray.fleet, "collect_api", side_effect=AssertionError("must not collect")
            ):
                data, ts = tray._panel_data()

        self.assertEqual(data, [])
        self.assertIsInstance(ts, float)

    def test_state_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            with mock.patch.object(tray, "STATE", state), mock.patch.object(
                tray.fleet, "collect_api", return_value=[{"agent": "Codex"}]
            ):
                tray.poll()

            self.assertEqual(json.loads(state.read_text())["data"], [{"agent": "Codex"}])
            # The staging file must not survive the replace.
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_failed_poll_keeps_the_last_state(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            expected = [{"agent": "Codex", "metrics": [{"pct": 20}]}]
            state.write_text(json.dumps({"data": expected, "ts": 100.0}))
            with mock.patch.object(tray, "STATE", state), mock.patch.object(
                tray.fleet, "collect_api", side_effect=RuntimeError("offline")
            ):
                actual = tray.poll()

        self.assertEqual(actual, expected)


class ProviderCacheTests(unittest.TestCase):
    def test_fallback_carries_the_real_data_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            (cache / "claude_last.json").write_text(
                json.dumps({"ts": 100.0, "plan": "max", "metrics": [{"pct": 42}]})
            )
            with mock.patch.object(fleet, "CACHE", cache), mock.patch.object(
                fleet.time, "time", return_value=400.0
            ):
                result = fleet._cached_or(
                    {"agent": "Claude", "plan": "", "metrics": [], "err": "offline"},
                    "claude",
                )

        self.assertEqual(result["cached_at"], 100.0)


class ClaudeUsageTests(unittest.TestCase):
    def test_unified_headers_become_session_and_weekly_metrics(self):
        metrics = fleet._claude_metrics_from_headers(
            {
                "Anthropic-Ratelimit-Unified-5H-Utilization": "0.42",
                "anthropic-ratelimit-unified-5h-reset": "2000",
                "anthropic-ratelimit-unified-7d-utilization": "0.71",
                "anthropic-ratelimit-unified-7d-reset": "3000",
            }
        )

        self.assertEqual(
            metrics,
            [
                fleet.M("Session", 42.0, 2000.0),
                fleet.M("Weekly", 71.0, 3000.0),
            ],
        )

    def test_messages_headers_rescue_a_throttled_usage_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cred = root / ".claude" / ".credentials.json"
            cred.parent.mkdir()
            cred.write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "access",
                            "refreshToken": "refresh",
                            "expiresAt": (time.time() + 3600) * 1000,
                            "subscriptionType": "max",
                        }
                    }
                )
            )
            response_headers = {
                "Retry-After": "120",
                "anthropic-ratelimit-unified-5h-utilization": "0.25",
                "anthropic-ratelimit-unified-5h-reset": "2000",
                "anthropic-ratelimit-unified-7d-utilization": "0.5",
                "anthropic-ratelimit-unified-7d-reset": "3000",
            }
            with mock.patch.object(fleet, "HOME", root), mock.patch.object(
                fleet, "CACHE", root
            ), mock.patch.object(
                fleet, "_claude_user_agent", return_value="claude-code/test"
            ), mock.patch.object(
                fleet, "_jget", return_value=(429, {"Retry-After": "120"}, {})
            ), mock.patch.object(
                fleet, "_http", return_value=(429, response_headers, b"")
            ):
                result = fleet.api_claude()

            self.assertEqual([m["pct"] for m in result["metrics"]], [25.0, 50.0])
            self.assertEqual(result["err"], "")
            self.assertTrue((root / "claude_usage_backoff").exists())

    def test_force_bypasses_an_auth_refresh_backoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cred = root / ".claude" / ".credentials.json"
            cred.parent.mkdir()
            cred.write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "expired",
                            "refreshToken": "refresh",
                            "expiresAt": (time.time() - 60) * 1000,
                        }
                    }
                )
            )
            (root / "claude_refresh_backoff").write_text(str(time.time() + 300))
            with mock.patch.object(fleet, "HOME", root), mock.patch.object(
                fleet, "CACHE", root
            ), mock.patch.object(
                fleet, "_refresh_claude_writeback", return_value=False
            ) as refresh:
                fleet.api_claude(force=True)

            refresh.assert_called_once()

    def test_cli_can_refresh_its_own_credentials_without_exposing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            cred = Path(temp) / ".credentials.json"
            cred.write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "expired",
                            "expiresAt": (time.time() - 60) * 1000,
                        }
                    }
                )
            )

            def refresh_credentials(*args, **kwargs):
                cred.write_text(
                    json.dumps(
                        {
                            "claudeAiOauth": {
                                "accessToken": "fresh",
                                "expiresAt": (time.time() + 3600) * 1000,
                            }
                        }
                    )
                )
                return SimpleNamespace(returncode=0)

            with mock.patch.object(
                fleet.subprocess, "run", side_effect=refresh_credentials
            ) as run:
                self.assertTrue(fleet._refresh_claude_via_cli(cred))

            self.assertIs(run.call_args.kwargs["stdout"], fleet.subprocess.PIPE)
            self.assertIs(run.call_args.kwargs["stderr"], fleet.subprocess.PIPE)


class PublicReleaseTests(unittest.TestCase):
    def test_antigravity_uses_existing_access_token_without_a_copied_client(self):
        with mock.patch.object(fleet, "cache_get", return_value=None), mock.patch.object(
            fleet, "_agy_stored_tokens", return_value=("access", "refresh")
        ):
            self.assertEqual(fleet._agy_token(), "access")

        self.assertFalse(hasattr(fleet, "_AGY_CLIENT_SECRET"))
        self.assertFalse(hasattr(fleet, "_refresh_agy_token"))

    def test_refresh_debug_never_includes_response_bodies(self):
        source = Path(fleet.__file__).read_text(encoding="utf-8")

        self.assertNotIn('dbg("claude refresh http", st, bd', source)
        self.assertNotIn('dbg("grok refresh http", st, bd', source)


class TrayAnchorTests(unittest.TestCase):
    def test_show_signal_carries_the_notification_icon_rectangle(self):
        with tempfile.TemporaryDirectory() as temp:
            signal = Path(temp) / "panel.show"
            with mock.patch.object(tray, "PANEL_SIGNAL", signal), mock.patch.object(
                tray,
                "_tray_icon_rect",
                return_value=(1581, 1032, 1613, 1080),
            ):
                tray._write_panel_signal(object())

            payload = json.loads(signal.read_text())

        self.assertEqual(payload["anchor"], [1581, 1032, 1613, 1080])
        self.assertIsInstance(payload["ts"], int)


class TrayIconTests(unittest.TestCase):
    def test_cycle_uses_each_providers_worst_cached_usage(self):
        data = [
            {"agent": "Claude", "metrics": [{"pct": 24}, {"pct": 71}]},
            {"agent": "Codex", "metrics": [{"pct": None}]},
        ]

        self.assertEqual(tray.usage_cycle(data), [("Claude", 71), ("Codex", None)])

    def test_provider_mark_changes_the_icon(self):
        claude = tray.make_icon_image("Claude", 50)
        codex = tray.make_icon_image("Codex", 50)

        self.assertEqual(claude.size, (64, 64))
        self.assertNotEqual(claude.tobytes(), codex.tobytes())

    def test_refresh_now_bypasses_claude_backoff(self):
        icon = object()
        with mock.patch.object(
            tray, "poll", return_value=SAMPLE_AGENTS
        ) as poll, mock.patch.object(tray, "apply") as apply:
            tray.refresh(icon)

        poll.assert_called_once_with(block=False, force_claude=True)
        apply.assert_called_once_with(icon)


class SingleInstanceTests(unittest.TestCase):
    def test_a_second_instance_is_refused_while_the_first_holds_the_name(self):
        name = r"Local\fleet-test-" + str(time.time_ns())

        self.assertTrue(tray.claim_single_instance(name))
        self.assertFalse(tray.claim_single_instance(name))

    def test_unrelated_names_do_not_block_each_other(self):
        stamp = str(time.time_ns())

        self.assertTrue(tray.claim_single_instance(r"Local\fleet-test-a-" + stamp))
        self.assertTrue(tray.claim_single_instance(r"Local\fleet-test-b-" + stamp))


class CodexWindowTests(unittest.TestCase):
    def test_labels_windows_from_actual_duration(self):
        self.assertEqual(fleet._window_label({"limit_window_seconds": 18_000}), "Session")
        self.assertEqual(fleet._window_label({"limit_window_seconds": 604_800}), "Weekly")


class PanelDismissalTests(unittest.TestCase):
    def test_click_inside_panel_is_not_outside(self):
        self.assertFalse(
            panel_wpf._point_outside((100, 100, 500, 600), (300, 350))
        )

    def test_click_beyond_each_panel_edge_is_outside(self):
        rect = (100, 100, 500, 600)
        for point in ((99, 350), (500, 350), (300, 99), (300, 600)):
            with self.subTest(point=point):
                self.assertTrue(panel_wpf._point_outside(rect, point))

    def test_tray_click_is_reserved_for_toggle(self):
        panel = (100, 100, 500, 600)
        tray_icon = (700, 900, 732, 932)

        self.assertFalse(panel_wpf._should_dismiss(panel, (716, 916), tray_icon))
        self.assertTrue(panel_wpf._should_dismiss(panel, (600, 700), tray_icon))


class PanelLayoutTests(unittest.TestCase):
    def test_bottom_right_position_preserves_shadow_clearance(self):
        left, top = panel_wpf._panel_origin(
            work_rect_px=(0, 0, 1920, 1040),
            cursor_px=(1900, 1000),
            window_size_dip=(360, 440),
            dpi_scale=(1.0, 1.0),
        )

        self.assertEqual((left, top), (1548.0, 588.0))

    def test_position_converts_monitor_pixels_to_wpf_dips(self):
        left, top = panel_wpf._panel_origin(
            work_rect_px=(0, 0, 2560, 1400),
            cursor_px=(2500, 1300),
            window_size_dip=(360, 440),
            dpi_scale=(1.5, 1.5),
        )

        self.assertAlmostEqual(left, 1334.6666666667)
        self.assertAlmostEqual(top, 481.3333333333)

    def test_panel_anchors_above_the_actual_notification_icon(self):
        left, top = panel_wpf._anchored_panel_origin(
            work_rect_px=(0, 0, 1920, 1032),
            anchor_rect_px=(1872, 1032, 1896, 1080),
            window_size_dip=(360, 438),
            dpi_scale=(1.0, 1.0),
        )

        self.assertEqual((left, top), (1550.0, 584.0))

    def test_panel_handles_a_left_docked_taskbar(self):
        left, top = panel_wpf._anchored_panel_origin(
            work_rect_px=(48, 0, 1920, 1080),
            anchor_rect_px=(0, 500, 48, 524),
            window_size_dip=(360, 438),
            dpi_scale=(1.0, 1.0),
        )

        self.assertEqual((left, top), (58.0, 293.0))


class PanelSurfaceTests(unittest.TestCase):
    def _markup(self, light=True):
        return panel_wpf._xaml(
            SAMPLE_AGENTS,
            time.time(),
            light,
            SimpleNamespace(_until=lambda _: "3h"),
        )

    def test_every_surface_colour_is_fully_opaque(self):
        # Windows build 26xxx no longer honours ACCENT_ENABLE_ACRYLICBLURBEHIND:
        # the call succeeds but leaves the client area undefined, and with
        # AllowsTransparency="False" undefined composites as black. Nothing here
        # may depend on transparency again.
        for light in (True, False):
            for token, value in panel_wpf._theme(light).items():
                with self.subTest(light=light, token=token):
                    self.assertRegex(value, r"^#FF[0-9A-Fa-f]{6}$")

    def test_the_window_never_asks_for_transparency(self):
        markup = self._markup()

        self.assertIn('AllowsTransparency="False"', markup)
        self.assertNotIn('Background="Transparent"', markup)
        for dead in ("BackdropHost", "BackdropBlur", "ImageBrush", "BlurEffect"):
            with self.subTest(element=dead):
                self.assertNotIn(dead, markup)

    def test_the_panel_stays_compact(self):
        # It was 360x436 DIP with a header row, icon tile and pill per provider.
        self.assertLessEqual(panel_wpf.PANEL_WIDTH, 280)
        self.assertIn(f'Width="{panel_wpf.PANEL_WIDTH:.0f}"', self._markup())

    def test_percentage_rows_show_their_reset_without_gaining_height(self):
        data = [
            {
                "agent": "Claude",
                "metrics": [{"label": "Session", "pct": 42, "resets": 123}],
                "err": "",
            }
        ]
        markup = panel_wpf._xaml(
            data,
            time.time(),
            True,
            SimpleNamespace(_until=lambda _: "3h"),
        )

        self.assertIn('Text="3h"', markup)
        self.assertEqual(markup.count('<Grid Height="19">'), 1)

    def test_panel_uses_bahnschrift(self):
        self.assertIn('FontFamily="Bahnschrift"', self._markup())

    def test_mixed_freshness_shows_real_cache_age(self):
        data = [
            {
                "agent": "Claude",
                "metrics": [{"label": "Session", "pct": 42, "resets": None}],
                "err": "offline",
                "cached_at": 100.0,
            },
            {
                "agent": "Codex",
                "metrics": [{"label": "Session", "pct": 12, "resets": None}],
                "err": "",
            },
        ]
        markup = panel_wpf._xaml(
            data,
            999.0,
            True,
            SimpleNamespace(_until=lambda _: "", _age=lambda _: "8m"),
        )

        self.assertIn("1/2 live · 8m old", markup)

    def test_each_provider_icon_identifies_its_own_freshness(self):
        data = [
            {
                "agent": "Claude",
                "metrics": [{"label": "Session", "pct": 42, "resets": None}],
                "err": "",
            },
            {
                "agent": "Codex",
                "metrics": [{"label": "Session", "pct": 12, "resets": None}],
                "err": "offline",
                "cached_at": 100.0,
            },
            {"agent": "Grok", "metrics": [], "err": "offline"},
        ]
        markup = panel_wpf._xaml(
            data,
            999.0,
            True,
            SimpleNamespace(_until=lambda _: "", _age=lambda _: "8m"),
        )

        self.assertIn('ToolTip="live"', markup)
        self.assertIn('ToolTip="cached · 8m old"', markup)
        self.assertIn('ToolTip="unavailable · offline"', markup)

    def test_panel_uses_its_own_provider_monograms(self):
        markup = self._markup()

        self.assertNotIn("<Image", markup)
        self.assertIn('Text="C"', markup)
        self.assertIn('Text="X"', markup)

    def test_each_metric_occupies_exactly_one_fixed_height_row(self):
        # Height is now predictable: rows * 19 plus chrome. A provider no longer
        # costs a header row of its own; its name rides on its first metric.
        markup = self._markup()
        rows = markup.count('<Grid Height="19">')
        metrics = sum(len(agent["metrics"]) for agent in SAMPLE_AGENTS)

        self.assertEqual(rows, metrics)

    def test_columns_add_up_to_the_content_width(self):
        # Over-allocating left 12 px for the percentage, so "88%" rendered as "88"
        # jammed against the bar.
        total = (
            panel_wpf.IDENTITY_COLUMN
            + panel_wpf.LABEL_COLUMN
            + panel_wpf.BAR_COLUMN
            + panel_wpf.VALUE_COLUMN
        )

        self.assertEqual(total, panel_wpf.CONTENT_WIDTH)
        self.assertEqual(
            panel_wpf.CONTENT_WIDTH,
            panel_wpf.PANEL_WIDTH - 2 * panel_wpf.PANEL_PADDING - 2 * panel_wpf.PANEL_BORDER,
        )
        self.assertGreaterEqual(panel_wpf.VALUE_COLUMN, 56.0)

    def test_a_bar_never_exceeds_its_track_and_zero_stays_empty(self):
        for pct in (0, 1, 41, 88, 100):
            with self.subTest(pct=pct):
                self.assertLessEqual(panel_wpf._bar_fill(pct), panel_wpf.BAR_TRACK)
        self.assertEqual(panel_wpf._bar_fill(0), 0.0)
        self.assertEqual(panel_wpf._bar_fill(100), panel_wpf.BAR_TRACK)

    def test_both_row_kinds_share_one_column_layout(self):
        # The "$33.75" / "off" rows used a two-column grid, so their values landed
        # somewhere no other row's value did.
        markup = self._markup()
        grids = markup.count(f'<ColumnDefinition Width="{panel_wpf.BAR_COLUMN:.0f}"/>')

        self.assertEqual(grids, markup.count(f'<Grid Height="{panel_wpf.ROW_HEIGHT:.0f}">'))

    def test_the_provider_name_appears_once_per_provider(self):
        markup = self._markup()

        for agent in SAMPLE_AGENTS:
            name = "Antigravity" if agent["agent"] == "Antigrav" else agent["agent"]
            with self.subTest(agent=name):
                self.assertEqual(markup.count(f'Text="{name}"'), 1)


class PanelSilhouetteTests(unittest.TestCase):
    def test_every_silhouette_layer_uses_the_same_corner_radius(self):
        # DWM rounds the window to PANEL_CORNER_RADIUS. Drawing the rim or the
        # clip at a different radius puts several curves on screen at once, which
        # is what made the corners look mislayered and then black.
        markup = panel_wpf._xaml(
            [],
            time.time(),
            True,
            SimpleNamespace(_until=lambda _: ""),
        )
        radius = f'CornerRadius="{panel_wpf.PANEL_CORNER_RADIUS:.0f}"'

        self.assertIn(f'x:Name="GlassClip" {radius}', markup)
        # One rounded layer only. The decorative rim, inner stroke and highlight
        # each drew their own arc, which is how three curves ended up on screen.
        self.assertEqual(markup.count("CornerRadius="), 1)


class PanelMaterialTests(unittest.TestCase):
    def test_nothing_in_the_panel_reports_a_size_from_a_bitmap(self):
        # History: the backdrop was an <Image>, which reports a desired size. Under
        # SizeToContent="Height" each captured frame fed its own height back into
        # the window, growing it 421 -> 2540 DIP. No element above the content may
        # size itself from an image again.
        markup = panel_wpf._xaml(
            [],
            time.time(),
            True,
            SimpleNamespace(_until=lambda _: ""),
        )

        self.assertNotIn("<Image ", markup.split("ContentRoot")[0])
        self.assertIn('SizeToContent="Height"', markup)

    def test_hidden_host_stays_invisible_until_the_backdrop_is_ready(self):
        markup = panel_wpf._xaml(
            [],
            time.time(),
            True,
            SimpleNamespace(_until=lambda _: ""),
        )

        self.assertIn('Topmost="True" Opacity="0"', markup)
        self.assertIn('<StackPanel x:Name="ContentRoot" Opacity="1">', markup)

    def test_prepared_window_is_final_before_show(self):
        calls = []
        window = SimpleNamespace()
        window.Opacity = 0.0
        window.UpdateLayout = lambda: calls.append("layout")
        window.Show = lambda: calls.append(("show", window.Opacity))
        window.Activate = lambda: calls.append("activate")

        panel_wpf._present_prepared_window(
            window,
            (1, 2, 3, 4),
            lambda anchor: calls.append(("position", anchor)),
            lambda: calls.append("slide"),
        )

        self.assertEqual(
            calls,
            [
                ("position", (1, 2, 3, 4)),
                "layout",
                "slide",
                ("show", 1.0),
                "activate",
            ],
        )


if __name__ == "__main__":
    unittest.main()
