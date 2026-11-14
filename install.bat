@echo off
rem =====================================================================
rem  install.bat - giu ten cu, chuyen sang install-toan-bo.bat
rem  (ban cu co loi cu phap va bo qua buoc tai Electron)
rem =====================================================================
echo [..] Dung install-toan-bo.bat (ban day du: Python + Electron)...
call "%~dp0install-toan-bo.bat"
exit /b %errorlevel%
