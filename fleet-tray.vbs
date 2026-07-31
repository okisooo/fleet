' Launch fleet tray fully hidden (no console flash). Used by Startup autostart.
Dim fso, here
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run _
  """" & here & "\.venv\Scripts\pythonw.exe"" """ & here & "\tray.py""", 0, False
