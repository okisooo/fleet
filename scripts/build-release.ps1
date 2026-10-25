param(
    [string]$Version = "dev",
    [string]$ScratchRoot = $(if (Test-Path 'S:\') { 'S:\tmp\codex\fleet-build' } else { Join-Path $env:TEMP 'fleet-build' }),
    [string]$OutputRoot = '',
    [string]$BuildVenv = '',
    [switch]$SkipDependencies
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# Tag builds pass values such as "v0.2.0"; artifacts use the bare number.
$Version = $Version -replace '^[vV]', ''
if (-not $Version) { $Version = "dev" }
if (-not $OutputRoot) { $OutputRoot = Join-Path $root 'dist' }
$out = Join-Path $OutputRoot 'Fleet'
$assets = Join-Path $ScratchRoot 'assets'
New-Item -ItemType Directory -Force $ScratchRoot, $assets, $OutputRoot | Out-Null
$env:TEMP = $ScratchRoot
$env:TMP = $ScratchRoot
$env:PYINSTALLER_CONFIG_DIR = Join-Path $ScratchRoot 'pyinstaller-cache'
$env:PIP_CACHE_DIR = Join-Path $ScratchRoot 'pip-cache'
$env:PYTHONDONTWRITEBYTECODE = '1'
$providerAssets = Join-Path $root "assets\providers"
$thirdPartyNotices = Join-Path $root "THIRD_PARTY_NOTICES.md"
if (-not $BuildVenv) { $BuildVenv = Join-Path $ScratchRoot 'venv' }
$python = Join-Path $BuildVenv "Scripts\python.exe"

Push-Location $root
try {
    if (-not (Test-Path $python)) {
        python -m venv $BuildVenv
    }
    if (-not $SkipDependencies) {
        & $python -m pip install -r requirements.txt -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) { throw "Dependency install failed." }
    }
    & $python scripts\build_assets.py --version $Version --out $assets
    if ($LASTEXITCODE -ne 0) { throw "Asset generation failed." }
    if (-not (Test-Path $providerAssets)) { throw "Provider assets are missing: $providerAssets" }
    if (-not (Test-Path $thirdPartyNotices)) { throw "Third-party notices are missing." }
    & $python -m PyInstaller --noconfirm --clean --windowed --onedir `
        --distpath $OutputRoot --workpath (Join-Path $ScratchRoot 'build') --specpath $ScratchRoot `
        --exclude-module tkinter `
        --name Fleet --collect-all pystray --collect-all PIL --collect-all pythonnet `
        --hidden-import clr --hidden-import panel_wpf `
        --add-data "${providerAssets}:assets/providers" `
        --icon (Join-Path $assets "fleet.ico") `
        --version-file (Join-Path $assets "version-info.txt") (Join-Path $root 'tray.py')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
    New-Item -ItemType Directory -Force $out | Out-Null
    $panelProject = Join-Path $root 'native\FleetPanel\FleetPanel.csproj'
    $panelVersion = if ($Version -match '^\d+(\.\d+){1,3}$') { $Version } else { '0.0.0' }
    $env:NUGET_PACKAGES = Join-Path $ScratchRoot 'nuget'
    $env:NUGET_HTTP_CACHE_PATH = Join-Path $ScratchRoot 'nuget-http'
    $env:DOTNET_CLI_HOME = Join-Path $ScratchRoot 'dotnet-home'
    $env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
    $env:DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE = 'true'
    $env:DOTNET_NOLOGO = 'true'
    $panelOut = Join-Path $ScratchRoot 'panel'
    & dotnet build $panelProject -c Release -nologo -v q "-p:Version=$panelVersion" -o $panelOut
    if ($LASTEXITCODE -ne 0) { throw 'Native panel build failed.' }
    Copy-Item (Join-Path $panelOut 'FleetPanel.exe'), (Join-Path $panelOut 'FleetPanel.exe.config') $out
    Copy-Item (Join-Path $root "LICENSE") $out
    Copy-Item (Join-Path $root "README.md") $out
    Copy-Item $thirdPartyNotices $out
    Copy-Item (Join-Path $root "docs") $out -Recurse -Force
    Copy-Item (Join-Path $root "providers.example.json") $out
    Copy-Item (Join-Path $root "CHANGELOG.md") $out
    Copy-Item (Join-Path $root "CONTRIBUTING.md") $out
    Copy-Item (Join-Path $root "SECURITY.md") $out
    Compress-Archive -Path (Join-Path $out "*") -DestinationPath (Join-Path $OutputRoot "Fleet-$Version-portable.zip") -Force
    Write-Host "Built Fleet $Version"
}
finally {
    Pop-Location
}
