# -*- coding: utf-8 -*-
# Cai binary Electron cho ui/ (Electron ~32).
# Duoc goi tu install-toan-bo.bat / Start.bat, nhung cung chay tay duoc:
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup-electron.ps1
#
# Xu ly 3 truong hop hay gap:
#   1. npm chan postinstall (allowScripts) -> tu goi node install.js
#   2. extract-zip giai nen nua chung (chi con vai file trong dist) -> giai nen lai bang Expand-Archive
#   3. khong co cache/binary -> tai truc tiep tu GitHub releases roi giai nen

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$uiDir = Join-Path $root 'ui'
$elDir = Join-Path $uiDir 'node_modules\electron'
$dist = Join-Path $elDir 'dist'
$exe = Join-Path $dist 'electron.exe'
$pathTxt = Join-Path $elDir 'path.txt'

function Say([string]$text, [string]$color = 'Gray') {
    Write-Host $text -ForegroundColor $color
}

function Fail([string]$text) {
    Say "[LOI] $text" 'Red'
    exit 1
}

if (-not (Test-Path (Join-Path $elDir 'package.json'))) {
    Fail "Chua co $elDir. Chay 'npm install' trong thu muc ui truoc (install-toan-bo.bat lam viec nay)."
}

$version = (Get-Content (Join-Path $elDir 'package.json') -Raw | ConvertFrom-Json).version
Say "[..] Kiem tra binary Electron $version" 'Cyan'

function Complete-Install {
    Set-Content -LiteralPath $pathTxt -Value 'electron.exe' -NoNewline -Encoding ASCII

    # Electron la GUI app nen PowerShell co the khong bat duoc stdout;
    # uu tien doc version tu file dist\version, chi thu chay khi can.
    $ver = 'unknown'
    $verFile = Join-Path $dist 'version'
    if (Test-Path $verFile) { $ver = (Get-Content $verFile -Raw).Trim() }

    $logFile = Join-Path $env:TEMP 'bcr-electron-version.txt'
    Remove-Item -LiteralPath $logFile -Force -ErrorAction SilentlyContinue
    try {
        $p = Start-Process -FilePath $exe -ArgumentList '--version' -NoNewWindow -PassThru `
            -RedirectStandardOutput $logFile -RedirectStandardError ($logFile + '.err')
        if (-not $p.WaitForExit(60000)) { try { $p.Kill() } catch { } }
        if (Test-Path $logFile) {
            $out = (Get-Content $logFile -Raw).Trim()
            if ($out) { $ver = $out.TrimStart('v', 'V') }
        }
    } catch {
        Say "      (khong doc duoc version truc tiep, bo qua kiem tra chay thu)" 'DarkYellow'
    }

    $sizeMB = [math]::Round((Get-Item $exe).Length / 1MB)
    Say "[OK] Electron san sang: v$ver (electron.exe, $sizeMB MB)" 'Green'
    exit 0
}

if (Test-Path $exe) {
    Say "[OK] Binary Electron da co san." 'Green'
    Complete-Install
}

# --- Buoc 1: de electron tu tai (dung cache neu co) -------------------------
Say '[..] Thu tai bang node install.js ...'
Push-Location $elDir
& node install.js 2>&1 | ForEach-Object { Say "      $_" 'DarkGray' }
Pop-Location
if (Test-Path $exe) { Complete-Install }

# --- Buoc 2: giai nen lai tu cache (extract-zip co the hong) ---------------
$cacheRoot = Join-Path $env:LOCALAPPDATA 'electron\Cache'
$cachedZip = $null
if (Test-Path $cacheRoot) {
    $cachedZip = Get-ChildItem -Path $cacheRoot -Recurse -Filter "electron-v$version-win32-x64.zip" -ErrorAction SilentlyContinue |
        Sort-Object Length -Descending | Select-Object -First 1
}

$zig = $null
if ($cachedZip -and $cachedZip.Length -gt 50MB) {
    $zig = $cachedZip.FullName
    Say "[..] Dung cache: $zig" 'Cyan'
}

# --- Buoc 3: tai truc tiep tu GitHub -------------------------------------
if (-not $zig) {
    $url = "https://github.com/electron/electron/releases/download/v$version/electron-v$version-win32-x64.zip"
    $tmpDir = Join-Path $env:TEMP 'bcr-electron'
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
    $zig = Join-Path $tmpDir "electron-v$version-win32-x64.zip"
    Say "[..] Tai Electron tu GitHub (co the mat vai phut) ..." 'Cyan'
    Say "      $url" 'DarkGray'
    try {
        Invoke-WebRequest -Uri $url -OutFile $zig -UseBasicParsing
    } catch {
        Fail "Tai Electron that bai. Kiem tra mang/Proxy roi chay lai. ($($_.Exception.Message))"
    }
}

# --- Giai nen sach -------------------------------------------------------
Say '[..] Giai nen Electron ...' 'Cyan'
if (Test-Path $dist) { Remove-Item -LiteralPath $dist -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Path $dist -Force | Out-Null
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    Expand-Archive -LiteralPath $zig -DestinationPath $dist -Force
} catch {
    Fail "Giai nen that bai: $($_.Exception.Message)"
}

if (-not (Test-Path $exe)) {
    Fail "Giai nen xong nhung khong thay electron.exe trong $dist"
}

Complete-Install
