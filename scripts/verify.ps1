param([switch]$Browser)
$ErrorActionPreference = 'Stop'
if (-not $env:RAILSTORM_DATABASE_URL) { throw 'Set RAILSTORM_DATABASE_URL to a disposable local PostgreSQL database.' }
uv run alembic upgrade head
if ($LASTEXITCODE) { throw 'Migration failed' }
uv run pytest -q
if ($LASTEXITCODE) { throw 'Tests failed' }
uv run ruff check railstorm tests migrations scripts experiments
if ($LASTEXITCODE) { throw 'Lint failed' }
uv run ruff format --check railstorm tests migrations scripts experiments
if ($LASTEXITCODE) { throw 'Format failed' }
uv run mypy railstorm
if ($LASTEXITCODE) { throw 'Type check failed' }
Push-Location frontend
try {
    pnpm run build
    if ($LASTEXITCODE) { throw 'Frontend build failed' }
    if ($Browser) {
        node e2e.mjs
        if ($LASTEXITCODE) { throw 'Browser test failed' }
        node correspondent.e2e.mjs
        if ($LASTEXITCODE) { throw 'Correspondent browser test failed' }
    }
} finally { Pop-Location }
