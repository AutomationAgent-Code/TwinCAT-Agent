TwinCAT Agent - TwinCAT 3.1 Build 4024 VM Candidate
====================================================

This is a compatibility candidate, not yet a production-certified 4024 release.

Run inside the Windows VM:

1. Install TwinCAT 3.1 Build 4024 with TwinCAT XAE Shell.
2. Take a VM snapshot and close TcXaeShell.
3. Open a normal (non-administrator) PowerShell in this folder and run:

   powershell -NoProfile -ExecutionPolicy Bypass -File .\Test-TwinCAT4024Environment.ps1 -Strict

4. Only when Compatible is True, run TwinCAT-Agent-4024-Setup.exe.
5. Start TwinCAT Agent as a normal user, then open TcXaeShell and test the panel.

Please return these files if a test fails:

- TwinCAT4024-Environment.json
- %LOCALAPPDATA%\Programs\TwinCAT Agent\_backend.log
- %APPDATA%\Beckhoff\TwinCAT\3.1\ComponentConfig\AppEnv\15.0\ActivityLog.xml

See VM-Test-Guide.md for the complete test matrix.
