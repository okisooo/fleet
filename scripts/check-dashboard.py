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
from System import Activator, Array, Boolean, Int32, IntPtr, Object
from System.Reflection import Assembly, BindingFlags
from System.Threading import ApartmentState, Thread, ThreadStart
from System.Windows import Application, LogicalTreeHelper, Point, PresentationSource, ShutdownMode
from System.Windows.Controls import TextBlock
from System.Windows.Automation import AutomationProperties
from System.Windows.Input import Key, KeyEventArgs, Keyboard
from System.Windows.Interop import WindowInteropHelper
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
        handle=WindowInteropHelper(parent).Handle
        press(root,'Dashboard');pump()
        assert field(view,'page')=='dashboard'
        assert app.Windows.Count==1 and assembly.GetType('FleetPanel.DashboardWindow') is None
        assert parent.Left < -10000 and parent.Top < -10000
        assert view.GetType().GetProperty('Root').GetValue(view)==root
        assert WindowInteropHelper(parent).Handle==handle and not parent.ShowInTaskbar
        checks.append('Dashboard changes the existing popup page; one window and the same root')
        scroll=field(view,'scroller')
        scroll.ScrollToVerticalOffset(260);parent.UpdateLayout();pump()
        offset=scroll.VerticalOffset
        assert offset>0
        updated=json.loads(sample.read_text());updated['ts']=time.time()+1
        (scratch/'panel.json').write_text(json.dumps(updated),encoding='utf-8')
        call(parent,'LoadModel');pump()
        assert field(view,'page')=='dashboard' and abs(scroll.VerticalOffset-offset)<2
        checks.append('data refresh preserves the dashboard page and scroll position')
        press(root,'Refresh usage now')
        assert (scratch/'refresh.request').exists()
        checks.append('the popup header Refresh still requests collection')
        press(root,'Back to usage');pump()
        assert field(view,'page')=='usage' and scroll.VerticalOffset<2
        press(root,'Dashboard');pump()
        assert field(view,'page')=='dashboard' and scroll.VerticalOffset<2
        checks.append('Dashboard and Back reset scroll on navigation')
        for element in walk(root):
            if isinstance(element,TextBlock) and element.ActualWidth>0:
                right=element.TranslatePoint(Point(element.ActualWidth,0),root).X
                assert right<=root.ActualWidth-28+1, ('text extends past popup',element.Text,right)
                if element.Text.startswith('$') or (element.Text[:1].isdigit() and len(element.Text)<12):
                    assert element.ActualHeight < element.FontSize*1.9, ('numeric cell wraps',element.Text)
        checks.append('dashboard text and numeric cells fit the popup column')
        press(root,'Back to usage');pump()
        args=Array[Object]([IntPtr.Zero,Int32(0x8049),IntPtr.Zero,IntPtr.Zero,Boolean(False)])
        parent.GetType().GetMethod('OnMessage',flags).Invoke(parent,args);pump()
        assert field(view,'page')=='dashboard' and app.Windows.Count==1
        checks.append('the tray dashboard message selects the same popup page')
        press(root,'Back to usage');pump()
        (scratch/'dashboard.show').write_text('fresh request')
        call(parent,'OnSignalTick');pump()
        assert field(view,'page')=='dashboard' and app.Windows.Count==1
        press(root,'Back to usage');pump()
        (scratch/'dashboard.show').write_text('old request')
        os.utime(scratch/'dashboard.show',(time.time()-30,time.time()-30))
        call(parent,'OnSignalTick')
        assert field(view,'page')=='usage' and not (scratch/'dashboard.show').exists()
        checks.append('fresh file requests select Dashboard; stale requests are ignored')
        call(parent,'Finish');pump()
        assert field(view,'page')=='usage' and scroll.VerticalOffset<2
        assert app.Windows.Count==1
        assert not (scratch/'panel-error.log').exists()
        checks.append('closing resets to usage; no extra window or native error')
    except Exception as error:
        checks.append('FAILED: '+str(error))
    finally:
        if app is not None:app.Shutdown()

thread=Thread(ThreadStart(verify));thread.SetApartmentState(ApartmentState.STA)
thread.Start();thread.Join()
(scratch/'dashboard-check.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
print('\n'.join(checks))
sys.exit(1 if any(x.startswith('FAILED') for x in checks) else 0)
