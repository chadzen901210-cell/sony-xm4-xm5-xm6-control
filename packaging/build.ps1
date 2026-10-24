# Builds the Windows release: "<Out>\XM Control\" (XM Control.exe and sonyxm.exe, no Python
# needed) and "<Out>\XM-Control-<version>-windows.zip". Needs Python 3.11+ with PyInstaller:
#   python -m pip install pyinstaller
#   .\packaging\build.ps1
param(
    [string]$Python = "python",
    [string]$Out = (Join-Path (Split-Path $PSScriptRoot) "dist")
)
# PyInstaller logs to stderr, which Windows PowerShell 5.1 would treat as a failure
# under "Stop"; exit codes are checked instead.
$root = Split-Path $PSScriptRoot
$version = & $Python -c "import sys; sys.path.insert(0, r'$root'); import sonyxm; print(sonyxm.__version__)"
if ($LASTEXITCODE) { throw "could not read the version" }
$work = Join-Path ([IO.Path]::GetTempPath()) "xm-control-build"

& $Python -m PyInstaller --noconfirm --clean --distpath $Out --workpath $work (Join-Path $PSScriptRoot "xm_control.spec")
if ($LASTEXITCODE) { throw "PyInstaller failed" }

$zip = Join-Path $Out "XM-Control-$version-windows.zip"
Compress-Archive -Path (Join-Path $Out "XM Control") -DestinationPath $zip -Force -ErrorAction Stop
Write-Host "Built $zip"
