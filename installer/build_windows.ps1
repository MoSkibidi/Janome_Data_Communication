# Builds the packaged Janome JP4 program: a folder with JanomeJP4.exe that runs on a Windows PC WITHOUT Python.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_windows.ps1
#
# Needs (on the PC that builds, not on the target PCs): Windows x64, Python 3.12 x64 (the `py` launcher or `python`),
# and internet access the first time (pip downloads the libraries and PyInstaller).
#
# Result:
#   installer\dist\JanomeJP4\                       the program folder (copy it to the target PC)
#   installer\dist\JanomeJP4-Windows-x64.zip        the same, zipped
# Everything this script creates is inside installer\ (.build-venv, build, dist); the source tree is not touched.

param(
    [switch]$NoZip
)

$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
$Root = Split-Path -Parent $Here
$Venv = Join-Path $Here '.build-venv'
$Dist = Join-Path $Here 'dist'
$Out = Join-Path $Dist 'JanomeJP4'
$PyInstallerVersion = '6.16.0'

function Run($exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Failed: $exe $($arguments -join ' ') (exit code $LASTEXITCODE)" }
}

# 1. A Python 3.12 to build with
$pyExe = $null
$pyArgs = @()
foreach ($candidate in @(@{ Exe = 'py'; Args = @('-3.12') }, @{ Exe = 'python'; Args = @() })) {
    try {
        $version = & $candidate.Exe @($candidate.Args + '--version') 2>&1
        if ("$version" -match 'Python 3\.12\.') { $pyExe = $candidate.Exe; $pyArgs = $candidate.Args; break }
    } catch { }
}
if (-not $pyExe) { throw "Python 3.12 x64 was not found. Install it from python.org (tick 'Add python.exe to PATH')." }
Write-Host "Using $pyExe $($pyArgs -join ' ')"

# 2. A private build environment (never mixed with the project's own .venv)
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    Run $pyExe ($pyArgs + @('-m', 'venv', $Venv))
}
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
Run $VenvPython @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip')
Run $VenvPython @('-m', 'pip', 'install', '--quiet', '-r', (Join-Path $Root 'requirements.txt'), "pyinstaller==$PyInstallerVersion")

# 3. Build
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
Push-Location $Root
try {
    Run $VenvPython @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', $Dist, '--workpath', (Join-Path $Here 'build'), (Join-Path $Here 'JanomeJP4.spec'))
} finally { Pop-Location }
if (-not (Test-Path (Join-Path $Out 'JanomeJP4.exe'))) { throw "JanomeJP4.exe was not produced." }

# 4. The files that make installing easy, next to the exe
foreach ($name in 'run.bat', 'stop.bat', 'run_service.bat', 'config.json') {
    Copy-Item (Join-Path $Root $name) $Out
}
foreach ($name in 'install_autostart.ps1', 'uninstall_autostart.ps1') {
    Copy-Item (Join-Path $Root "deploy\$name") $Out
}
Copy-Item (Join-Path $Here 'package\*') $Out

# 5. Zip
if (-not $NoZip) {
    $zip = Join-Path $Dist 'JanomeJP4-Windows-x64.zip'
    Compress-Archive -Path $Out -DestinationPath $zip -Force
    Write-Host "Zip: $zip"
}
Write-Host "Program folder: $Out"
Write-Host "NOTE: config.json inside the package is a copy of the project's config.json (it holds the database settings and token)."
