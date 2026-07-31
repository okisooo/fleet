param(
    [string]$Version = "dev"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "dist\Fleet-$Version"
$buildVenv = Join-Path $root ".build-venv"
$python = Join-Path $buildVenv "Scripts\python.exe"

Push-Location $root
try {
    if (-not (Test-Path $python)) {
        python -m venv $buildVenv
    }
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt pyinstaller
    & $python -m PyInstaller --noconfirm --clean --windowed --onefile `
        --name Fleet --collect-all pystray --collect-all PIL --collect-all pythonnet `
        --hidden-import clr --hidden-import panel_wpf tray.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
    New-Item -ItemType Directory -Force $out | Out-Null
    Copy-Item (Join-Path $root "dist\Fleet.exe") (Join-Path $out "Fleet.exe") -Force
    Copy-Item (Join-Path $root "LICENSE") $out
    Copy-Item (Join-Path $root "README.md") $out
    Compress-Archive -Path (Join-Path $out "*") -DestinationPath (Join-Path $root "dist\Fleet-$Version-portable.zip") -Force
}
finally {
    Pop-Location
}
