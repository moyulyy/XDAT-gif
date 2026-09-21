@echo off
rem ============================================================
rem  XDATCAR to GIF  -  GUI debug launcher (console + pause)
rem  Use this one when run_gui.bat seems to close immediately.
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=D:\miniconda3\envs\chem_env\python.exe"
if not exist "%PY%" (
    for %%I in (python.exe) do set "PY=%%~$PATH:I"
)

echo ============================================================
echo  XDATCAR to GIF - GUI (debug mode)
echo  Python : %PY%
echo  Script : %~dp0xdatcar_gif_gui.py
echo ============================================================
echo.

if not exist "%PY%" (
    echo [ERROR] Python interpreter not found:
    echo         D:\miniconda3\envs\chem_env\python.exe
    pause
    exit /b 1
)

"%PY%" "%~dp0xdatcar_gif_gui.py"
echo.
echo [exit code] %errorlevel%
pause
