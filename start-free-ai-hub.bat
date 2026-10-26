@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Prefer the bundled portable Python; fall back to system Python for a source checkout.
set "PY=python"
if exist "%~dp0python\python.exe" set "PY=%~dp0python\python.exe"

echo Starting Free AI Hub on 127.0.0.1:8899 ...
echo Dashboard: http://127.0.0.1:8899/
echo API base : http://127.0.0.1:8899/v1
"%PY%" run.py --host 0.0.0.0 --port 8899
if errorlevel 1 pause
endlocal
