$ErrorActionPreference = "Stop"

$SkillDest = Join-Path $HOME ".gemini\config\skills\agytg"
$EnvDest = Join-Path $HOME ".gemini\agytg.env"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==> Installing /agytg skill to $SkillDest ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Path (Join-Path $SkillDest "scripts") -Force | Out-Null
Copy-Item -Path (Join-Path $RepoRoot "SKILL.md") -Destination (Join-Path $SkillDest "SKILL.md") -Force
Copy-Item -Path (Join-Path $RepoRoot "scripts\*.py") -Destination (Join-Path $SkillDest "scripts\") -Force

if (-not (Test-Path $EnvDest)) {
    Copy-Item -Path (Join-Path $RepoRoot ".env.example") -Destination $EnvDest
    Write-Host "==> Created config template at: $EnvDest" -ForegroundColor Yellow
    Write-Host "    Please edit $EnvDest and fill in your TELEGRAM_BOT_TOKEN and ALLOWED_USERS." -ForegroundColor Yellow
} else {
    Write-Host "==> Existing config found at: $EnvDest (kept unchanged)" -ForegroundColor Green
}

Write-Host "==> Installation complete! Type '/agytg' inside any agy CLI session to start." -ForegroundColor Green
