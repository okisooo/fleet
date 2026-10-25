"""Exercise the native dashboard with synthetic data and an offscreen HWND only.

Usage: python scripts/check-dashboard.py PANEL_EXE SAMPLE_JSON SCRATCH_DIR
Requires Windows and the project's pythonnet dependency. No API calls or keys.
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ['FLEET_PANEL_OFFSCREEN'] = '1'
exe, sample, scratch = map(Path, sys.argv[1:4])
scratch.mkdir(parents=True, exist_ok=True)
(scratch/'panel.json').write_bytes(sample.read_bytes())
import clr
for name in ('WindowsBase','PresentationCore','PresentationFramework'):
    clr.AddReference(str(Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF')/(name+'.dll')))
from System import Activator, Array, Object
from System.Reflection import Assembly, BindingFlags
from System.Threading import ApartmentState, Thread, ThreadStart
from System.Windows import Application, LogicalTreeHelper, PresentationSource, ShutdownMode
from System.Windows.Automation import AutomationProperties
from System.Windows.Input import Key, KeyEventArgs, Keyboard
from System.Windows.Threading import DispatcherFrame, DispatcherTimer
from System import TimeSpan, EventHandler

assembly=Assembly.LoadFile(str(exe.resolve()))
flags=BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Instance|BindingFlags.Static
checks=[]
def call(obj,name,*args):
    return obj.GetType().GetMethod(name,flags).Invoke(obj,Array[Object](args))
def field(obj,name):
    return obj.GetType().GetField(name,flags).GetValue(obj)
def walk(root):
    yield root
    for child in LogicalTreeHelper.GetChildren(root):
        if hasattr(child,'GetType'):
            try: yield from walk(child)
            except TypeError: pass
def pump(milliseconds=250):
    frame=DispatcherFrame()
    timer=DispatcherTimer()
    timer.Interval=TimeSpan.FromMilliseconds(milliseconds)
    def end(sender,event):
        timer.Stop();frame.Continue=False
    handler=EventHandler(end)
    timer.Tick+=handler;timer.Start()
    from System.Windows.Threading import Dispatcher
    Dispatcher.PushFrame(frame)
    timer.Tick-=handler
def press(root,name):
    button=next(x for x in walk(root) if AutomationProperties.GetName(x)==name)
    event=KeyEventArgs(Keyboard.PrimaryDevice,PresentationSource.FromVisual(root),0,Key.Enter)
    event.RoutedEvent=Keyboard.KeyDownEvent
    button.RaiseEvent(event)

def verify():
    app=None
    try:
        app=Application();app.ShutdownMode=ShutdownMode.OnExplicitShutdown
        parent=Activator.CreateInstance(assembly.GetType('FleetPanel.PanelWindow'),Array[Object]([str(scratch.resolve())]))
        call(parent,'Start');pump(1200)
        view=field(parent,'view');root=view.GetType().GetProperty('Root').GetValue(view)
        press(root,'Dashboard');pump()
        dashboard=field(parent,'dashboard')
        assert dashboard is not None and dashboard.IsVisible
        assert dashboard.Left < -10000 and dashboard.Top < -10000
        checks.append('Enter on the flyout Dashboard button opens an offscreen native window')
        press(root,'Dashboard');pump()
        assert field(parent,'dashboard')==dashboard
        checks.append('repeated clicks reuse the same dashboard')
        dashboard.Width=760;pump(350)
        assert field(dashboard,'columns')==1
        dashboard.Width=1120;pump(350)
        assert field(dashboard,'columns')==2
        checks.append('resize switches between one and two columns')
        scroll=field(dashboard,'scroll')
        scroll.ScrollToVerticalOffset(260);dashboard.UpdateLayout();pump()
        offset=scroll.VerticalOffset
        updated=json.loads(sample.read_text());updated['ts']=time.time()+1
        (scratch/'panel.json').write_text(json.dumps(updated),encoding='utf-8')
        call(parent,'LoadModel');pump()
        assert abs(scroll.VerticalOffset-offset)<2
        checks.append('model refresh preserves scroll position')
        press(dashboard.Content,'Refresh')
        assert (scratch/'refresh.request').exists()
        checks.append('dashboard Refresh requests collection through the tray')
        call(dashboard,'ChangeTheme',False);pump()
        assert not field(dashboard,'light')
        assert abs(field(dashboard,'scroll').VerticalOffset-offset)<2
        checks.append('theme changes retain the window and scroll position')
        assert (scratch/'panel.visible').exists()
        dashboard.Close();pump()
        assert field(parent,'dashboard') is None
        press(root,'Dashboard');pump()
        assert field(parent,'dashboard') is not None
        checks.append('closing and reopening creates one clean dashboard')
        assert not (scratch/'panel-error.log').exists()
        checks.append('no native errors; all actions use isolated sample data')
    except Exception as error:
        checks.append('FAILED: '+str(error))
    finally:
        if app is not None:app.Shutdown()

thread=Thread(ThreadStart(verify));thread.SetApartmentState(ApartmentState.STA)
thread.Start();thread.Join()
(scratch/'dashboard-check.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
print('\n'.join(checks))
sys.exit(1 if any(x.startswith('FAILED') for x in checks) else 0)
