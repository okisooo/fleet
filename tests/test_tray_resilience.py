import unittest
import threading
import json
import tempfile
import time
from pathlib import Path
from unittest import mock

import tray
import panel_wpf


class TrayRegistrationTests(unittest.TestCase):
    def test_missing_shell_registration_is_recreated(self):
        icon = mock.Mock()
        with mock.patch.object(
            tray,
            "_tray_icon_rect",
            side_effect=[None, (1673, 1032, 1695, 1080)],
        ):
            recovered = tray.ensure_tray_registration(icon)

        icon._show.assert_called_once_with()
        self.assertTrue(recovered)

    def test_existing_shell_registration_is_left_alone(self):
        icon = mock.Mock()
        with mock.patch.object(
            tray,
            "_tray_icon_rect",
            return_value=(1673, 1032, 1695, 1080),
        ):
            recovered = tray.ensure_tray_registration(icon)

        icon._show.assert_not_called()
        self.assertTrue(recovered)

    def test_failed_recovery_does_not_kill_the_tray_loop(self):
        icon = mock.Mock()
        icon._show.side_effect = OSError("notification area unavailable")
        with mock.patch.object(tray, "_tray_icon_rect", return_value=None):
            recovered = tray.ensure_tray_registration(icon)

        self.assertFalse(recovered)


class TrayLifecycleTests(unittest.TestCase):
    def test_icon_construction_failure_also_retries(self):
        stopping = threading.Event()
        icon = mock.Mock()
        icon.run.side_effect = lambda setup: stopping.set()
        factory = mock.Mock(side_effect=[OSError("shell"), icon])
        with mock.patch.object(tray, "RESTART_SECS", 0):
            tray.run_tray(stop_event=stopping, icon_factory=factory)
        self.assertEqual(factory.call_count, 2)

    def test_ui_heartbeat_includes_collection_and_shell_health(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "heartbeat.json"
            with mock.patch.object(tray, "TRAY_HEARTBEAT", path), mock.patch.object(
                tray, "_tray_icon_rect", return_value=None
            ), mock.patch.object(tray, "_LAST_POLL", 123), mock.patch.dict(
                tray._UI_ACK, {"ts": 0.0, "registered": False}
            ):
                self.assertEqual(tray._heartbeat(mock.Mock()), 0)
                tray._publish_heartbeat()
            beat = json.loads(path.read_text())
            self.assertFalse(beat["registered"])
            self.assertEqual(beat["poll_ts"], 123)
            self.assertLess(time.time() - beat["ts"], 2)

    def test_ui_callback_never_touches_the_disk(self):
        """The pump acknowledges in memory; the update thread does the writing."""
        with mock.patch.dict(tray._UI_ACK, {"ts": 0.0, "registered": False}),                 mock.patch.object(tray, "_tray_icon_rect", return_value=(0, 0, 1, 1)),                 mock.patch.object(tray, "_atomic_write") as write:
            self.assertEqual(tray._heartbeat(mock.Mock()), 0)
            write.assert_not_called()
            self.assertTrue(tray._UI_ACK["registered"])

    def test_an_unpublished_acknowledgement_writes_nothing(self):
        with mock.patch.dict(tray._UI_ACK, {"ts": 0.0, "registered": False}),                 mock.patch.object(tray, "_atomic_write") as write:
            tray._publish_heartbeat()
            write.assert_not_called()

    def test_a_held_destination_is_retried_before_it_is_reported(self):
        """A reader without FILE_SHARE_DELETE loses the rename for milliseconds."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            replace = mock.Mock(side_effect=[PermissionError("in use"), None])
            with mock.patch.object(tray.os, "replace", replace), mock.patch.object(
                tray.time, "sleep"
            ):
                tray._atomic_write(path, "{}")
            self.assertEqual(replace.call_count, 2)

    def test_a_permanently_held_destination_still_raises(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            with mock.patch.object(
                tray.os, "replace", side_effect=PermissionError("in use")
            ), mock.patch.object(tray.time, "sleep"):
                with self.assertRaises(PermissionError):
                    tray._atomic_write(path, "{}")

    def test_dead_panel_is_replaced_without_trusting_pid_file(self):
        old = mock.Mock()
        old.poll.return_value = 1
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            tray, "_PANEL_PROCESS", old
        ), mock.patch.object(tray, "_PANEL_START", 0), mock.patch.object(
            tray.subprocess, "Popen"
        ) as launch:
            with mock.patch.multiple(tray, **{name: Path(temp) / name for name in (
                "PANEL_PID", "PANEL_QUIT", "PANEL_SIGNAL", "PANEL_HEARTBEAT"
            )}):
                tray.ensure_panel_host()
                launch.assert_called_once()
                self.assertIn("FLEET_PARENT_PID", launch.call_args.kwargs["env"])

    def _watch(self, heartbeat, responds):
        host = mock.Mock(pid=777)
        host.poll.return_value = None
        with mock.patch.object(tray, "_PANEL_PROCESS", host), mock.patch.object(
            tray, "_PANEL_START", 0
        ), mock.patch.object(tray, "_PANEL_LATE", False), mock.patch.object(
            tray, "PANEL_HEARTBEAT", heartbeat
        ), mock.patch.object(tray, "_panel_responds", return_value=responds) as probe, mock.patch.object(
            tray.time, "sleep"
        ), mock.patch.object(tray.subprocess, "Popen") as launch, mock.patch.object(tray, "_log_runtime") as log:
            tray.ensure_panel_host()
        return host, probe, launch, log

    def test_a_heartbeat_caught_mid_replace_is_not_a_stall(self):
        # .NET File.Replace leaves the name missing for a moment; a stat there
        # used to restart a healthy panel (2026-10-08 15:41, 2026-10-09 00:52).
        heartbeat = mock.Mock()
        heartbeat.stat.side_effect = [FileNotFoundError(2, "mid-replace"), mock.Mock(st_mtime=time.time())]
        host, probe, launch, log = self._watch(heartbeat, responds=False)
        host.terminate.assert_not_called()
        probe.assert_not_called()
        launch.assert_not_called()

    def test_a_late_heartbeat_from_a_responsive_panel_keeps_it(self):
        # After a sleep or a held file the beat is old, but the window answers.
        with tempfile.TemporaryDirectory() as temp:
            heartbeat = Path(temp) / "panel-heartbeat.json"
            heartbeat.write_text("{}")
            old = time.time() - 600
            tray.os.utime(heartbeat, (old, old))
            host, probe, launch, log = self._watch(heartbeat, responds=True)
        host.terminate.assert_not_called()
        launch.assert_not_called()
        probe.assert_called_once()
        self.assertIn("responds", log.call_args.args[0])

    def test_a_silent_panel_with_a_stale_heartbeat_is_restarted(self):
        with tempfile.TemporaryDirectory() as temp:
            heartbeat = Path(temp) / "panel-heartbeat.json"
            heartbeat.write_text("{}")
            old = time.time() - 600
            tray.os.utime(heartbeat, (old, old))
            host, probe, launch, log = self._watch(heartbeat, responds=False)
        host.terminate.assert_called_once()
        log.assert_called_once_with("panel heartbeat stalled; restarting")

    def test_the_last_panel_window_survives_a_heartbeat_mid_replace(self):
        host = mock.Mock(pid=777)
        host.poll.return_value = None
        heartbeat = mock.Mock()
        heartbeat.read_text.side_effect = FileNotFoundError(2, "mid-replace")

        def owner(hwnd, out):
            out._obj.value = 777

        with mock.patch.object(tray, "_PANEL_PROCESS", host), mock.patch.object(
            tray, "_PANEL_HWND", 4242
        ), mock.patch.object(tray, "PANEL_HEARTBEAT", heartbeat), mock.patch.object(
            tray.ctypes.windll.user32, "GetWindowThreadProcessId", side_effect=owner
        ):
            self.assertEqual(tray._panel_window(), 4242)

    def test_locked_heartbeat_file_does_not_escape_the_update_thread(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            tray, "TRAY_HEARTBEAT", Path(temp) / "heartbeat.json"
        ), mock.patch.dict(
            tray._UI_ACK, {"ts": time.time(), "registered": True}
        ), mock.patch.object(
            tray.os, "replace", side_effect=PermissionError("file is being read")
        ), mock.patch.object(tray.time, "sleep"), mock.patch.object(
            tray, "_log_runtime_exception"
        ) as log:
            tray._publish_heartbeat()
            log.assert_called_once()

    def test_explicit_quit_signals_both_app_processes(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            tray, "_SHUTDOWN", threading.Event()
        ), mock.patch.object(tray, "PAUSED", Path(temp) / "paused"), mock.patch.object(
            tray, "PANEL_QUIT", Path(temp) / "quit"
        ):
            icon = mock.Mock()
            tray.quit_all(icon)
            self.assertTrue(tray.PAUSED.exists())
            self.assertTrue(tray._SHUTDOWN.is_set())
            icon.stop.assert_called_once()

    def test_old_state_cannot_claim_live_and_partial_cache_is_honest(self):
        providers = [{"err": ""}, {"err": "offline", "cached_at": 100}]
        self.assertEqual(panel_wpf._freshness_status(providers, 999, now=1000)[0], "1 live · 1 cached")
        self.assertEqual(panel_wpf._freshness_status(providers, 100, now=1000)[0], "updated 15m ago")

    def test_an_unexpected_message_loop_exit_restarts_the_tray(self):
        stopping = threading.Event()
        first = mock.Mock()
        second = mock.Mock()
        second.run.side_effect = lambda setup: stopping.set()

        with mock.patch.object(tray, "RESTART_SECS", 0), mock.patch.object(
            tray, "_log_runtime"
        ) as log_runtime:
            tray.run_tray(
                stop_event=stopping,
                icon_factory=mock.Mock(side_effect=[first, second]),
            )

        first.run.assert_called_once()
        second.run.assert_called_once()
        log_runtime.assert_called_with("tray message loop ended; restarting")

    def test_a_real_quit_does_not_restart_the_tray(self):
        stopping = threading.Event()
        icon = mock.Mock()
        icon.run.side_effect = lambda setup: stopping.set()
        factory = mock.Mock(return_value=icon)

        with mock.patch.object(tray, "_log_runtime") as log_runtime:
            tray.run_tray(stop_event=stopping, icon_factory=factory)

        factory.assert_called_once_with()
        log_runtime.assert_not_called()

    def test_an_update_loop_failure_stops_the_broken_icon(self):
        stopping = threading.Event()
        icon = mock.Mock()

        with mock.patch.object(tray.threading, "Thread"), mock.patch.object(
            tray, "apply", side_effect=OSError("shell unavailable")
        ), mock.patch.object(tray, "_log_runtime_exception") as log_exception:
            tray.loop(icon, stopping)

        self.assertTrue(stopping.is_set())
        icon.stop.assert_called_once_with()
        log_exception.assert_called_once()


if __name__ == "__main__":
    unittest.main()
