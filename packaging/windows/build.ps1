# Builds the Windows download: dist\ClearJAV-Uploader\ (the program, its license and README.txt) and a zip of it.
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 [-Python <python.exe>] [-AlsoRefuse <word>,...]
#
# FFmpeg and MediaInfo are never included: members download them themselves (README-windows.txt says where)
# and put them next to the .exe, on PATH, or set their places on the page (Settings -> Programs).
# The build refuses to finish when a file names this computer (user name, folders, the GitHub account):
# packaging\windows\check_dist.py.
param(
    [string]$Python = "python",
    [string[]]$AlsoRefuse = @()
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$build = Join-Path $root "build\windows"
$venv = Join-Path $build "venv"
$dist = Join-Path $root "dist"
$app = Join-Path $dist "ClearJAV-Uploader"

$version = (Select-String -Path (Join-Path $root "clearjav_aio\__init__.py") -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "ClearJAV Uploader $version"

if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Making the build environment ($build\venv)..."
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed" }
}
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $root "requirements.txt") -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$running = Get-Process -Name "ClearJAV-Uploader" -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$app\*" }
if ($running) { throw "ClearJAV-Uploader.exe is running from $app (Quit the uploader on its page, or close its window), then build again." }
if (Test-Path $app) { Remove-Item -Recurse -Force $app }
& $py -m PyInstaller --noconfirm --clean --log-level WARN --distpath $dist --workpath (Join-Path $build "work") (Join-Path $PSScriptRoot "clearjav-uploader.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# The license and the short guide (which says where to download FFmpeg and MediaInfo).
Copy-Item (Join-Path $root "LICENSE") (Join-Path $app "LICENSE.txt")
Copy-Item (Join-Path $PSScriptRoot "README-windows.txt") (Join-Path $app "README.txt")

# FFmpeg and MediaInfo are never part of the download.
$bundled = @(Get-ChildItem -Recurse -File $app | Where-Object { $_.Name -in @("ffmpeg.exe", "ffprobe.exe", "MediaInfo.exe") })
if ($bundled) { throw "The build contains $(($bundled | ForEach-Object { $_.Name }) -join ', '): FFmpeg and MediaInfo are never included." }

# Nothing in the download may name this computer or its owner.
$refuse = @($env:USERNAME, $env:COMPUTERNAME, $root, (Split-Path $root -Parent), $env:USERPROFILE) + $AlsoRefuse
$gitUser = (git -C $root config user.name) 2>$null
$gitMail = (git -C $root config user.email) 2>$null
if ($gitUser) { $refuse += $gitUser }
if ($gitMail) { $refuse += $gitMail }
& $py (Join-Path $PSScriptRoot "check_dist.py") $app @($refuse | Where-Object { $_ })
if ($LASTEXITCODE -ne 0) { throw "The build names this computer (see above): not zipped." }

$zip = Join-Path $dist "ClearJAV-Uploader-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $app -DestinationPath $zip -CompressionLevel Optimal
$hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
$size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "Done: $zip ($size MB)"
Write-Host "SHA-256: $hash"
