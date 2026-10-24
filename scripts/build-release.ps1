param(
    [string]$Version = "dev"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# Tag builds pass values such as "v0.2.0"; artifacts use the bare number.
$Version = $Version -replace '^[vV]', ''
if (-not $Version) { $Version = "dev" }
$out = Join-Path $root "dist\Fleet-$Version"
$assets = Join-Path $root "build\assets"
$providerAssets = Join-Path $root "assets\providers"
$thirdPartyNotices = Join-Path $root "THIRD_PARTY_NOTICES.md"
$buildVenv = Join-Path $root ".build-venv"
$python = Join-Path $buildVenv "Scripts\python.exe"

Push-Location $root
try {
    if (-not (Test-Path $python)) {
        python -m venv $buildVenv
    }
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency install failed." }
    & $python scripts\build_assets.py --version $Version --out $assets
    if ($LASTEXITCODE -ne 0) { throw "Asset generation failed." }
    if (-not (Test-Path $providerAssets)) { throw "Provider assets are missing: $providerAssets" }
    if (-not (Test-Path $thirdPartyNotices)) { throw "Third-party notices are missing." }
    & $python -m PyInstaller --noconfirm --clean --windowed --onefile `
        --name Fleet --collect-all pystray --collect-all PIL --collect-all pythonnet `
        --hidden-import clr --hidden-import panel_wpf `
        --add-data "${providerAssets}:assets/providers" `
        --icon (Join-Path $assets "fleet.ico") `
        --version-file (Join-Path $assets "version-info.txt") tray.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
    New-Item -ItemType Directory -Force $out | Out-Null
    Copy-Item (Join-Path $root "dist\Fleet.exe") (Join-Path $out "Fleet.exe") -Force
    Copy-Item (Join-Path $root "LICENSE") $out
    Copy-Item (Join-Path $root "README.md") $out
    Copy-Item $thirdPartyNotices $out
    Copy-Item (Join-Path $root "docs") $out -Recurse -Force
    Copy-Item (Join-Path $root "providers.example.json") $out
    Copy-Item (Join-Path $root "CHANGELOG.md") $out
    Copy-Item (Join-Path $root "CONTRIBUTING.md") $out
    Copy-Item (Join-Path $root "SECURITY.md") $out
    Compress-Archive -Path (Join-Path $out "*") -DestinationPath (Join-Path $root "dist\Fleet-$Version-portable.zip") -Force
    Write-Host "Built Fleet $Version"
}
finally {
    Pop-Location
}
