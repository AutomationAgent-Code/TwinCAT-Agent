@echo off
REM ============================================================================
REM  TwinCAT Agent - portable backend launcher (path-agnostic, no hardcoded paths)
REM
REM  MUST run NON-ELEVATED. The COM bridge attaches to TcXaeShell through the
REM  running-object table, which is isolated between elevated and non-elevated
REM  processes. An elevated backend cannot see a normally-launched TcXaeShell.
REM  So: just double-click Start-Backend.vbs (never "Run as administrator").
REM ============================================================================
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%runtime\python\python.exe"
set "APP=%ROOT%app"

if not exist "%PY%" (
  echo [ERROR] bundled Python not found at "%PY%"
  echo         The package looks incomplete - re-copy the whole folder.
  exit /b 1
)

cd /d "%APP%" || exit /b 1
"%PY%" -m tc_agent.backend >> "%ROOT%_backend.out" 2>> "%ROOT%_backend.log"
