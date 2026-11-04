# HappenHere — Claude Code Instructions

## Overview

Generic local events aggregator. Users configure a city ZIP, add event sources
via the admin UI, and the app scrapes + aggregates events from those sources.

## Stack

- FastAPI + Jinja2 + HTMX, APScheduler, SQLite + SQLAlchemy, Playwright (Chromium)
- Mirrors peakevents/metafin patterns throughout

## Git Workflow

- Never commit directly to `main`. Use `feat/`, `fix/`, `chore/` branches.
- Open a PR rather than merging locally.

## Commit Style

Conventional Commits: `feat:`, `fix:`, `chore:`, `refactor:`.

## Code Conventions

- Run `ruff check` before committing.
- No Alembic — use `create_all` + `_migrate_schema` pattern (see `app/db.py`).

## Adding a New Scraper

1. Create `app/scrapers/generic.py` or a new file implementing `BaseScraper`.
2. Constructor must accept `config`, `source_url`, and `venue_name`.
3. Add to `_TYPE_MAP` in `app/scrapers/registry.py` with a unique type key.
4. Add a label to `TYPE_LABELS` in registry.py.

## Key Architecture

- Sources are user-managed in the DB (no hardcoded seed list for venues).
- `app/discovery.py` auto-detects the right scraper for a pasted URL.
- `app/scrapers/registry.py` maps `source.type` → scraper class.
- Admin actions (source CRUD, scrape triggers) require login.
- Event browsing is public unless `require_login_to_view: true`.

## Secret Scanning

gitleaks runs in CI (`lint` job). To also catch secrets **before** push:

```bash
cp .github/hooks/pre-push .git/hooks/pre-push
# or: git config core.hooksPath .github/hooks
```

The hook blocks the push if gitleaks finds secrets. Add false positives
to `.gitleaks.toml` allowlists, not with `--no-verify`.
