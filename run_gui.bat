@echo off
rem ============================================================
rem  XDATCAR to GIF  -  GUI launcher (no console window)
rem  Keep this file ASCII-only with CRLF line endings.
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "APPDIR=%~dp0"
set "SCRIPT=%APPDIR%xdatcar_gif_gui.py"
set "PY=D:\miniconda3\envs\chem_env\python.exe"

if not exist "%SCRIPT%" (
    echo [ERROR] xdatcar_gif_gui.py not found:
    echo         %SCRIPT%
    echo         Please keep this bat next to the python script.
    pause
    exit /b 1
)

if not exist "%PY%" (
    for %%I in (python.exe) do set "PY=%%~$PATH:I"
)
if not exist "%PY%" (
    echo [ERROR] Python interpreter not found.
    echo         Expected: D:\miniconda3\envs\chem_env\python.exe
    echo         Install it, or edit the PY path inside this bat file.
    pause
    exit /b 1
)

rem prefer pythonw.exe (no console); fall back to python.exe
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"

start "" "%PYW%" "%SCRIPT%"
exit /b 0
