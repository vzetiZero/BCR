@echo off
REM Push nhanh moi thay doi len GitHub. Bam dup file nay la duoc.
REM Hoac: push_update.bat "mo ta thay doi"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0push_update.ps1" %*
