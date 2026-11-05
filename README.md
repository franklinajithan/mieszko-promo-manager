# Mieszko Promo Manager

Standalone supermarket promotion management for an existing Ubuntu server. FastAPI / Python, React / TypeScript, PostgreSQL 16, a persistent job worker, ReportLab A4 PDFs and Docker Compose. No integration with other retail applications and no paid services required.

Implemented workflows include staff/admin login, catalogue CRUD/archive/restore, multiple indexed barcodes, server-side search/pagination, XLSX preview and background import, identity conflict detection, retained image originals and thumbnails, reusable image assets, multi-product campaigns, editable label presets, PDF preview, background printing, retained reprints, audit history and user management.

**Production deployment is a separate release gate.** Review actual CI and benchmark results and the remaining environment checks before exposing a real server. See [installation, backups, recovery and upgrade instructions](docs/OPERATIONS.md).

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
# For development only; production uses PostgreSQL and secure HTTPS cookies.
export DATABASE_URL=sqlite:///./data/promo.db
export DATA_DIR=./data
export COOKIE_SECURE=false
export ALLOWED_ORIGIN=http://127.0.0.1:8000
alembic upgrade head
python -m app.cli create-admin --username yourname
npm ci --prefix frontend
npm run build --prefix frontend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# Second terminal, same environment and virtualenv:
python -m app.worker
```

When using the Vite dev server, set `ALLOWED_ORIGIN=http://127.0.0.1:5173` and open that origin. Run only one worker per development SQLite database. Production uses the PostgreSQL queue lock.

## Verification

```bash
ruff check app migrations tests scripts
ruff format --check app migrations tests scripts
mypy app scripts/deploy.py
python -m pytest -q
npm run build --prefix frontend
```

GitHub Actions additionally exercises PostgreSQL migrations/API tests, Chromium end-to-end workflows, a 50,000-row import and repeat-import/search benchmark, Docker startup and a backup/restore drill. Evidence is uploaded as a workflow artifact. Benchmarking refuses to run outside an empty PostgreSQL database with `benchmark` in its name. Never benchmark against a production database.

Use the previously supplied workbook and official images through the authenticated UI; no business workbook, production database, credentials or original business imagery is stored in this repository.
