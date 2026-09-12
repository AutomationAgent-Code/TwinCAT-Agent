' Launch the TwinCAT Agent backend with no visible console window.
' A copy of this file lives in the user's Startup folder so the backend is
' already running when TcXaeShell opens the panel.
' Runs at normal (non-elevated) integrity by design — see start_backend.cmd.
CreateObject("WScript.Shell").Run """G:\claude\twin-cat-agent\scripts\start_backend.cmd""", 0, False
