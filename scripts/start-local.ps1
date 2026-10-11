param([switch]$SQLite, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

try {
    $running = Invoke-RestMethod 'http://127.0.0.1:8000/api/health' -TimeoutSec 2
    if ($running.status -eq 'ok' -and $running.model -eq 'deepseek-flash') {
        Write-Host '银小二已运行：http://127.0.0.1:8000/'
        if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8000/' }
        exit 0
    }
} catch { }

foreach ($dependency in @('uv', 'npm.cmd')) {
    if (-not (Get-Command $dependency -ErrorAction SilentlyContinue)) {
        throw "请先安装 $dependency，再运行本脚本。"
    }
}
if ($SQLite) {
    $localDirectory = Join-Path $projectRoot '.local'
    New-Item -ItemType Directory -Path $localDirectory -Force | Out-Null
    $sqliteFile = (Join-Path $localDirectory 'bank.sqlite3').Replace('\', '/')
    $env:DATABASE_URL = "sqlite:///$sqliteFile"
} else {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw '请安装并启动 Docker Desktop，或使用 -SQLite 参数。'
    }
    & docker compose up -d --wait postgres
    if ($LASTEXITCODE -ne 0) { throw '数据库未启动，请打开 Docker Desktop 后重试。' }
    $env:DATABASE_URL = 'postgresql+psycopg://concierge:local-demo-only@127.0.0.1:55432/concierge'
}
& uv sync --locked
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }
Push-Location (Join-Path $projectRoot 'web')
try {
    & npm.cmd ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
} finally { Pop-Location }

Write-Host '启动银小二：http://127.0.0.1:8000/'
Write-Host '保持本窗口打开；按 Ctrl+C 停止应用。首次打开页面若尚未就绪，稍后刷新即可。'
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8000/' }
& uv run python -X utf8 -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
exit $LASTEXITCODE
