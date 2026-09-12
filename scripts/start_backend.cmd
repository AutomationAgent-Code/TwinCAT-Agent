@echo off
REM TwinCAT Agent backend launcher.
REM MUST run non-elevated: the COM bridge attaches to XAE via the running-object
REM table, which is isolated between elevated and non-elevated processes. An
REM elevated backend cannot see a normally-launched TcXaeShell.
REM Exits immediately if a backend already owns port 8765.

set REPO=G:\claude\twin-cat-agent
set PY=C:\Users\Aurora Home Office\AppData\Local\Programs\Python\Python314\python.exe

cd /d "%REPO%" || exit /b 1
"%PY%" -m tc_agent.backend >> "%REPO%\_backend.out" 2>> "%REPO%\_backend.log"
