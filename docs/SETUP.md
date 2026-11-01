# Setup and reproducible demonstration

Docker quickstart and the all-rail smoke are in the README. Use a new local synthetic database.
Never point tests or fault injection at a real user or financial database. The full recovery tests
need a PostgreSQL role allowed to create disposable databases.

## Native installation (PowerShell)

Install Python 3.12, uv, PostgreSQL 18.6, Node 22.12+ and pnpm 11.25.0. Configure credentials
privately if your PostgreSQL role requires them. These commands assume a local synthetic role.

```powershell
uv sync --frozen --python 3.12
createdb -h 127.0.0.1 -U YOUR_POSTGRES_ROLE railstorm_preview
$env:RAILSTORM_DATABASE_URL='postgresql+psycopg://YOUR_POSTGRES_ROLE@127.0.0.1:5432/railstorm_preview'
uv run --frozen alembic upgrade head
uv run --frozen python scripts/load_scenarios.py
$env:CI='true'
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend run build
uv run --frozen uvicorn railstorm.api:factory --factory --host 127.0.0.1 --port 8840
```

On Bash use `export NAME=value` in place of PowerShell environment assignments. Open port8840.
`pnpm --dir frontend dev` provides development mode with an API proxy to 8840. The production build
is served by FastAPI. The pinned package policy allows the esbuild setup script.

## Financial demonstration

With the same database URL configured and the backend running, use a second terminal:

```powershell
uv run --frozen python -m experiments.r5_smoke initial --base http://127.0.0.1:8840 --checkpoint docs/local-smoke.json
```

Restart only that backend and run:

```powershell
uv run --frozen python -m experiments.r5_smoke restart --base http://127.0.0.1:8840 --checkpoint docs/local-smoke.json --restart-scope backend
```

For independent audit of a UI run ID, replace the quoted placeholder:

```powershell
uv run --frozen python -c "from railstorm.store import Store; from railstorm.p2a_oracle import independent_audit; print(independent_audit(Store(), 'YOUR_RUN_ID'))"
uv run --frozen python -m railstorm.cli reconcile YOUR_RUN_ID
uv run --frozen python -m railstorm.cli replay YOUR_RUN_ID
```

The audit independently reads financial tables; replay reconstructs and checks financial state from
events. Neither is a substitute for real-world bank reconciliation or regulatory certification.

## Maintained checks

```powershell
uv run --frozen python scripts/check_quality.py
uv run --frozen pytest -q
pnpm --dir frontend run build
```

Run tests on a fresh disposable database after migrations. Tests generate synthetic worlds and
some local evidence files. Historical benchmark generators are retained for inspection and require
their original campaign manifests; they are not release acceptance commands. No large benchmark
is run in CI. Local Docker smoke and GitHub Actions results are separate evidence classes.
