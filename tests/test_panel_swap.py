"""The warm panel host keeps one window for days and swaps fresh content in.

These run the real WPF window offscreen on an STA thread; it is never shown.
"""
import gc
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import fleet
import panel_wpf


PANEL_DATA = [
    {
        "agent": "Claude",
        "plan": "pro",
        "err": "Claude Code is signed out",
        "configured": False,
        "metrics": [],
    },
    {
        "agent": "Codex",
        "plan": "pro",
        "err": "",
        "metrics": [fleet.M("Weekly", 41.0, time.time() + 86400)],
    },
]


def _on_sta(body):
    from System.Threading import ApartmentState, Thread, ThreadStart

    failure = []

    def run():
        try:
            body()
        except BaseException as exc:
            failure.append(exc)

    thread = Thread(ThreadStart(run))
    thread.SetApartmentState(ApartmentState.STA)
    thread.Start()
    thread.Join()
    if failure:
        raise failure[0]


def _find(root, match):
    from System.Windows import LogicalTreeHelper, UIElement

    stack = [root]
    while stack:
        node = stack.pop()
        if match(node):
            return node
        if isinstance(node, UIElement):
            stack.extend(
                child for child in LogicalTreeHelper.GetChildren(node)
                if isinstance(child, UIElement)
            )
    raise AssertionError("control not found")


class PanelSwapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "win32":
            raise unittest.SkipTest("WPF is Windows-only")
        try:
            panel_wpf._load_wpf()
            import System.Windows  # noqa: F401
        except Exception as exc:
            raise unittest.SkipTest(f"WPF unavailable: {exc}")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.sign_ins = []

        def claude_sign_in(on_exit=None):
            self.sign_ins.append(on_exit)
            return True

        self.fleet = SimpleNamespace(
            CACHE=Path(temp.name),
            _until=fleet._until,
            _age=fleet._age,
            claude_sign_in=claude_sign_in,
        )

    def test_swapped_out_content_is_released(self):
        # The host swaps on every poll. pythonnet's `button.Click += handler`
        # keeps that button, and through its parents the whole fragment, alive
        # until a matching `-=`: the host grew by ~130 MB a day.
        from System import GC, WeakReference

        alive = []

        def body():
            window, _, swap = panel_wpf._create_window(
                PANEL_DATA, time.time(), False, self.fleet, auto_present=False
            )
            try:
                # The window's own name scope keeps its first content; skip it.
                swap(PANEL_DATA, time.time())
                replaced = []
                for index in range(12):
                    replaced.append(WeakReference(window.Content.Child))
                    swap(PANEL_DATA, time.time() + index)
                gc.collect()
                GC.Collect()
                GC.WaitForPendingFinalizers()
                GC.Collect()
                alive.append(sum(ref.IsAlive for ref in replaced))
            finally:
                window.Close()

        _on_sta(body)
        # Nothing may accumulate; before the fix every replaced fragment stayed.
        self.assertLessEqual(alive[0], 1)

    def test_buttons_in_swapped_content_still_act(self):
        from System.Windows import RoutedEventArgs
        from System.Windows.Controls import Button

        enabled = {}

        def click(window, match):
            button = _find(window, lambda node: isinstance(node, Button) and match(node))
            button.RaiseEvent(RoutedEventArgs(Button.ClickEvent))
            return button.IsEnabled

        def body():
            window, _, swap = panel_wpf._create_window(
                PANEL_DATA, time.time(), False, self.fleet, auto_present=False
            )
            try:
                swap(PANEL_DATA, time.time() + 1)
                swap(PANEL_DATA, time.time() + 2)
                enabled["refresh"] = click(window, lambda node: node.Name == "RefreshButton")
                enabled["sign_in"] = click(window, lambda node: str(node.Tag) == "ClaudeSignIn")
            finally:
                window.Close()

        _on_sta(body)
        self.assertTrue((self.fleet.CACHE / "refresh.request").exists())
        self.assertEqual(len(self.sign_ins), 1)
        self.assertEqual(enabled, {"refresh": False, "sign_in": False})


if __name__ == "__main__":
    unittest.main()
