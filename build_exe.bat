@echo off
rem ============================================================
rem  Build the PORTABLE app folder (PyInstaller onedir)
rem  Output: dist\VASP轨迹可视化\  (exe + _internal\)
rem  Copy that folder anywhere - no Python needed, no unpacking at startup.
rem
rem  Set VASP_ONEFILE=1 before running to build a single-file exe instead
rem  (works too, but every launch unpacks ~650 MB to %TEMP%: slow + heavy).
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=D:\miniconda3\envs\chem_env\python.exe"
if not exist "%PY%" (
  for %%I in (python.exe) do set "PY=%%~$PATH:I"
)
if not exist "%PY%" (
  echo [ERROR] Python interpreter not found.
  echo         Expected: D:\miniconda3\envs\chem_env\python.exe
  pause
  exit /b 1
)

echo ============================================================
echo  Building with: %PY%
if "%VASP_ONEFILE%"=="1" (
  echo  Mode: ONEFILE ^(single exe, unpacks to TEMP on every start^)
) else (
  echo  Mode: PORTABLE FOLDER ^(recommended^)
)
echo ============================================================

rem --- 关键: 把本环境的 DLL 目录放到 base conda 前面 -------------------
rem 否则 PyInstaller 会从 D:\miniconda3\Library\bin 解析 libexpat.dll,
rem 与本环境的 pyexpat.pyd 版本不匹配, exe 一启动就报 DLL load failed。
for %%D in ("%PY%") do set "ENVDIR=%%~dpD"
if exist "%ENVDIR%Library\bin" set "PATH=%ENVDIR%Library\bin;%ENVDIR%DLLs;%ENVDIR%Scripts;%PATH%"

"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
  echo [INFO] Installing PyInstaller ...
  "%PY%" -m pip install pyinstaller || (echo [ERROR] pip install failed & pause & exit /b 1)
)

"%PY%" -m PyInstaller --noconfirm --clean VASP_Trajectory.spec
if errorlevel 1 (
  echo.
  echo [ERROR] Build failed. See messages above.
  pause
  exit /b 1
)

echo.
echo ============================================================
if "%VASP_ONEFILE%"=="1" (
  echo  Done!  Single file: %~dp0dist\VASP轨迹可视化.exe
) else (
  echo  Done!  Portable folder: %~dp0dist\VASP轨迹可视化\
  echo         Run  VASP轨迹可视化.exe  inside that folder.
  echo         Copy the WHOLE folder to another PC to run it there.
)
echo ============================================================
pause
