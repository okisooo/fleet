"""Exercise the real WPF panel with a synthetic model in an isolated process.

Usage: python scripts/check-panel-performance.py PANEL_EXE SAMPLE_JSON OUTPUT_DIR
Requires pythonnet and psutil. Reports host-process memory separately from the
standalone benchmark; Python/CLR test overhead is included here. Never forces GC.
"""
import ctypes
import json
import os
from pathlib import Path
import sys
import time

import psutil

os.environ['FLEET_PANEL_OFFSCREEN'] = '1'
exe, fixture, out = map(Path, sys.argv[1:4])
out.mkdir(parents=True, exist_ok=True)
cache = out / 'cache'
cache.mkdir(exist_ok=True)
model = json.loads(fixture.read_text())
(cache/'panel.json').write_text(json.dumps(model))
import clr
for name in ('WindowsBase', 'PresentationCore', 'PresentationFramework'):
    clr.AddReference(str(Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF')/(name+'.dll')))
from System import Activator, AppDomain, Array, Boolean, EventHandler, GC, Object, TimeSpan
from System.Reflection import Assembly, BindingFlags
from System.Threading import ApartmentState, Thread, ThreadStart
from System.Windows import Application, ShutdownMode
from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherTimer

assembly = Assembly.LoadFile(str(exe.resolve()))
flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static
process = psutil.Process()
AppDomain.MonitoringIsEnabled = True
samples, checks = [], []
ctypes.windll.user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
ctypes.windll.user32.GetGuiResources.restype = ctypes.c_uint
ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p


def call(obj, name, *args):
    method = next(m for m in obj.GetType().GetMethods(flags)
                  if m.Name == name and len(m.GetParameters()) == len(args))
    return method.Invoke(obj, Array[Object](args))


def field(obj, name):
    return obj.GetType().GetField(name, flags).GetValue(obj)


def pump(ms=40):
    frame, timer = DispatcherFrame(), DispatcherTimer()
    timer.Interval = TimeSpan.FromMilliseconds(ms)
    def end(sender, event):
        timer.Stop()
        frame.Continue = False
    handler = EventHandler(end)
    timer.Tick += handler
    timer.Start()
    Dispatcher.PushFrame(frame)
    timer.Tick -= handler


def sample(cycle):
    m = process.memory_full_info()
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    samples.append(dict(cycle=cycle, private_resident=m.uss, working_set=m.rss,
                        private_commit=m.private, handles=process.num_handles(),
                        gdi=ctypes.windll.user32.GetGuiResources(handle, 0),
                        user=ctypes.windll.user32.GetGuiResources(handle, 1),
                        threads=process.num_threads(), managed_live=GC.GetTotalMemory(False),
                        allocated=AppDomain.CurrentDomain.MonitoringTotalAllocatedMemorySize))


def run():
    app = None
    try:
        app = Application()
        app.ShutdownMode = ShutdownMode.OnExplicitShutdown
        parent = Activator.CreateInstance(assembly.GetType('FleetPanel.PanelWindow'), Array[Object]([str(cache.resolve())]))
        call(parent, 'Start')
        pump(1300)
        view = field(parent, 'view')
        content = field(view, 'content')
        call(view, 'ShowDashboard')
        parent.UpdateLayout()
        original = content.Children[0]
        start = AppDomain.CurrentDomain.MonitoringTotalAllocatedMemorySize
        clock = time.perf_counter()
        reused = 0
        for i in range(100):
            model['ts'] += 1
            (cache/'panel.json').write_text(json.dumps(model))
            call(parent, 'LoadModel')
            pump(5)
            reused += int(content.Children[0] == original)
        refresh = dict(count=100, reused=reused, elapsed_ms=(time.perf_counter()-clock)*1000,
                       allocated=AppDomain.CurrentDomain.MonitoringTotalAllocatedMemorySize-start)
        # A real value change must still replace the controls.
        model['summary']['today'] += 1
        (cache/'panel.json').write_text(json.dumps(model))
        call(parent, 'LoadModel')
        pump()
        assert content.Children[0] != original
        checks.append('changed content rebuilds the dashboard')
        if assembly.GetType('FleetPanel.PanelData').GetField('ContentKey') is not None:
            assert reused == 100
            checks.append('100 timestamp-only refreshes retain existing controls')
            saved = json.loads(json.dumps(model))
            # Every meaningful status remains in the comparison, including the
            # timestamps that describe the source's freshness and log coverage.
            for section, key, value in [('coverage', 'updated', 42),
                                        ('settings', 'cost', False),
                                        ('summary', 'state', 'indexing')]:
                previous = content.Children[0]
                model[section][key] = value
                (cache/'panel.json').write_text(json.dumps(model))
                call(parent, 'LoadModel')
                pump()
                assert content.Children[0] != previous
            previous = content.Children[0]
            model['api_accounts'][0]['source'] = 'test-source'
            (cache/'panel.json').write_text(json.dumps(model))
            call(parent, 'LoadModel')
            pump()
            assert content.Children[0] != previous
            checks.append('coverage, settings, summary and account status changes still rebuild')
            model.clear()
            model.update(saved)
            (cache/'panel.json').write_text(json.dumps(model))
            call(parent, 'LoadModel')
            pump()
        sample(0)
        for i in range(1, 101):
            call(parent, 'Open')
            pump(360)  # settle entrance, including starting the real glass worker
            call(view, 'ShowDashboard')
            parent.UpdateLayout()
            call(view, 'ShowSettings')
            parent.UpdateLayout()
            call(view, 'ShowUsage')
            parent.UpdateLayout()
            call(parent, 'Close', Boolean(False))
            pump(80)
            assert not field(parent, 'open') and not field(parent, 'closing')
            if i % 10 == 0:
                sample(i)
                (out/'progress.json').write_text(json.dumps(samples, indent=2))
        checks.append('100 open/close and usage/dashboard/settings navigation cycles complete')
        call(parent, 'Close')  # Window.Close, not the private Close(bool)
        pump(200)
        sample(101)
        assert not (cache/'panel-error.log').exists()
        result = dict(checks=checks, refresh=refresh, samples=samples)
        (out/'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(dict(checks=checks, refresh=refresh)), flush=True)
    except Exception as error:
        (out/'failure.txt').write_text(str(error))
        print('FAILED:', error, flush=True)
    finally:
        if app is not None:
            app.Shutdown()


thread = Thread(ThreadStart(run))
thread.SetApartmentState(ApartmentState.STA)
thread.Start()
thread.Join()
sys.exit(1 if (out/'failure.txt').exists() else 0)
