@echo off
rem Mo giao dien Electron (BCR Desktop). Moi tro chay engine Python rieng.
setlocal
rem Moi truong co the dat bien nay -> Electron se chay nhu Node thuong va bao loi ipcMain.
set "ELECTRON_RUN_AS_NODE="
cd /d "%~dp0ui"

if not exist "node_modules\electron\dist\electron.exe" (
  echo [XAI] Chua co Electron.
  echo Chay: cd ui ^&^& npm install
  pause
  exit /b 1
)

start "" "node_modules\electron\dist\electron.exe" .
endlocal
