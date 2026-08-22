$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$dist = Join-Path $root "dist"
$work = Join-Path $root "private\pyinstaller"

if (-not (Test-Path -LiteralPath $python)) {
  throw "Missing virtual environment: $python"
}

& $python -m PyInstaller --noconfirm --clean --onedir --name job-agent-backend `
  --paths $root `
  --distpath $dist `
  --workpath $work `
  --specpath $work `
  --add-data "$root\app\templates;app\templates" `
  --add-data "$root\app\static;app\static" `
  (Join-Path $PSScriptRoot "backend_entry.py")
