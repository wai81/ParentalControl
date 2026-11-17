# Builds dist\pcagent\pcagent.exe (one-folder) and dist\pcagent-<version>.zip
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

uv sync
if ($LASTEXITCODE -ne 0) { throw 'uv sync failed' }
uv run pytest -q
if ($LASTEXITCODE -ne 0) { throw 'tests failed' }
uv run pyinstaller agent\pcagent.spec --distpath dist --workpath build --noconfirm --log-level WARN
if ($LASTEXITCODE -ne 0) { throw 'pyinstaller failed' }

$version = (uv run python -c "import pc_agent; print(pc_agent.__version__)").Trim()
$zip = Join-Path $root "dist\pcagent-$version.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $root 'dist\pcagent') -DestinationPath $zip
Write-Host "Built: dist\pcagent\pcagent.exe"
Write-Host "Archive: $zip"
