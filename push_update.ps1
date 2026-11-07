# Push nhanh moi thay doi trong du an len GitHub (origin/main).
# Cach dung:
#   powershell -ExecutionPolicy Bypass -File .\push_update.ps1
#   powershell -ExecutionPolicy Bypass -File .\push_update.ps1 "mo ta thay doi"
# .gitignore van duoc ton trong: chrome_profile, bcr_state.json, drivers... khong bi push.

param([string]$Message = "")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$git = "C:\Program Files\Git\cmd\git.exe"
if (-not (Test-Path $git)) {
    $git = "git"
}

& $git add -A
$staged = & $git diff --cached --name-only
if (-not $staged) {
    Write-Host "Khong co thay doi nao de push."
    exit 0
}

if (-not $Message) {
    $Message = "Update " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
}

& $git commit -m $Message
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $git push origin main
exit $LASTEXITCODE
