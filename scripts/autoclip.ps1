# Windows helper: .\scripts\autoclip.ps1 up | down | test | migrate | logs | status | lint
param([Parameter(Mandatory = $true)][ValidateSet("up", "down", "test", "migrate", "logs", "status", "lint")] [string]$Task)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
switch ($Task) {
    "up"      { docker compose up -d --build }
    "down"    { docker compose down }
    "test"    { & "$root\.venv\Scripts\python.exe" -m pytest -q }
    "migrate" { docker compose run --rm api alembic upgrade head }
    "logs"    { docker compose logs -f --tail 100 worker beat api }
    "status"  { docker compose ps; Start-Process "http://127.0.0.1:8010/status" }
    "lint"    { & "$root\.venv\Scripts\python.exe" -m compileall -q autoclip tests; Push-Location web; npm run typecheck; Pop-Location }
}
