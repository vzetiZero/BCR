@echo off
rem Cai dat du an BCR: tim Python, cai thu vien, kiem tra Chrome/driver, chay test.
rem Chi lam viec tren may nay. Khong tai cookie, khong doc mat khau, khong mo cong ra ben ngoai.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
echo ==============================================
echo  BCR - Cai dat va kiem tra moi truong
echo ==============================================

set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto :have_python
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY goto :have_python
where python >nul 2>nul
if %errorlevel%==0 (set "PY=python" & goto :have_python)
if not exist "%SystemRoot%\py.exe" goto :no_python
"%SystemRoot%\py.exe" -3 -c "import sys" >nul 2>nul
if %errorlevel%==0 (set "PY=%SystemRoot%\py.exe -3" & goto :have_python)

:no_python
echo [XAI] Chua thay Python 3. Tai ve tu https://www.python.org/downloads/
echo        Khi cai, chon "Add Python to PATH" va tich "tcl/tk and IDLE".
pause
exit /b 1

:have_python
echo [OK] Python: %PY%
"%PY%" -c "import sys, tkinter; print('      ' + sys.version.split()[0] + ' - tkinter san sang')"
if not %errorlevel%==0 (
  echo [XAI] Python nay thieu tkinter. Cai lai Python va tich "tcl/tk and IDLE".
  pause
  exit /b 1
)

echo.
echo [..] Cai thu vien: selenium, websocket-client, Pillow
"%PY%" -m pip install --disable-pip-version-check selenium websocket-client Pillow
if not %errorlevel%==0 (
  echo [XAI] pip cai khong duoc. Kiem tra mang internet roi chay lai file nay.
  pause
  exit /b 1
)

echo.
echo [..] Kiem tra file du an va driver Chrome
"%PY%" -c "import browser, engine, lobby_stream, method, money; kind, note = browser.driver_for(); print('      ' + note); print('      Chrome: ' + (browser.chrome_path() or 'khong tim thay o vi tri mac dinh'))"
if not %errorlevel%==0 (
  echo [XAI] Thieu thu vien hoac file du an bi doi. Xem thong bao phia tren.
  pause
  exit /b 1
)

echo.
echo [..] Chay don vi test logic Python
"%PY%" -m unittest -q test_live_app test_longlong test_longlong_table test_longlong_auto test_engine test_architecture
if not %errorlevel%==0 (
  echo [XAI] Don vi test Python that bai. Chua nen chay gia lap.
  pause
  exit /b 1
)

where node >nul 2>nul
if %errorlevel%==0 goto 
echo.
echo [..] Cai giao dien Electron (ui)
where npm >nul 2>nul
if not %errorlevel%==0 (
  echo [BO QUA] Chua co npm - giao dien Electron se khong cai duoc.
  goto :skip_ui
)
pushd "%~dp0ui"
call npm install --no-audit --no-fund
if not exist "node_modules\electron\dist\electron.exe" (
  echo [..] Tai binary Electron
  node node_modules\electron\install.js
)
popd
:skip_ui

:node_tests
echo.
echo [..] Bo qua test reader.js (may chua cai Node.js). Khong anh huong van hanh.
goto :done

:node_tests
echo.
echo [..] Chay don vi test dau doc cau (reader.js)
node --test test_reader.cjs
if not %errorlevel%==0 (
  echo [XAI] Test reader.js that bai.
  pause
  exit /b 1
)

:done
echo.
echo [OK] Cai dat hoan tat.
echo Mo giao dien moi: double-click Chay_UI.bat (Electron)
echo Mo giao dien cu:  double-click Chay_BCR.bat (Tk)
echo   - "Mo Chrome" hoac "Gan Chrome dang mo" (CDP localhost 9222) roi bam "Vao sanh EVO".
echo   - "Quet toan sanh" roi "Tu mo ban dat chuan" de vao ban va gia lap duoi tay that.
echo   - Tick "Chi ban Ua thich" de app bam o loc Ua thich tren sanh va chi quet cac ban da thich.
echo   - Chuot nguoi that (WindMouse + ghost-cursor) bat san trong Cai dat; khong can cai them thu vien.
echo   - Ung dung chi doc cau va gia lap cuoc, khong dat cuoc that.
pause
endlocal
