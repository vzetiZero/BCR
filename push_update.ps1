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

# May nay chua cau hinh git user.name/user.email thi `git commit` bao
# "unable to auto-detect email address". Truyen danh tinh inline (khong sua git config).
$identName = "vzetiZero"
$identEmail = "vzetiZero@users.noreply.github.com"
$hasName = (& $git config --get user.name)
$hasEmail = (& $git config --get user.email)
if ($hasName -and $hasEmail) {
    & $git commit -m $Message
} else {
    & $git -c "user.name=$identName" -c "user.email=$identEmail" commit -m $Message
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $git push origin main
exit $LASTEXITCODE
