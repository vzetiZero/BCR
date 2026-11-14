@echo off
rem =====================================================================
rem  BCR - Mo ung dung (Electron Desktop)
rem  Moi tro (Baccarat / LONG-LONG) chay 1 engine Python rieng.
rem =====================================================================
setlocal EnableExtensions
cd /d "%~dp0"

rem Neu bien nay duoc dat -> Electron chay nhu Node thuong va bao loi ipcMain.
set "ELECTRON_RUN_AS_NODE="

set "ELECTRON_EXE=%~dp0ui\node_modules\electron\dist\electron.exe"
set "APP_DIR=%~dp0ui"

if not exist "%APP_DIR%\package.json" goto :no_app

if exist "%ELECTRON_EXE%" goto :launch

echo [..] Chua co binary Electron, dang tu cai ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-electron.ps1"
if not exist "%ELECTRON_EXE%" (
  echo.
  echo [LOI] Khong the cai Electron tu dong.
  echo       Chay install-toan-bo.bat de cai day du.
  pause
  exit /b 1
)

:launch
start "" "%ELECTRON_EXE%" "%APP_DIR%"
endlocal
exit /b 0

:no_app
echo [LOI] Khong tim thay giao dien Electron tai: %APP_DIR%
echo       Chay install-toan-bo.bat de cai dat lai.
pause
exit /b 1
