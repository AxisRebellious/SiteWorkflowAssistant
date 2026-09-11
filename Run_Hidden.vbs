Set objFSO = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
strPath = objFSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = strPath

' Set environment variable for Playwright browsers
WshShell.Environment("PROCESS")("PLAYWRIGHT_BROWSERS_PATH") = strPath & "\runtime\browsers"

' Launch application using standalone embedded python (port-resilient launcher)
WshShell.Run "cmd /c runtime\python\python.exe launch.py", 0, False
