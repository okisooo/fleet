import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tray


class NativePanelTests(unittest.TestCase):
    def test_the_native_panel_is_preferred_when_it_is_beside_the_tray(self):
        with tempfile.TemporaryDirectory() as temp:
            exe = Path(temp) / "FleetPanel.exe"
            exe.write_bytes(b"")
            with mock.patch.object(tray, "_native_panel", return_value=exe):
                command = tray._panel_host_command()

        self.assertEqual(command, [str(exe), "--cache", str(tray.CACHE)])

    def test_the_python_host_remains_the_fallback(self):
        with mock.patch.object(tray, "_native_panel", return_value=None):
            command = tray._panel_host_command()

        self.assertIn("--panel-host", command)

    def test_fleet_panel_python_forces_the_old_host(self):
        with mock.patch.dict(os.environ, {"FLEET_PANEL": "python"}):
            self.assertIsNone(tray._native_panel())

    def test_every_poll_publishes_the_panel_model_beside_the_state(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            data = [{"agent": "Codex", "err": "", "metrics": [{"label": "Weekly", "pct": 20, "resets": None}]}]
            with mock.patch.object(tray, "STATE", state), mock.patch.object(
                tray.fleet, "collect_api", return_value=data
            ):
                tray.poll()

            model = json.loads((Path(temp) / "panel.json").read_text())

        self.assertEqual(model["providers"][0]["name"], "Codex")
        self.assertEqual(model["providers"][0]["groups"][0]["rows"][0]["value"], "20%")

    def test_an_open_panel_speeds_up_polling(self):
        with tempfile.TemporaryDirectory() as temp:
            visible = Path(temp) / "panel.visible"
            with mock.patch.object(tray, "PANEL_VISIBLE", visible):
                self.assertFalse(tray._panel_visible())
                visible.write_text("1")
                self.assertTrue(tray._panel_visible())
                old = time.time() - tray.PANEL_VISIBLE_SECS - 5
                os.utime(visible, (old, old))
                self.assertFalse(tray._panel_visible())
        self.assertLess(tray.OPEN_REFRESH_SECS, tray.REFRESH_SECS)
        self.assertLessEqual(tray.REFRESH_SECS, 30)


class InstantClickTests(unittest.TestCase):
    def test_dashboard_menu_uses_its_own_native_message(self):
        host = mock.Mock(pid=777, poll=mock.Mock(return_value=None))
        with mock.patch.object(tray, "_PANEL_PROCESS", host), mock.patch.object(
            tray, "_allow_panel_foreground"
        ), mock.patch.object(tray, "_post_panel", return_value=True) as post:
            tray.open_panel(mock.Mock(_hwnd=99), page="dashboard")
        self.assertEqual(post.call_args.args[:2], (tray.WM_FLEET_DASHBOARD, 99))

    def test_a_cold_dashboard_launch_leaves_a_request_for_the_native_host(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(tray, "CACHE", Path(temp)), mock.patch.object(
                tray, "_PANEL_PROCESS", None
            ), mock.patch.object(tray, "ensure_panel_host") as ensure:
                tray.open_panel(page="dashboard")
                self.assertTrue((Path(temp) / "dashboard.show").is_file())
            ensure.assert_called_once_with()

    def _host(self, temp, hwnd=4242, pid=777):
        heartbeat = Path(temp) / "panel-heartbeat.json"
        heartbeat.write_text(json.dumps({"pid": pid, "hwnd": hwnd, "ts": time.time()}))
        return heartbeat, mock.Mock(pid=pid, poll=mock.Mock(return_value=None))

    def test_a_click_is_one_window_message_not_a_file(self):
        with tempfile.TemporaryDirectory() as temp:
            heartbeat, host = self._host(temp)
            posted = []
            with mock.patch.object(tray, "_PANEL_PROCESS", host), mock.patch.object(
                tray, "PANEL_HEARTBEAT", heartbeat
            ), mock.patch.object(tray, "_panel_window", return_value=4242), mock.patch.object(
                tray, "_allow_panel_foreground"
            ) as allow, mock.patch.object(
                tray, "_post_panel", side_effect=lambda message, w=0, l=0: posted.append((message, w)) or True
            ), mock.patch.object(tray, "ensure_panel_host", side_effect=AssertionError("file fallback used")):
                tray.open_panel(mock.Mock(_hwnd=99))

        allow.assert_called_once_with(777)
        self.assertEqual(posted, [(tray.WM_FLEET_TOGGLE, 99)])

    def test_without_a_native_window_the_file_signal_still_works(self):
        with mock.patch.object(tray, "_PANEL_PROCESS", None), mock.patch.object(
            tray, "ensure_panel_host"
        ) as ensure:
            tray.open_panel("icon")

        ensure.assert_called_once_with(show=True, icon="icon")

    def test_a_heartbeat_from_another_process_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as temp:
            heartbeat, host = self._host(temp, pid=111)
            host.pid = 222
            with mock.patch.object(tray, "_PANEL_PROCESS", host), mock.patch.object(
                tray, "PANEL_HEARTBEAT", heartbeat
            ):
                self.assertIsNone(tray._panel_window())

    def test_new_data_is_announced_to_the_panel(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            with mock.patch.object(tray, "STATE", state), mock.patch.object(
                tray, "_post_panel"
            ) as post:
                tray._write_panel_model([{"agent": "Codex", "err": "", "metrics": []}], time.time())

        post.assert_called_once_with(tray.WM_FLEET_DATA)


class RefreshSignalTests(unittest.TestCase):
    """Opening the flyout refreshes within each provider's pace; Refresh now forces."""

    def run_loop(self, which):
        with tempfile.TemporaryDirectory() as temp:
            soon, forced = Path(temp) / "refresh.soon", Path(temp) / "refresh.request"
            stop, calls = threading.Event(), []

            def poll(block=True, force_claude=False):
                calls.append(force_claude)
                if len(calls) == 1:
                    (soon if which == "soon" else forced).write_text("1")
                else:
                    stop.set()
                return []

            with mock.patch.object(tray, "REFRESH_SOON", soon), mock.patch.object(tray, "REFRESH_SIGNAL", forced),                     mock.patch.object(tray, "poll", side_effect=poll),                     mock.patch.object(tray, "_panel_visible", return_value=False):
                tray.poll_loop(stop)
        return calls

    def test_opening_the_flyout_polls_without_forcing(self):
        self.assertEqual(self.run_loop("soon"), [False, False])

    def test_refresh_now_forces(self):
        self.assertEqual(self.run_loop("forced"), [False, True])


if __name__ == "__main__":
    unittest.main()
