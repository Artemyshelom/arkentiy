# AGENTS.md

## Cursor Cloud specific instructions

### Services overview

This is a **FastAPI + APScheduler** application (Python 3.12 compatible, pinned to 3.11 in Docker). It integrates with iiko restaurant management, Telegram Bot API, Google Sheets, and several other external services. The web frontend is static HTML/CSS/JS served by FastAPI's `StaticFiles`.

### Database

The app supports two database backends selected at import time by the `DATABASE_URL` environment variable (checked via `os.getenv` in `app/db.py`):

- **PostgreSQL** (`DATABASE_URL=postgresql://...`) — required for the full app; several modules (e.g. `app/jobs/audit.py`, `app/jobs/arkentiy.py`) import `get_pool` from `app.db` which is only available in PG mode.
- **SQLite** — legacy single-tenant mode; not all modules import cleanly in this mode.

**Critical:** `DATABASE_URL` must be set as an **actual environment variable** (not just in `.env`), because `app/db.py` reads it at module-import time via `os.getenv()` before pydantic-settings loads the `.env` file. Always export it or pass it inline: `DATABASE_URL=postgresql://... python -m uvicorn app.main:app ...`

PostgreSQL is available locally. To start it: `sudo pg_ctlcluster 16 main start`. The dev database is `ebidoebi` with user `ebidoebi` and password `devpassword`.

### Running the app

```bash
sudo pg_ctlcluster 16 main start
DATABASE_URL=postgresql://ebidoebi:devpassword@localhost:5432/ebidoebi \
  /workspace/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Key endpoints: `GET /health` (health check), `GET /docs` (Swagger UI), `GET /openapi.json`, `GET /` (landing page).

### Linting

No project-level linter config exists. Use `flake8` for basic checks:
```bash
/workspace/venv/bin/python -m flake8 app/ --select=E9,F63,F7,F82
```

### Testing

No automated tests exist in the repo yet. Run `pytest` from `/workspace` if tests are added to `tests/`.

### Missing dependency

`PyJWT` is used by `app/routers/cabinet.py` but is not listed in `requirements.txt`. It is installed in the venv.

### External API dependencies

Most scheduled jobs (iiko polling, Telegram, Google Sheets, competitor scraping) require valid API tokens/credentials. Without them, jobs fail gracefully with logged errors but the server stays healthy.
