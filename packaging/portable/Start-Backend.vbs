' ============================================================================
'  TwinCAT Agent - start the portable backend hidden (no console window),
'  at normal (non-elevated) integrity. Path-relative: works from any folder.
'  Double-click this file to start the backend. Safe to run again - a second
'  instance just fails to bind the port and exits.
' ============================================================================
Dim sDir
sDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run """" & sDir & "\Start-Backend.cmd""", 0, False
