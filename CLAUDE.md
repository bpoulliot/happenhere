# HappenHere — Claude Code Instructions

## Overview

Generic local events aggregator. Users add search locations (a US ZIP code, a US city and state,
or latitude/longitude, each with a radius) and event sources via the admin UI, and the app scrapes +
aggregates events from those sources.

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

## Search locations (item 30)

- `app/locations.py` keeps the list (Admin > Locations, or `locations:` in the import file below).
  A ZIP code or city resolves **offline** from the bundled US Census Gazetteer (`app/gazetteer.py`,
  data in `app/geodata/`, vintage and source in its `SOURCE.md`, refreshed by
  `scripts/gazetteer_trim.py`). No lookup leaves the server; tests use `tests/fixtures/gazetteer`.
- A scraper class with `LOCATION_AWARE = True` takes `location=` (a `SearchArea`) and runs once per
  location assigned to its source (`Source.location_ids`: NULL = every location, a list = exactly
  those). `app/jobs.py` merges the searches into one batch and links each event row to the
  locations that returned it (`event_locations`). A per-venue type does not fan out.
- An identity key never contains a location: editing the list must never move a feed UID.
- No location ships. A fresh install searches nothing until one is added.

## Source import/export

A deployment keeps its source list in a YAML file outside this repo and loads it with:

```bash
python -m app.sources export > sources.yml             # every source, YAML to stdout
python -m app.sources import sources.yml               # dry run: prints the add/update/unchanged/missing plan
python -m app.sources import sources.yml --apply       # write it (one transaction)
python -m app.sources import sources.yml --apply --disable-missing   # also disable sources not in the file
```

Keyed by `name`; never deletes; a bad entry (unknown type, non-public URL, ...) refuses the whole
file, except that a *disabled* entry whose URL the guard refuses is stored disabled with the
refusal as its `last_error` and a warning in the plan (enabling it re-runs the guard). Inside the container: `docker exec <container> python -m app.sources ...` (add
`--db <path>` to name another database).

## Adding a New Scraper

1. Create `app/scrapers/generic.py` or a new file implementing `BaseScraper`.
2. Constructor must accept `config`, `source_url`, and `venue_name` (and `location`, if the class
   sets `LOCATION_AWARE = True`: item 30).
3. Add to `_TYPE_MAP` in `app/scrapers/registry.py` with a unique type key.
4. Add a label to `TYPE_LABELS` in registry.py.

A scraper for one particular site does not belong in this repository: write it as a plugin.

## Local scraper plugins

A deployment keeps site-specific scrapers in its own directory, outside the repo, and sets
`plugins_dir` in `config.yml` (or env `PLUGINS_DIR`). Unset means no plugins.

- At start-up every `*.py` directly in that directory is imported as `happenhere_plugins.<stem>`
  (a sibling can be imported as `happenhere_plugins.<other>`). Each class **defined in that module**
  that subclasses `BaseScraper`, is not abstract and sets `SOURCE_TYPE` in its own body is registered
  under that type. `TYPE_LABEL` (default: the class name) is the label in the admin type selector.
- `SOURCE_TYPE` is `[a-z0-9][a-z0-9_-]*` and must be new: one that collides with a built-in type or an
  earlier plugin (modules load in file-name order) is refused, never overridden.
- The constructor contract is the built-ins': `__init__(self, config, source_url="", venue_name="")`;
  `scrape()` returns `list[EventData]`; `close()` is optional.
- Set `EventData.source_uid` when the site gives an event id, else the event's own `url`: they decide
  the event's feed UID (see "Feeds"). Never put the title in either.
- The API a plugin may rely on is `app.scrapers.base` (`BaseScraper`, `EventData`, `DEFAULT_HEADERS`)
  and the shared bases `app.scrapers._html_base`, `_ical_base`, `_playwright_base`. Playwright only
  through `PlaywrightScraper`; a fetch of a user-provided URL goes through `app/netguard.py`.
- A module that fails to import (syntax error, exception, `sys.exit`) is skipped and logged at ERROR with
  its traceback; the app still starts. `/admin` shows a "Plugins" panel with what loaded and what failed.
- `python -m app.sources` loads the plugins too, so a plugin's type is valid in a `sources.yml`.
- Auto-detection (`app/discovery.py`) is unchanged: a plugin type is chosen by hand in the source form.
- **Security:** a plugin is code running with the app's privileges. It is loaded only from the
  configured directory, never from the database, an upload or a URL. The directory must be writable
  only by the operator; mount it read-only into the container (`./plugins:/app/plugins:ro`).
  Nothing in the admin UI can add or change a plugin.

## Feeds

Public, read-only `text/calendar`, `ETag` + `Cache-Control: public, max-age=900` (a matching
`If-None-Match` gets `304`), reaching `feed_lookahead_days` ahead (default 365):

- `/feed/all.ics` (`/feed/events.ics` is an alias, kept for existing subscriptions);
- `/feed/tags/<tag>[+<tag>...].ics`: events whose source carries **every** listed tag;
- `/feed/venue/<source-name>.ics`: one source.

An unknown tag or source is a 404. Tags are slugs (`[a-z0-9-]`) on a source, set in the admin form or
`sources.yml`; `categories` only label events in the UI. `/feeds` lists every feed with an upcoming
event, with copy, `webcal://` and Google Calendar links. Each calendar is named `<site_name>` or
`<site_name> — <selection>`.

**A feed UID never changes** (`app/dedup.py`): a calendar app keys an event on it. A scraped event is
matched first on its identity key: the source's own event id (`source_uid`), else its own URL (not the
listing page), else start time + venue; the title is never part of it. Two events in one scrape with
the same key fall back to the dedup hash (title + date + venue). `events.uid` is set once at insert;
rows stored before item 03 keep their old `<dedup_hash>@peakevents` UID. A dedup-hash match is a
different event when `is_different_event` says so (same source, different non-null key: item 25).

**Retired rows are never shown** (item 25): a successful scrape with at least one event retires the
source's upcoming rows it did not return (`events.retired_at`; nothing is deleted), and a row
returned again is un-retired with its UID. Query events for a reader only through
`app/models.py` `shown_events()`; a test refuses a `start_dt` filter anywhere else in `app/`.

## Deployment

- The container needs an init as PID 1: `init: true` in compose (the example `docker-compose.yml`
  has it), `docker run --init` otherwise. uvicorn as PID 1 does not reap Chromium helper processes
  that outlive their parent, and they stay as zombies (roadmap item 20).

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
