# HappenHere — Claude Code Instructions

## Overview

Generic local events aggregator. Users configure a city ZIP, add event sources
via the admin UI, and the app scrapes + aggregates events from those sources.

## Stack

- FastAPI + Jinja2 + HTMX, APScheduler, SQLite + SQLAlchemy, Playwright (Chromium)

## Roadmap and process

- `docs/ROADMAP.md` is canonical: item status, labels (READY / NEEDS DECISION /
  NEEDS MEASUREMENT), tiers and the next free item number. One item per PR.
- **This repository is public and generic.** Never commit a deployment's venue list,
  city, hostnames, addresses, scraped events or any credential. Tests use synthetic
  fixtures on `example.com` hosts.

## Git Workflow

- `main` accepts changes only through a pull request with every CI check green
  (branch ruleset). Work in a worktree: `git worktree add /tmp/hh-NN-slug -b item-NN-slug origin/main`.
- Before every commit: `ruff check app/`, `black --check app/`, `bandit -r app/ -ll -x app/web/static`,
  `pytest tests/`, `gitleaks protect --staged`.

## Commit Style

Conventional Commits: `feat:`, `fix:`, `chore:`, `refactor:`.

## Code Conventions

- Every HTML page has a render test; templates use `TemplateResponse(request, name, context)`.
- Tests never enter `TestClient(app)` as a context manager (that runs the lifespan: scheduler,
  bootstrap, the real data path); wrap it in `contextlib.nullcontext`.
- Server-side fetches of a user-provided URL go through `app/netguard.py`.
- Playwright only through `PlaywrightScraper` (launch and close on one thread).
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

The hook blocks the push if gitleaks finds secrets. A real finding is removed, never
allowlisted; only a proven false positive goes in `.gitleaks.toml`. Never `--no-verify`.
