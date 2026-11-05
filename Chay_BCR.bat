@echo off
rem Chay ung dung BCR (cua so desktop, khong mo cua so lenh)
cd /d "%~dp0"
if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (
  start "" "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" app.py
  goto :eof
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw app.py
  goto :eof
)
if exist "%SystemRoot%\pyw.exe" (
  start "" pyw -3 app.py
  goto :eof
)
start "" py -3 app.py
