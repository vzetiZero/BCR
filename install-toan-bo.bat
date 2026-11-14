@echo off
rem =====================================================================
rem  BCR - CAI DAT TOAN BO (Python backend + Electron UI)
rem  Chay 1 lan tren may moi. Sau do dung Start.bat de mo ung dung.
rem =====================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo   BCR - Cai dat toan bo (Python + Electron)
echo ============================================================
echo.

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY="

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY if exist "%SystemRoot%\py.exe" (
  "%SystemRoot%\py.exe" -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=%SystemRoot%\py.exe -3"
)

if not defined PY (
  echo [LOI] Khong tim thay Python 3.
  echo       Tai tai: https://www.python.org/downloads/
  echo       Khi cai dat: tich "Add Python to PATH".
  echo.
  pause
  exit /b 1
)

echo [1/5] Python: %PY%
%PY% -c "import sys; print('      Python ' + sys.version.split()[0])"
if errorlevel 1 goto :fail_python

echo.
echo [2/5] Cai thu vien Python (selenium, websocket-client, Pillow)
%PY% -m pip install --disable-pip-version-check --upgrade selenium websocket-client Pillow
if errorlevel 1 (
  echo [LOI] pip cai that bai. Kiem tra ket noi mang / proxy roi chay lai.
  pause
  exit /b 1
)

echo.
echo [3/5] Kiem tra ma nguon BCR (MVC: models / adapters / controllers / service)
%PY% -c "import bcr, bcr.service.engine, bcr.controllers.baccarat, bcr.controllers.longlong, bcr.models.longlong, bcr.models.live_sim, bcr.adapters.browser; print('      Module BCR OK')"
if errorlevel 1 (
  echo [LOI] Khong import duoc module du an - thieu thu vien hoac file bi doi.
  pause
  exit /b 1
)

echo.
echo [3b/5] Chay kiem thu nhanh (khong can Chrome)
%PY% -m unittest discover -s tests -t . -q
if errorlevel 1 (
  echo [CANH BAO] Kiem thu co loi - van tiep tuc cai giao dien, nhung hay xem lai.
)

echo.
echo [4/5] Cai giao dien Electron - npm install trong thu muc ui
where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo [LOI] Khong tim thay Node.js/npm.
  echo       Tai Node.js LTS tai: https://nodejs.org/  roi chay lai file nay.
  pause
  exit /b 1
)
node -v
call npm.cmd -v

pushd "%~dp0ui"
if not exist "package.json" (
  echo [LOI] Khong tim thay ui\package.json
  popd
  pause
  exit /b 1
)

rem npm moi chan postinstall cua electron -> cho phep ro rang (loi thi bo qua)
call npm.cmd install-scripts approve electron >nul 2>nul

call npm.cmd install --no-audit --no-fund
if errorlevel 1 (
  echo [LOI] npm install that bai.
  popd
  pause
  exit /b 1
)
popd

echo.
echo [5/5] Cai binary Electron (tu dong sua loi tai/giai nen)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-electron.ps1"
if errorlevel 1 (
  echo.
  echo [LOI] Chua cai duoc binary Electron. Xem thong bao phia tren.
  pause
  exit /b 1
)

if not exist "%~dp0ui\node_modules\electron\dist\electron.exe" (
  echo [LOI] Khong thay ui\node_modules\electron\dist\electron.exe
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   CAI DAT HOAN TAT
echo.
echo   Mo ung dung (Electron):  Start.bat
echo   Chay engine bang tay:    python -m bcr --game baccarat
echo ============================================================
echo.
choice /C YN /N /M "Mo ung dung ngay bay gio? [Y/N] "
if errorlevel 2 goto :end
if errorlevel 1 start "" "%~dp0Start.bat"

:end
endlocal
exit /b 0

:fail_python
echo [LOI] Python khong chay duoc. Cai lai Python 3.12+ va tich "Add Python to PATH".
pause
exit /b 1
