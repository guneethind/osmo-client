# Builds the Windows release into dist\ : the program folder, the portable zip, the installer (when Inno Setup
# is installed) and SHA256SUMS.txt. The same script runs on a developer machine and in CI.
#   .\packaging\build.ps1                 full build
#   .\packaging\build.ps1 -SkipInstaller  program folder and zip only
[CmdletBinding()]
param([switch]$SkipInstaller)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$build = Join-Path $root 'build'
$dist = Join-Path $root 'dist'
$venv = Join-Path $build 'venv'
$python = Join-Path $venv 'Scripts\python.exe'

function Invoke-Checked([scriptblock]$Command, [string]$What) {
    # Windows PowerShell 5.1 turns a native program's stderr output (PyInstaller logs there) into an error.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

# Python 3.14 is what the release is built and tested with (see requirements-windows.lock).
if (-not (Test-Path $python)) {
    New-Item -ItemType Directory -Force $build | Out-Null
    Write-Host 'Creating the build environment...'
    if (Get-Command py -ErrorAction SilentlyContinue) { Invoke-Checked { py -3.14 -m venv $venv } 'Creating the environment (is Python 3.14 installed?)' }
    else { Invoke-Checked { python -m venv $venv } 'Creating the environment' }
}
Invoke-Checked { & $python -m pip install --disable-pip-version-check -q --only-binary=:all: --require-hashes -r packaging\requirements-windows.lock } 'Installing the build requirements'

if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
$version = (& $python packaging\make_version.py).Trim()
Write-Host "Building Osmo Client $version"

# License texts of everything that ends up in the program folder.
Invoke-Checked { & $python -m piplicenses --format=plain-vertical --with-license-file --no-license-path --with-urls --output-file (Join-Path $build 'THIRD_PARTY_LICENSES.txt') } 'Collecting the licenses'
Add-Content -Path (Join-Path $build 'THIRD_PARTY_LICENSES.txt') -Value (Get-Content packaging\FFMPEG_NOTICE.txt -Raw) -Encoding utf8

if ($env:SOURCE_DATE_EPOCH -eq $null) { $env:SOURCE_DATE_EPOCH = (git log -1 --format=%ct) }
Invoke-Checked { & $python -m PyInstaller --noconfirm --clean --distpath $dist --workpath (Join-Path $build 'pyi') packaging\osmo-client.spec } 'PyInstaller'

$program = Join-Path $dist 'OsmoClient'
if (-not (Test-Path (Join-Path $program 'OsmoClient.exe'))) { throw 'OsmoClient.exe was not built' }

$zip = Join-Path $dist "OsmoClient-$version-windows-x64-portable.zip"
Invoke-Checked { & $python packaging\make_zip.py $program $zip "OsmoClient-$version" } 'Making the portable zip'

if (-not $SkipInstaller) {
    $iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
    if (-not $iscc) { $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1 }
    if (-not $iscc) { throw 'Inno Setup 6 is not installed (winget install JRSoftware.InnoSetup), or use -SkipInstaller' }
    Invoke-Checked { & $iscc "/DAppVersion=$version" "/O$dist" packaging\installer.iss } 'Inno Setup'
}

Get-ChildItem $dist -File | Get-FileHash -Algorithm SHA256 | ForEach-Object { "$($_.Hash.ToLower())  $(Split-Path -Leaf $_.Path)" } | Set-Content (Join-Path $dist 'SHA256SUMS.txt') -Encoding ascii
Write-Host "Done. Output in $dist"
Get-ChildItem $dist | Format-Table Name, Length -AutoSize
