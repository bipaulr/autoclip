# Called by the Task Scheduler task: waits for Docker Desktop, then starts the stack.
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "backups\startup.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
$docker = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
if (-not (Get-Process "Docker Desktop" -ErrorAction SilentlyContinue)) { Start-Process $docker }
for ($i = 0; $i -lt 60; $i++) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 5
}
# The local fallback model (Ollama) runs outside Docker
$ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if ((Test-Path $ollama) -and -not (Get-Process "ollama" -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden
}
Set-Location $root
"$(Get-Date -Format s) starting autoclip" | Out-File -Append $log
docker compose up -d *>> $log
