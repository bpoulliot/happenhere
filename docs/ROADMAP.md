# HappenHere roadmap

Canonical list of what is built and what is next. Labels: **DONE**, **READY** (the spec is
complete, a session can run it), **NEEDS DECISION** (an owner's answer is missing; the question
is written down), **NEEDS MEASUREMENT** (a fact is missing; a session finds it). Never start an
item that is not READY.

## How this roadmap works

* **Tiers, in order.** Tier 0 is correctness and security: a crash, a wrong event, a leak, an
  auth hole. Tier 1 is what a deployment needs to be useful (sources, feeds, sign-in). Tier 2 is
  automation and health (canaries, alerts). Tier 3 is reach (new source types, UI).
* **One item per session and one PR per item**, in its own worktree
  (`git worktree add /tmp/hh-NN-slug -b item-NN-slug origin/main`). Findings go into the item's
  own section, newest last. A measurement goes in `docs/measurements/NN-*.md`, by counts.
* **This repository is public and generic.** No deployment's venue list, city, hostnames,
  addresses, scraped events or credentials go in it. Tests use synthetic fixtures per platform
  type on `example.com` hosts. A deployment keeps its own source list outside the repo and loads
  it with the import command (item 05).
* **Labels are honest.** A READY item whose text says "decide first" is not READY.
* **CI is the gate:** ruff, black, hadolint, bandit, pip-audit, gitleaks, pytest, CodeQL
  (blocking; reviewed exceptions in `.github/codeql-accepted.json`, each with a reason and the
  narrowest count) and the Docker build. `main` takes changes only through a PR with all of them
  green.

**Next free item number: 27.** Always re-read it here before filing.

## Items

| # | Tier | Item | Status |
|---|---|---|---|
| 01 | 0 | Green CI; blocking CodeQL; Chromium leak; bcrypt; SSRF guard; fail-closed auth | **DONE** |
| 02 | 1 | OpenID Connect admin sign-in (group-gated, local login disabled); every HTML page rendering | **DONE** |
| 03 | 1 | Feeds by tag and by venue, stable event UIDs, "add to calendar" links | **DONE** |
| 04 | 1 | Personal feeds behind a long random token; an "artists I listen to" feed | **NEEDS MEASUREMENT** |
| 05 | 1 | Idempotent source import/export from a YAML file | **DONE** |
| 06 | 2 | Per-source canary: a source that returns zero events or keeps failing raises one alert | **READY** |
| 07 | 0 | Deprecations that will break: `datetime.utcnow()`, `ORJSONResponse`, authlib/Starlette on `httpx` | **READY** |
| 08 | 2 | Release workflow without a personal access token, compatible with a PR-only `main` | **NEEDS DECISION** |
| 09 | 0 | Dependency bumps, low risk: CI actions and patch-level libraries (Dependabot) | **DONE** |
| 10 | 0 | Web-framework bump: FastAPI and uvicorn (Starlette moves with them) | **DONE** |
| 11 | 3 | Facebook and Instagram as event sources; reading event flyers with a vision model | **NEEDS DECISION** |
| 12 | 1 | Ticketing platforms as source options: Ticketmaster-by-venue type, wider JSON-LD, platform recognition | **READY** |
| 13 | 1 | Local scraper plugins: extra source types loaded from a directory outside the repo | **DONE** |
| 14 | 1 | Configurable notification digest that never repeats an unchanged list | **DONE** |
| 15 | 0 | A re-scraped event keeps its first start time: a changed time never reaches the feed or the digest | **READY** |
| 16 | 1 | Eventbrite: the regional scraper is against the site's terms; an API token or the partner program instead | **NEEDS DECISION** |
| 17 | 0 | Hard-coded `America/Denver`: parsed times and the ICS feed ignore the configured timezone | **READY** |
| 18 | 0 | Two same-title shows on one day in one batch abort the whole scrape run (UNIQUE, no rollback) | **DONE** |
| 19 | 0 | Every `ical` and `spothopper` source fails: the generic scrapers have no `SOURCE_NAME` | **DONE** |
| 20 | 0 | Orphaned Chromium processes are never reaped while uvicorn is PID 1 | **DONE** |
| 21 | 0 | Secrets in logs: request lines and error text carry API keys from query strings | **DONE** |
| 22 | 1 | Ticketmaster regional search: `postalCode` ignores the radius; a real area search passes the 1,000-result cap | **READY** |
| 23 | 1 | JSON-LD: events listed under a `Place`'s capitalised `Events` key are missed by detection and the scraper | **READY** |
| 24 | 1 | Import: a disabled entry whose URL fails the network guard refuses the whole file | **READY** |
| 25 | 0 | A same-title show first seen in a later run gets its own row; rows a source stops listing retire | **DONE** |
| 26 | 0 | Scrapers that can return a truncated list retire events that are still on (item 25) | **NEEDS DECISION** |

### 01 — Green CI and safety fixes — DONE

CI on `main` was red; CodeQL never blocked. Fixed: secret scanning, CodeQL blocking with a
reviewed-exceptions file, the `Secure` session cookie, `bcrypt` pinned (it was never installed, so
passwords fell back to unsalted SHA-256), the Playwright browser launched and closed on one thread
with every context closed and failures logged (`tests/test_lifecycle.py`), `app/netguard.py`
refusing server-side fetches of non-public addresses (every hop), fail-closed `_get_user()`, the
first-run password written to a 0600 file instead of the log.

### 02 — OIDC admin sign-in — DONE

`auth_mode: oidc` with any provider: local login disabled, admin = a group in the ID token's
groups claim (`oidc_scopes` includes `groups` by default; some providers send the claim only for
that scope), PKCE, 12 h sessions keyed per mode, `next` carried in the session, never in a
redirect. Found while testing: every HTML page except `/login` crashed on current Starlette (old
`TemplateResponse` signature); every page now has a render test (`tests/test_oidc.py`).

### 03 — Feeds by tag and by venue — DONE

**Why:** one combined feed cannot be filtered in a calendar app. A subscriber wants, e.g., one
calendar per region and type, each with its own colour and toggle.

**Spec:**
1. **Tags on a source:** a free-form list (lowercase slugs, `[a-z0-9-]`), editable in the admin
   source form and stored on the source. The existing `categories` stay as they are (they label
   events in the UI); tags are what feeds select on.
2. **Feed URLs** (all public, read-only, `text/calendar`):
   * `/feed/all.ics`; `/feed/events.ics` stays as an alias of it;
   * `/feed/tags/<tag>[+<tag>...].ics`: events whose source carries **every** listed tag;
   * `/feed/venue/<source-name>.ics`: one source.
   An unknown tag or source is a 404, not an empty calendar.
3. **Per-feed calendar name:** `X-WR-CALNAME` = the configured site name plus the selection
   (e.g. "HappenHere — music + downtown"), `X-WR-CALDESC` likewise. Replace the leftover
   hard-coded product/description strings with the configured site name; `PRODID` names the app,
   not a city.
4. **Stable `UID`s:** a calendar app keys an event on its UID, so a UID that changes when a title is
   edited ("— SOLD OUT") leaves a ghost event and a duplicate. Today's UID comes from the dedup hash,
   which is built from the normalised title and date. First **measure**: on a synthetic source,
   retitle one event and show the UID change (failing test). Then derive the UID from the most stable
   identity the source gives: the source's own event UID (iCal) or event URL (JSON-LD, most HTML
   sources), else the source id + start time + venue; the dedup hash stays the fallback. Keep
   dedup behaviour unchanged unless a test shows it is the same defect.
5. **Look-ahead:** `feed_lookahead_days` in config (default 365). Large venues announce months
   ahead.
6. **"Add to calendar":** a page (`/feeds`) listing every feed URL that has at least one event,
   each with a copy button, a `webcal://` link and a Google Calendar subscribe link
   (`https://calendar.google.com/calendar/r?cid=<webcal URL>`).
7. **Caching:** `ETag` + `Cache-Control: public, max-age=900` on feeds; a conditional request
   gets `304`.

**Tests:** each URL form; intersection semantics; 404s; UID stable across a retitle (fails first,
then passes); look-ahead boundary; ETag round trip; the `/feeds` page renders.

**Not in scope:** personal/tokened feeds (item 04); source import (item 05).

**Findings (item 03 session).**

*Failing first.* A synthetic iCal source (`UID:evt-1@example.com`), its event retitled "Quartet
Night" → "Quartet Night — SOLD OUT" between two `upsert_events` calls, on `main` before this change
(`tests/test_feeds.py::test_uid_is_stable_when_an_ical_event_is_retitled`):

    UIDs before the retitle: ['9df43ee3…bd58@peakevents']
    UIDs after the retitle:  ['9df43ee3…bd58@peakevents', 'e4d8dcb9…b5ac@peakevents']

The UID changed **and the old event stayed**: the dedup hash is title + date + venue, so the retitled
event was stored as a new row and the old row stayed in every feed until its date passed. **The ghost
is the dedup defect itself**, so the dedup lookup changed with the UID: a stable UID on top of
title-keyed rows would have put two events with one UID in the feed.

*How each built-in type identifies an event* (what `identity()` in `app/dedup.py` gets):

| Type | Identity the source gives | Kind used |
|---|---|---|
| `ical` | the VEVENT `UID` (+ `RECURRENCE-ID` for an overridden occurrence) | `uid` |
| `ticketmaster` | the Discovery API event `id` | `uid` |
| `jsonld`, `playwright_jsonld` | schema.org `url`, else `@id` | `url`; `slot` if absent or the listing page |
| `squarespace` | the event's `.eventlist-title a` link, else the page URL | `url`, else `slot` |
| `spothopper` | a card link if the card has one, else the page URL | `url`, else `slot` |
| `eventbrite` | the event card's link | `url` |
| `meetup` | the event card's link | `url` |
| `fathom` | the film's page, **shared by every date of that film** | collides in the batch → dedup hash |
| plugins | whatever the plugin sets: `source_uid`, else `url` | as set |

*The rule as built.* Identity key = sha256(source name, kind, value): `uid` the source's own event id,
`url` the event URL (fragment and trailing slash dropped; not the source's own URL), `slot` start
time + normalised venue. The title is never in it. A re-scrape is matched on the identity key first
(and then takes the new start time and venue too, since the source says it is the same event), then
on the dedup hash as before (which still merges one event from two sources). Two events in one
scrape with the same key (a weekly series under one URL, a film's dates, two events in one slot) have
no usable identity and fall back to the dedup hash alone. `events.uid` is written once at insert
(`<identity key or dedup hash>@happenhere`) and never changes; rows stored before this item keep
`<dedup hash>@peakevents`, frozen before their dedup hash is ever rewritten, so an upgrade changes no
existing UID. The digest's `event_identity` is the same UID (old rows: the dedup hash, as before), so
an upgrade changes no digest fingerprint either.

**Still on a hash:** Fathom (every film with more than one date), any source that lists a series under
one URL, and any two events sharing a start time and venue in a source with no event URL. A retitle of
one of those still makes a new UID and a ghost. `spothopper` cards without a link use `slot`, so a
retitle is safe there; a time change keeps the UID but not the new time (item 15).

*Mutants* (each applied alone, then reverted; all three killed, the unmutated file passed 24/24):

    M1 identity key built from the title           → test_uid_is_stable_when_an_ical_event_is_retitled FAILED
    M2 feed UID = the title-based dedup hash       → test_uid_is_stable_when_an_ical_event_is_retitled FAILED
    M3 tag intersection matches ANY tag            → test_a_tag_intersection_needs_every_tag FAILED

*URL forms, as built:* `/feed/all.ics` (alias `/feed/events.ics`), `/feed/tags/<tag>[+<tag>...].ics`,
`/feed/venue/<source-name>.ics`, the page `/feeds`. `X-WR-CALNAME` `<site_name>` or
`<site_name> — music + downtown` / `— <source display name>`; `PRODID -//happenhere//happenhere//EN`.
New config: `site_name` (default "HappenHere"), `feed_lookahead_days` (365). `DTSTAMP` is the row's
`last_seen`, so an unchanged feed is byte-identical and its `ETag` holds between scrapes. Schema:
`events.identity_key`, `events.uid` (indexed) and `sources.tags`, added by `_migrate_schema` at
start-up. The `tags` seam in the import (item 05) is now live. Item 04 can add a tokened route beside
these on `_feed_response` without changing them.

### 04 — Personal feeds and an "artists I listen to" feed — NEEDS MEASUREMENT

A feed that only lists events whose performer matches artists the owner listens to. Inputs to
measure before building: a ListenBrainz user's top artists and similar artists (public API), and
an optional Subsonic-compatible server's starred artists. Personal feeds sit behind a long random
token per feed (rotatable one at a time), because calendar apps cannot send credentials. Measure:
how many artists the inputs give, how event titles name performers across source types, and the
match rate on a synthetic set before choosing a matcher.

### 05 — Source import/export — DONE

`python -m app.sources export > sources.yml` and `python -m app.sources import sources.yml
[--apply]` (dry run by default; prints the add/update/unchanged/would-disable plan). Keyed by
source `name`; fields: display name, type, URL, categories, tags (item 03 if landed, else ignored
with a warning), enabled. Import never deletes: a source missing from the file is reported, and
disabled only with `--disable-missing`. Every URL passes `netguard`. Round-trip test:
export → import is a no-op.

**Built:** `app/sources.py`, tests in `tests/test_sources_io.py`.

    python -m app.sources [--db PATH] export > sources.yml
    python -m app.sources [--db PATH] import FILE [--apply] [--disable-missing]

The file is `sources:` with one mapping per source (`name`, `type` required; `display_name`, `url`,
`categories`, `tags`, `enabled` optional). The whole file is validated first (known type, a name,
every URL through `netguard`, no unknown fields, no duplicate names) and a bad entry refuses the
import, each one named, exit status 1. The write is one transaction. The admin form and the
importer share `check_source_url` and `split_categories`. `tags` is ignored with one warning until
item 03 adds the field to the model (item 03 must then store it and replace
`test_tags_ignored_with_one_warning_without_model_field`). Finding: a URL that does not resolve
refuses the import, so an export of a database holding a source whose host has gone away does not
re-import until that entry is fixed; the same rule as saving it in the admin form.

### 06 — Per-source canary — READY

After each scrape run, a source with zero upcoming events for two consecutive runs, or a
non-zero `error_count` for three, raises one notification (the existing ntfy settings) naming it;
it re-arms when the source recovers. Shown in the admin source list too. Tests with a fake clock.

### 07 — Deprecations — READY

`datetime.utcnow()` (Python 3.12 deprecation; use aware UTC), `ORJSONResponse` (FastAPI
deprecation; drop the custom default response class, keep behaviour), and the authlib/Starlette
test-client warnings about `httpx` (pin-compatible fix or a tracked note if upstream has no
replacement yet). The test run must show no `DeprecationWarning` from this package.

### 08 — Release workflow — NEEDS DECISION

`release.yml` bumps `VERSION`, pushes a commit and a tag straight to `main` with a personal access
token, which a PR-only `main` refuses. **Question for the owner:** (a) drop releases (deploy from
`main` SHAs), (b) release from a tag pushed by hand, the workflow only building and publishing, or
(c) the workflow opens a version-bump PR and tags after it merges.

### 09 — Dependency bumps, low risk — DONE

The open Dependabot PRs that touch CI actions or patch-level libraries (not FastAPI, Starlette or
uvicorn: item 10). Each passed CI alone; together they edit the same files. One combined PR with
exactly the versions Dependabot proposed, after reading the release notes of every **major**
action bump (inputs, environment variables, permissions, runtime) and adjusting the workflow if a
note requires it. Local checks, a local `docker build`, and CI green on the combined head. The
superseded Dependabot PRs are closed with a pointer to the combined one. A workflow that only runs
on a tag or by hand (`release.yml`) is named in the PR as not exercised.

**Findings.** One combined PR with exactly the versions Dependabot proposed. Each source PR had all
four required checks green on its own head (Lint & Security, Tests, CodeQL, Docker Build & Scan;
Publish to GHCR skipped by its branch filter).

| Dependabot PR | File(s) | From → to |
|---|---|---|
| 1 | `ci.yml` | `gitleaks/gitleaks-action` v2 → v3 (major) |
| 2 | `requirements.txt` | `apscheduler` 3.11.2 → 3.11.3 |
| 3 | `requirements.txt` | `python-multipart` 0.0.32 → 0.0.33 |
| 4 | `pyproject.toml` | `setuptools` build requirement `>=82.0.1` → `>=84.2.1` |
| 7 | `ci.yml`, `release.yml` | `actions/checkout` v4 → v7 (major, 7 uses) |
| 8 | `ci.yml` | `hadolint/hadolint-action` 3.1.0 → 3.5.0 (minor) |

Release notes of the versions crossed, and what each means here (no workflow change was needed):

* **gitleaks-action v3:** runtime Node 20 → 24; "no changes to inputs, outputs, or behavior".
  GitHub drops Node 20 from hosted runners on 2026-09-16, so v2 would have stopped working. The
  workflow's `GITHUB_TOKEN` env and `pull-requests: read` permission are unchanged.
* **actions/checkout v5:** Node 24, runner ≥ 2.327.1. **v6:** `persist-credentials` writes the
  token to a file under `$RUNNER_TEMP` instead of `.git/config` (runner ≥ 2.329.0 for Docker
  container actions); plain `git` in later steps still works, which is all `release.yml` uses.
  **v7:** refuses a fork-PR checkout under `pull_request_target` and `workflow_run`, and v5.1/v6.1
  backport the `allow-unsafe-pr-checkout` input. Neither trigger exists in these workflows (`push`,
  `pull_request`, `workflow_dispatch` only) and the runners are GitHub-hosted `ubuntu-latest`.
* **hadolint-action 3.2 – 3.5:** only bumps the bundled Hadolint (to 2.15.1); inputs unchanged.
* **apscheduler 3.11.3, python-multipart 0.0.33, setuptools 84.2.1:** patch or build-requirement
  bumps; the image installs `requirements.txt` only, so the `setuptools` line affects no build here.

Local: ruff, black, bandit, pip-audit, gitleaks and pytest (158 passed) clean; `docker build` of the
branch succeeded and the image imports `apscheduler` 3.11.3 and `python-multipart` 0.0.33.
**`release.yml` (manual `workflow_dispatch` only) was not exercised**: its only change is the
`checkout` bump, and item 08 decides whether it survives at all.

### 10 — FastAPI and uvicorn — DONE

FastAPI's and uvicorn's Dependabot PRs together, because FastAPI moves Starlette, which is not
pinned here, and Starlette changes have broken every page before (item 02). Read the changelogs
between the current and proposed versions of FastAPI, Starlette and uvicorn; list each breaking or
deprecation note that touches code here. **Pin Starlette** to the version the new FastAPI
resolves, so it never moves silently again. The full test suite (every page has a render test),
the count of `DeprecationWarning`s before and after, and a smoke run of a scratch container built
from the branch (`AUTH_MODE=local`): `/health`, `/events`, `/events/calendar`, `/feed/events.ics`,
`/admin` redirect, a local sign-in round trip. Item 07 runs after this one (some of its
deprecations may change with the bump).

**Findings.** `fastapi` 0.136.3 → 0.162.0 and `uvicorn[standard]` 0.49.0 → 0.54.0 (exactly what
Dependabot proposed), with `starlette==1.10.0` now pinned in `requirements.txt`.

**Starlette did not move.** The old pins already resolved to Starlette 1.10.0 and 0.162.0
resolves to the same, so the pin changes nothing today; it stops the next FastAPI bump from
moving Starlette unseen. The only new package in the install is `opentelemetry-api` 1.45.2
(Apache-2.0 from its package metadata, 652 KB, requires only `typing-extensions`), a dependency of
the new FastAPI; the app configures no OpenTelemetry.

| Test run (`pytest -q tests/ -W default`) | Before (`origin/main`) | After |
|---|---|---|
| Result | 158 passed | 158 passed |
| Warnings | 5 | 5, the same five |

The five: `StarletteDeprecationWarning` (`httpx` with `starlette.testclient`, from
`fastapi/testclient.py`), `AuthlibDeprecationWarning` (`httpx`, from authlib),
`FastAPIDeprecationWarning` (`ORJSONResponse`, from `app/main.py`), and two `DeprecationWarning`s
for `datetime.utcnow()` (`app/ical_export.py`). None became an error, none was added or removed;
all belong to item 07.

Release notes read for every FastAPI version in (0.136.3, 0.162.0] (67 releases) and uvicorn in
(0.49.0, 0.54.0] (11). What touches code here:

| Change | Version | Here |
|---|---|---|
| `router.routes` becomes a tree of router and route objects | FastAPI 0.137.0 | No code reads `router.routes`; the app uses `include_router` twice. Not affected. |
| Omitted `bool` form fields inferred `False` (0.147), then reverted to the declared default (0.162); empty scalars in form *models* count as missing (0.149); empty file inputs count as missing (0.150) | 0.147 – 0.162 | Not affected: every `Form(...)` here is `str` or `int` (checkbox fields are `str = Form("off")`), no form models, no files. The smoke run posted the settings form with the checkboxes omitted: 302 saved. |
| Defaults of omitted `Query`/`Form` parameters are not validated | 0.148 | Not affected: defaults are plain, valid values. |
| JSON `null` body distinct from an omitted body; header/query/cookie parameter models; `**extra` removed from `Query`/`Form`/etc.; `openapi_prefix`/`**extra` removed from `FastAPI()`; `root_path` parameter deprecated | 0.145 – 0.160 | None used (`grep` for each API in `app/`). |
| OpenTelemetry exporter setup is opt-in | 0.143.0 | Not used. |
| `ORJSONResponse` deprecated | already in 0.136.3 | Still deprecated in 0.162.0, not removed; behaviour unchanged. Item 07's to fix. |
| uvicorn: legacy `websockets` implementation deprecated, `auto` defaults to `websockets-sansio` (0.50); `colorama` dropped from `[standard]` (0.51); experimental `zttp`/HTTP/2 opt-in (0.52 – 0.54); `::1` trusted by default for forwarded headers (0.53) | 0.50 – 0.54 | No WebSocket endpoint, no `--http`/`--loop` flag (the `CMD` uses `--proxy-headers` only). The `::1` default adds the IPv6 loopback to the peers whose `X-Forwarded-*` are trusted; the `CMD` sets no `--forwarded-allow-ips`, so only loopback peers qualify either way. |

**Smoke run.** A scratch container from a local build of the branch (`AUTH_MODE=local`, random
password, anonymous data volume, no published port, `--memory 1g`), driven from inside the container:

* `/health` 200 (`application/json`), `/events` 200, `/events/calendar` 200, `/feed/events.ics` 200
  `text/calendar; charset=utf-8`.
* `/admin` without a session: 302 to `/login?next=/admin`. `POST /login` with a wrong password 401;
  with the right one 302 to `/` with a session cookie; with the cookie `/admin`, `/admin/settings`,
  `/admin/sources/new` and `/admin/digest` all 200; `POST /admin/settings` 302 `?saved=1`.
* `docker logs`: real lifespan started (database, scheduler, digest slot), no traceback and no
  error line; the only warnings are the authlib and `ORJSONResponse` deprecations above.

Local: ruff, black, bandit, pip-audit (no known vulnerabilities), gitleaks clean.

### 11 — Facebook and Instagram; flyers through a vision model — NEEDS DECISION

Many small venues announce shows only on Facebook or Instagram. Before any code, a session
establishes, with dated evidence and the fewest possible requests:

1. **Facebook:** what a logged-out client gets from a public Page's events (page and mobile
   variants), what the Graph API requires for Page events today (permissions, app review, which
   Pages), and whether any calendar export exists for events a user follows. Scraping behind a login
   is out: this project does not hold anyone's social-media credentials.
2. **Instagram:** what a logged-out client gets from a public profile; whether posts carry event
   data or only images.
3. **Flyers through a vision model:** on a set of real event flyers (public posts saved by URL,
   kept outside the repo) and their hand-written truth (title, date, start time, venue, price),
   measure each candidate local vision model (an Ollama-compatible endpoint, configurable) for field
   accuracy, wrong-date rate and time per flyer. A wrong date is worse than a missing event: report it
   separately.

Decide after the numbers: build (which path), or record why not. Whatever is built takes its
endpoint and model from config and never assumes one host.

**Measured 2026-11-04** ([`measurements/11-facebook-instagram-vision.md`](measurements/11-facebook-instagram-vision.md)):

* **Facebook:** logged out, a Page's events tab answers an error page (desktop) or a redirect to
  login (mobile). Graph API access to Page events is restricted to whitelisted partners, and Meta's
  terms bar automated collection outside the APIs. The personal "upcoming events" iCal export
  carries a secret key, which makes it a credential, and it was not tested. **No sanctioned path.**
* **Instagram:** logged out, a profile is a JavaScript shell with no posts. Business Discovery needs
  an approved Meta app (Advanced Access, Business Verification), and its posts are an image plus a
  caption, with **no event data.**
* **Flyers:** 17 public images. The one installed vision model (`gemma3:12b-it-qat`) reads titles
  (13/17) at about 5 s per flyer. Dates fail both ways. Without "today" in the prompt, 4 of 5
  printed dates got the wrong year. With "today" and "do not guess", 8 of 12 undated images got
  today's date. Venue and price are unreliable. Unattended, it would publish events on the wrong day.

**Decision needed (owner):**

1. Record Facebook and Instagram as automated sources **won't-do**: no sanctioned path, per the
   evidence above. *Recommended.*
2. Then choose one:
   * (a) close item 11 as **DONE-WON'T-DO**; or
   * (b) build a **human-confirmed flyer import**. A session can run it as written:
     * The admin uploads an image or pastes a public image URL (fetched through `netguard`).
     * The configured Ollama-compatible endpoint and model (`vision_endpoint`, `vision_model`;
       unset hides the feature) fill a **draft** event form.
     * The draft is never published by itself. The date and start-time fields start empty unless
       the model's value is shown beside them as a suggestion, and an admin must enter or confirm
       both before saving.
     * Tests mock the endpoint, and a render test covers the new page.

   (b) is worth building only if the admin will actually paste flyers. It adds no unattended source.

### 12 — Ticketing platforms as built-in source options — READY

Make the common ticketing platforms first-class source types, offered in the admin "add source"
form and recognised by auto-detection from a pasted URL: **Eventbrite** (today only a regional
search scraper), **DICE**, **Bandsintown**, **See Tickets** and **Etix**. Per platform, measure
first: how a venue's or organizer's event list is reachable (public API and its key and terms,
JSON-LD on venue pages, an iCal export, or JS-rendered only), what a request returns for a test
venue, and the rate limits. Then build one source type per platform that has a sanctioned or
structured path, each with saved-fixture tests (no live site in CI) and a detection rule; a platform
with no such path is recorded as such, not scraped around.

**Measured 2026-11-04** (`docs/measurements/12-ticketing-platforms.md`, with Ticketmaster and AXS
added for comparison). Only **Ticketmaster** has a sanctioned path that a self-hosted deployment can
use, through Discovery's `venueId`. **DICE, Bandsintown, See Tickets, Etix and AXS** have no open
path: their APIs are partner-only or artist-only, or their terms forbid automated reading, and
every one except DICE refuses a plain HTTP client (403 or a WAF challenge). Each is recorded, and
none gets a type. **Eventbrite** is a decision, item 16. The measurement also found two gaps in the
generic JSON-LD reader, which this item fixes:
* G1: Event subtypes such as `EducationEvent` are dropped.
* G2: events nested in `Place.event` are dropped. A DICE venue page lists 30 events that way and
  the reader returns 0.

**Spec:** the measurement doc's "Build spec" section, in four parts:
1. Generic JSON-LD: G1 and G2, de-duplicated, with fixtures.
2. A built-in `ticketmaster_venue` type:
   * pasted URL: a Discovery `venueId` URL, or a ticketmaster.com `/venue/<n>` page resolved once
     through `venues.json` by its `url`;
   * the key stays in config;
   * the paging limit is 1000 items;
   * the field mapping is given in the doc;
   * UID identity: `ticketmaster:<event id>`.
3. Recognition in `discovery.py` of the platforms that cannot be sources, by host and before any
   request, with a note telling the admin to add the venue's own site.
4. No types for the five not-buildable platforms.

### 13 — Local scraper plugins — DONE

**Why:** some venues need a scraper written for their one site. That code is about a particular
place, so it does not belong in this generic repository, yet a deployment needs it. A deployment
keeps such scrapers in its own directory and the app loads them as extra source types.

**Spec:**
1. `plugins_dir` in config (env `PLUGINS_DIR`); unset means no plugins.
2. At start-up every `*.py` directly in it is imported (as `happenhere_plugins.<stem>`). Each
   `BaseScraper` subclass defined there with a `SOURCE_TYPE` is registered under that type, with
   `TYPE_LABEL` (or the class name) as its admin label. Same constructor contract as the built-ins
   (`config`, `source_url`, `venue_name`).
3. **Failure never stops the app:** a module that fails to import is skipped and logged at ERROR
   with its traceback; a `SOURCE_TYPE` that collides with a built-in or another plugin is refused and
   logged. The admin page lists loaded plugins and load errors.
4. The plugin API is `app.scrapers.base` (`BaseScraper`, `EventData`, `DEFAULT_HEADERS`) and the
   shared bases (`_html_base`, `_ical_base`, `_playwright_base`); document it in `CLAUDE.md`.
5. **Security:** a plugin is code running with the app's privileges. It is loaded only from the
   configured directory, never from the database, an upload or a URL. Document that the directory
   must be writable only by the operator, and mount it read-only.
6. Detection (`app/discovery.py`) is unchanged: a plugin type is chosen by hand in the source form.

**Tests** (a temp plugin dir): a good plugin registers and scrapes a saved fixture; a module with a
syntax error is skipped and logged while the app starts; a colliding type is refused; no
`PLUGINS_DIR` loads nothing; the admin page renders the plugin list.

**Built:** `app/plugins.py` (`load_plugins`, `unload`, `get_report`), `register_type` /
`unregister_type` in `app/scrapers/registry.py`, `plugins_dir` / `PLUGINS_DIR` in `app/config.py`,
loaded in the lifespan before the database and the scheduler start and by `python -m app.sources`
(so a plugin's type is valid in a `sources.yml`), a "Plugins" panel on `/admin`; tests in
`tests/test_plugins.py`. The API and the security rule are in `CLAUDE.md`.

Choices the spec left open: a class registers only if it is defined in the plugin module (not
imported into it), is not abstract and sets `SOURCE_TYPE` in its own body (a subclass of a built-in
base does not inherit the base's type); `SOURCE_TYPE` must match `[a-z0-9][a-z0-9_-]*`; on a
collision the first module in file-name order wins; `SystemExit` raised by a plugin at import is
caught like any other failure; a missing `PLUGINS_DIR` is reported as an error on the admin page, not
fatal; plugin files named `_*.py` load like any other. Found while building: the `POST
/admin/scrape/{name}` handler renders `admin.html` with its own context and would have crashed on the
new panel; it now passes the plugin report and has a render test.

**Mutant:** with the collision check removed from the loader (and from `register_type`), the
built-in-collision and plugin-collision tests fail, as does the inherited-type test (the mutant also
leaves the registry damaged for later tests).

### 14 — Configurable digest that never repeats itself — DONE

**Why:** today's digest runs daily, decides "new" from a per-event `notified` flag, and its title
hard-codes one city. The owner wants a digest on a configurable interval that **never sends when
nothing new has appeared since the last one it sent**.

**The rule, in the owner's example:** a digest goes out Monday. At the next interval (Thursday)
the list has not changed: nothing is sent. At the following interval (Sunday) there are new events:
it is sent, as scheduled. A skipped run does not move the schedule.

**Spec:**
1. **Settings in the admin UI** (persisted like the others): `digest_enabled` (default on; it does
   nothing until ntfy is configured), `digest_interval_days` (default **3**), `digest_time` (HH:MM,
   default 09:00, in the configured timezone), `digest_window_days` (events starting within the next
   N days, default 14). Saving reschedules the job without a restart.
2. **Schedule:** one job every `digest_interval_days` at `digest_time`, anchored on the last
   *scheduled* run (stored), not on the last *sent* one.
3. **Content:** the upcoming events in the window, sorted by start; at most 20 lines, then "… and
   N more"; a title from the configured site name ("<site> — N events in the next D days"), never a
   hard-coded place; a link to the public events page.
4. **The change gate:** each digest entry has a fingerprint (the event's stable identity; title,
   start time and venue). A run **sends only if the new digest has at least one entry whose
   fingerprint was not in the last SENT digest** (a new event, or a changed title, time or venue).
   Removals alone (events passed or cancelled) never trigger a send. The first run after enabling
   sends. A failed post is not recorded as sent, so the next run retries.
5. Every run writes a `NotificationLog` row: `sent`, `skipped-unchanged`, `skipped-empty`,
   `failed`. Retire the per-event `notified` flag from the decision (keep the column if dropping it
   needs a migration).
6. **Admin page:** "Preview" renders the next digest and says whether it would send and why; "Send
   test" sends it marked TEST and records nothing.

**Tests** (fake clock): the owner's Monday/Thursday/Sunday example exactly; removal-only does not
send; a changed start time sends; first run sends; a failed post retries next run; changing the
interval reschedules; no ntfy settings means no send and no error; the title names no place.

**Built.** `app/digest.py` (schedule, content, gate, preview, test send), `app/ntfy.py` (transport
only), `scheduler.schedule_digest()`, the Digest card on Settings and `/admin/digest` (Preview and
Send test). Tests: `tests/test_digest.py`.

* **Settings** (admin UI only; of these only the older `DIGEST_TIME` is also read from the
  environment, and it wins at every start): `digest_enabled` (default on), `digest_interval_days`
  (3, clamped 1-30), `digest_time` (09:00; a value that is not HH:MM is refused and the old one kept),
  `digest_window_days` (14, clamped 1-365). Saving reschedules the job without a restart.
* **Schedule:** one date job, re-armed after every run. The anchor (`AppMeta` `digest_last_scheduled`)
  is written for every scheduled run, sent or skipped. Slots are anchor + whole intervals at the
  wall-clock `digest_time` in the configured timezone (stable across a DST change). A slot that
  passed while the app was down is skipped, not run late, and the phase is kept. With no anchor the
  first slot is the next `digest_time`.
* **Gate:** the last SENT digest's fingerprints are stored (`digest_last_sent_fingerprints`), written
  only after a successful post. A fingerprint is a hash of `event_identity(ev)` (the feed UID since
  item 03; the dedup hash for a row stored before it), title, start time and venue. Sends iff at
  least one entry is not in that set, or nothing has ever been sent. All entries are fingerprinted,
  not just the 20 listed.
* **`NotificationLog.status`:** `sent`, `skipped-unchanged`, `skipped-empty`, `failed`. "ntfy not
  configured" and "digest off" write no row, so enabling ntfy later still sends the first digest.
  `Event.notified` is no longer read or written by the digest (the column stays).
* **Transport:** the digest is published to ntfy as JSON. The old `Title:` header carried an em dash,
  and httpx refuses a non-ASCII header value, so a title like "<site> — N events" could not have
  been sent that way.
* **Title:** `<city_name or "HappenHere"> — N events in the next D days`. No place is named in code.
* **Mutants** (run against the finished tests; each reverted):
  * gate against the last RUN (the fingerprints stored after every run, sent or not) fails
    `test_a_failed_new_event_on_thursday_is_retried_on_sunday` (the Sunday step: C was new on
    Thursday, its post failed, and the mutant then sees nothing new on Sunday),
    `test_an_event_that_drops_out_and_comes_back_is_not_new` and
    `test_a_failed_post_is_not_recorded_as_sent_and_the_next_run_retries`: 3 failed, 31 passed.
    The plain Monday/Thursday/Sunday test passes under this mutant, because its Sunday has a new
    event whichever digest it is compared with; the two variants above are what separate them.
  * gate that fires on any difference, removals included, fails `test_removal_only_does_not_send`,
    `test_passed_events_alone_do_not_send` and the drop-out test: 3 failed, 31 passed.

### 15 — A re-scraped event keeps its first start time — READY

**Found by item 14, not fixed there.** `upsert_events` (`app/dedup.py`) refreshes title,
description, end time, address, URL and image on an event it has seen, but never `start_dt` or
`venue_name`. The dedup hash is title + date + venue, so a venue that moves an event's start time
within the same day (7 pm to 9 pm) updates nothing: the public page, the ICS feed and the digest
all keep the old time. Reproduced with two `upsert_events` calls on a synthetic event: the second
call leaves `start_dt` at 19:00. **Spec:** update `start_dt` on a seen event, and let item 03's
stable UID decide what counts as "the same event" first (the feed UID must not change when a time
does). **Test:** the reproduction above, plus a feed test that the UID is unchanged by the edit.

*Since item 03:* an event matched on its identity key (a source UID or event URL) already takes the
new `start_dt` and `venue_name` (`test_a_moved_event_keeps_its_uid_and_its_new_time`). What is left
is the dedup-hash match: a `slot`-identity event whose time moves within the day (its key changes,
the hash matches), and a hash-fallback event. The reproduction above (no URL, no UID) still holds.

*Since item 25:* the rule "a dedup-hash match is a different event" lives in one function,
`app/dedup.py` `is_different_event(row, key, source_id, batch_keys)`; `batch_keys` is the
frozenset of identity keys the batch returned (a key shared within the batch excluded). Today a
same-source match with a different, non-null key is a new row under `timed_hash`, and the old row
retires when the scrape no longer returns it. Item 15's "only the time moved" case is the one where
`row.identity_key not in batch_keys`: that is where an in-place update replaces the new row.

### 16 — Eventbrite: the regional scraper against the terms; a sanctioned path — NEEDS DECISION

**Found by item 12.** Eventbrite's Terms of Service 13.1 (updated 2025-08-20) say: *"you agree not to
scrape, crawl, or employ any automated means to extract data from the Sites"*. The built-in
`eventbrite` type (`app/scrapers/eventbrite.py`) is a Playwright scraper of
`eventbrite.com/d/<region>/events/`, so it does exactly that. There are two sanctioned paths
(`docs/measurements/12-ticketing-platforms.md`):
* **API v3 with a free private token:** `GET /v3/venues/{id}/events/` or
  `GET /v3/organizations/{id}/events/`. Whether the token reads public events of venues and
  organizations it does not own is unmeasured. API Terms 4.2 requires the consent of each user whose
  content is accessed, and the 2019 changelog points multi-creator readers to the distribution
  partner program. Display rules: title plus a direct link, no past events stored.
* **The distribution partner program**, for which an operator applies.

**Decisions:**
1. Remove the `eventbrite` type, or keep it disabled with a warning in the admin form?
2. Should someone with an Eventbrite account create a private token, so a session can measure
   whether `venues/{id}/events/` returns events the token does not own? If it does, an
   `eventbrite_venue` type follows the `ticketmaster_venue` pattern in item 12.
3. Apply to the partner program, or not?

### 17 — Hard-coded `America/Denver` timezone — READY

**Found by item 12, not fixed there.** Four places convert or emit times in a fixed zone instead of
the configured one:
* `_parse_dt` in `app/scrapers/_html_base.py`, which serves every JSON-LD source;
* `app/scrapers/_ical_base.py`;
* `app/scrapers/ticketmaster.py`;
* `_TZ_NAME` in `app/ical_export.py`.

`app/scheduler.py` defaults `TZ` to it as well. Measured: a London venue's JSON-LD
`startDate` `2026-11-05T19:00:00+00:00` parses to `12:00`. A deployment in any other zone gets
every offset-carrying time shifted, and its feed labelled with the wrong zone. **Spec:** one helper
returns the configured zone: `cfg.tz` through `tz_of` in `app/digest.py`, which falls back to `UTC`
for an unknown zone. All five places use it. **Tests:** a synthetic JSON-LD event with a `+00:00` offset parses to
local wall-clock time in a configured zone that is not UTC. The ICS `DTSTART` carries the configured
`TZID`.

### 18 — One source's duplicate batch aborts the whole scrape run — DONE

**Found in a deployment (2026-11-04), not fixed there.** Two shows with the same title on the same
day at the same venue (an early and a late show, common at comedy clubs) share the dedup hash
(title + date + venue). `upsert_events` (`app/dedup.py`) looks the hash up in the database only, and
the session has `autoflush=False`, so the second event of the batch does not see the first: both are
inserted and the commit fails with `UNIQUE constraint failed: events.dedup_hash`. `run_all_scrapers`
(`app/jobs.py`) then catches it and calls `session.commit()` on the failed session **without
`rollback()`**, which raises `PendingRollbackError` out of the `except`: the loop ends, every later
source is skipped, and the run reports nothing. Measured: a scheduled-style run stored 0 events after
3 s. `run_single_scraper` has the same `except`.

**Spec:**
1. In one batch, a second event with a hash already taken is the **same event** when its identity
   key (item 03) is the same or absent (merge: refresh the first row), and a **different event**
   when its identity key differs (the late show: stored as its own row under a hash that adds the
   start time, so it never collides with the first).
2. Every `except` around a source's scrape calls `session.rollback()` before recording the error, so
   one failing source costs only its own events; the run goes on to the next source.

**Tests (fail first):** a source returning two same-title same-day shows with different URLs, then a
second source: both shows and the second source's event are stored, and the run completes. A source
whose `upsert_events` raises mid-batch is recorded with its error, the next source still scrapes.

**Findings (item 18 session).**

*Failing first.* `tests/test_scrape_run.py` (a fake scraper type registered for the test) on `main`
before this change: all 5 fail, with the deployment's errors: `UNIQUE constraint failed:
events.dedup_hash`, then `PendingRollbackError` raised out of `run_all_scrapers`' `except`, so the
next source never ran; `run_single_scraper` raised instead of returning its error. Item 03 did not fix
it: its `taken_hashes` guarded only the identity-key path, not the hash path.

*As built.* `upsert_events` keeps the batch's rows by dedup hash. A second event with a taken hash is
the same event when its identity key is the same or absent (refreshed, counted as updated), and a
different event when its key differs: stored under `timed_hash` (title + start time + venue), so the
late show is its own row, its UID comes from its own identity key, and a second run matches both by
key (`test_both_shows_survive_a_second_run_unchanged`). Both `except` blocks in `app/jobs.py` call
`session.rollback()` first: a failing source loses only its own batch, its error is recorded, the
run goes on.

*Mutants* (each alone, then reverted; all killed): M1 no `rollback()` → the two failing-source tests
fail; M2 no batch lookup → the three duplicate tests fail; M3 distinct keys merged → the late show is
lost, two tests fail.

*Left as it was:* a late show that first appears in a **later** run, when the early show's row is
already stored, still matches the early row by dedup hash (the cross-source merge path) and is folded
into it. Telling those apart needs the stored row's source and key compared on that path; the same
question as item 15's hash-fallback match. A plugin-side guard that keeps only the first event per
hash (a workaround some deployments added for this item) can be dropped.

### 19 — `ical` and `spothopper` sources all fail on `SOURCE_NAME` — DONE

**Found in a deployment (2026-11-04).** Every source of type `ical` or `spothopper` fails with
`'GenericICalScraper' / 'GenericSpotHopperScraper' object has no attribute 'SOURCE_NAME'`: the bases
(`app/scrapers/_ical_base.py`, `_spothopper_base.py`) log `self.SOURCE_NAME`, `BaseScraper` only
annotates it, and the generic subclasses never set it. The log call's arguments are evaluated even at
`DEBUG`, so a feed that parsed fine still raises after parsing, and the source's events are lost.
**Spec:** `BaseScraper` gives every scraper a `SOURCE_NAME` (default empty), and the generic
URL-driven scrapers set it to the source's display name, else its URL, so their log lines say which
source spoke. **Tests (fail first):** a synthetic iCal feed and a synthetic SpotHopper page (mocked
transport) each return their future event through the generic scraper.

**Findings (item 19 session).** Failing first, `tests/test_generic_scrapers.py` on `main` before this
change: both generic scrapers raised exactly the deployment's error (`'GenericICalScraper' object has
no attribute 'SOURCE_NAME'`, the same for `GenericSpotHopperScraper`), and the class check found
`jsonld` without one too (it never logged it, so it did not fail). As built: `BaseScraper.SOURCE_NAME`
defaults to `""`, and all five generic URL-driven scrapers set it to the display name, else the URL.
3 tests, all failing before, passing after. A deployment that disabled its `ical`/`spothopper`
sources over this error can re-enable them.

### 20 — Orphaned Chromium never reaped while uvicorn is PID 1 — DONE

**Found in a deployment (2026-11-06).** The image starts uvicorn as the container's PID 1, and
uvicorn does not reap children it did not start. Chromium's helper processes that outlive their
parent (a crashed renderer, a browser killed on timeout) are re-parented to PID 1 and stay as
zombies: 30 `chrome-headless-shell` zombies after two days of scheduled scrapes. Item 01 closed every
browser the app opens; this is the processes Chromium itself leaves. **Spec:** run an init as PID 1:
`init: true` in the example `docker-compose.yml`, the same requirement written next to the image's
`CMD` and in `CLAUDE.md`'s deployment notes (`docker run --init` for a plain run). **Test (fails
first):** the example compose file sets `init: true` for the service.

**Findings (item 20 session).** `tests/test_deploy_example.py` fails on `main` before this change
(the example compose had no `init`) and passes after. As built: `init: true` in the example
`docker-compose.yml`, the requirement next to the `Dockerfile`'s `CMD` and under "Deployment" in
`CLAUDE.md`. Measured in the deployment that found it: after recreating the container with
`init: true` (Docker's `tini` as PID 1, uvicorn its child), 0 zombies, against 30 before. The image
itself still starts uvicorn directly: a deployment that drops `init` gets the zombies back, which is
why the requirement is written in three places rather than only in compose.

### 21 — Secrets in logs: API keys in request lines and error text — DONE

**Found in a deployment (2026-11-06).** `httpx` logs every request at `INFO` as `HTTP Request: GET
<full URL>`, and the Ticketmaster Discovery API takes its key in the query string (`?apikey=...`),
so the key was written to the container log once per request (273 lines in two days). The same URL
is in `httpx.HTTPStatusError`'s text, which `run_all_scrapers` writes to the log with a traceback and
stores in `sources.last_error`, shown in the admin UI. Any plugin that passes a key in a query string
leaks the same way. **Spec:**
1. `httpx` and `httpcore` loggers at `WARNING` (a per-request line is noise at `INFO`).
2. One redaction step for every log record, whichever handler writes it, and for the stored
   `last_error`: the value of a query parameter named `apikey`, `api_key`, `key`, `token`,
   `access_token`, `client_secret` (case-insensitive) becomes `REDACTED`.
3. Logging is configured in one function that the app's start-up calls.

**Tests (fail first):** with logging configured as the app configures it and a mocked Ticketmaster
response of `401`, a scrape run's log output (messages and tracebacks) and the source's
`last_error` do not contain the key; the redaction unit-tests each parameter name and leaves other
parameters alone.

**Findings (item 21 session).**

*Failing first.* The same scrape on `main` before this change (logging as `lifespan` set it up, a
mocked `401` from the Discovery API, a synthetic key): **3 log lines held the key** (httpx's
`HTTP Request: GET ...?apikey=<key>&...` at `INFO`, and the `Scraper tm failed: Client error '401
Unauthorized' for url '...apikey=<key>...'` line with its traceback), and `sources.last_error` held it
too. `tests/test_log_redaction.py` fails on that code (no `configure_logging`), passes after.

*As built.* `app/logredact.py`: `redact(text)` replaces the value of `apikey`, `api_key`, `key`,
`token`, `access_token`, `client_secret` after `?`, `&` or `;` (case-insensitive; `monkey=`,
`tokens=` and prose like `key = value` are untouched). `install()` wraps the log-record factory, so
**every record is redacted at creation**, whatever logger made it and whatever handler writes it
(uvicorn's, a test's, a plugin's): the message, each argument whose text holds a secret (an
`httpx.URL` included), and the traceback text (`exc_text`, which formatters use instead of rendering
`exc_info` again). Arguments keep their shape, because uvicorn's access formatter unpacks
`record.args` (`test_access_log_args_keep_their_shape`). `app.main.configure_logging(cfg)` installs
it, calls `basicConfig` as before and sets `httpx`/`httpcore` to `WARNING`; `lifespan` and the import
CLI call it. `run_all_scrapers`/`run_single_scraper` store `redact(str(exc))` in `last_error`.

*Mutants* (each alone, then reverted): no `install()` in `configure_logging` → 3 tests fail;
`last_error = str(exc)` → `test_a_failed_request_leaves_no_key_in_the_log_or_the_stored_error` fails.

*Not covered:* a secret in a request **header** never reaches a log line here (ntfy's token is a
header; httpx does not log headers). A deployment upgrading from before this item still has old log
lines and old `last_error` values with the key until the log rotates and the source scrapes again.

### 22 — Ticketmaster regional search ignores the radius — READY

**Found in a deployment (2026-11-04).** The built-in `ticketmaster` type sends `postalCode` +
`radius` to the Discovery API. Measured: with `postalCode` the API returns only events at venues
**in that ZIP** and ignores `radius` (one ZIP at 1, 25 and 75 miles: the same 81 events each; a
ZIP with no venue: 0 at any radius), so `city_radius_miles` does nothing. A real area search
(`latlong` + `radius`) works, but a 25-mile metro search returned 1,822 events, over the API's
paging cap of 1,000 results (`size` × `page` < 1000), so it silently loses the tail. A deployment
covering two cities needs two searches, which one ZIP setting cannot express.

**Decision needed (owner):**
1. (a) Rebuild the regional type on `latlong` + `radius` (a geocoded point in config) and page
   past the cap by splitting the date range into windows that each stay under 1,000; (b) make
   the location per source (its URL carries `latlong`/`radius` or `postalCode`), so a deployment
   adds one regional source per area; (c) drop the regional type in favour of item 12's
   per-venue `ticketmaster_venue` type and document why. *Recommended: (b), with (a)'s date
   windows, since one config value cannot cover several areas.*
2. Whatever is chosen: a source that hits the cap logs it and records it on the source, never
   silently.

**Decided (owner, 2026-11-06): (b) with (a)'s date windows.** Spec:
1. The regional `ticketmaster` source carries its own location: its URL holds `latlong=<lat>,<lon>`
   and `radius` (miles); a source without them is refused on save and on import with a message
   naming the fields. `postalCode` and the global `city_zip`/`city_radius_miles` are no longer
   used by this type (other uses of them unchanged). Two areas = two sources.
2. Paging past the cap: the search runs over date windows (start with the look-ahead split into
   windows; a window whose `page.totalElements` is over 1,000 is halved and re-run) until every
   window is under the cap; events are de-duplicated by Ticketmaster id across windows.
3. A window that still hits the cap at one day logs a warning and records it on the source
   (`last_error`), never silently.
4. The key stays in config; item 21's redaction covers the request lines.

**Tests:** saved-fixture pages through a mocked transport; the window split covers the range
exactly once with no gap or overlap; a window over the cap is halved; a second source with
another location does not change the first; a source without `latlong` is refused.

### 23 — JSON-LD events under a `Place`'s `Events` key — READY

**Found in a deployment (2026-11-04).** A ticketing platform (Seat Engine) publishes one JSON-LD
`Place` whose capitalised `Events` key lists every event. `extract_jsonld_events`
(`app/scrapers/_html_base.py`) reads top-level items and `@graph` only, so URL detection reports no
events and the `jsonld` scraper returns 0; the deployment works around it with a private plugin.
This is item 12's gap G2 (`Place.event`) with one more key spelling. **Spec:** when an item is a
`Place` (or a subtype), take the events under `event`, `events` and `Events` (a list or a single
object), each kept only if it is an Event type (item 12's G1 subtypes included), de-duplicated
with top-level events; whichever of items 12 and 23 runs first does both spellings, and the other
records that. **Tests:** synthetic fixtures for each key spelling, a single object and a list;
detection reports the type `jsonld` and the count; a page with the same event both top-level and
nested yields it once.

### 24 — Import: a disabled entry with a refused URL refuses the whole file — READY

**Found in a deployment (2026-11-04).** `python -m app.sources import` checks every entry's URL
through `netguard`, enabled or not, and one refusal refuses the file (item 05's rule). A source
kept disabled *because* its host does not resolve publicly from the deployment (a DNS filter, a
dead domain) cannot be written down in the file at all, so its blocker is kept as a comment
instead, out of the import's reach. (Item 05's other import finding from the same run, `tags`
reading as a change on every import, is gone since item 03 stored tags: the same file now imports
as 91 unchanged.)

**Decision needed (owner):** (a) keep the rule (a refused URL is never stored), document the
comment workaround; (b) a **disabled** entry whose URL is refused is stored disabled with the
refusal as its `last_error` and a warning in the plan, and enabling it (admin form or a later
import) re-runs the check and refuses there; (c) skip the check for disabled entries without
recording anything. *Recommended: (b): the file stays the whole truth, and nothing unchecked is
ever fetched.* **Decided (owner, 2026-11-06): (b).** Spec:
1. Import: an entry with `enabled: false` whose URL the guard refuses is stored disabled; the
   refusal is written to its `last_error` and shown in the plan as a warning (not an error). An
   **enabled** entry with a refused URL still refuses the file, as now.
2. Enabling a source (admin form toggle or edit, or a later import with `enabled: true`) re-runs
   the guard on its URL and refuses the change if it still fails, with the reason shown.
3. The scraper never fetches a disabled source (unchanged), so nothing unchecked is fetched.
4. Export writes the entry like any other (round trip stays a no-op).

**Tests:** a disabled entry with a refused URL imports (stored, disabled, `last_error` set,
warning in the plan); the same entry enabled refuses the file; enabling the stored source in the
admin form and through an import both re-check and refuse; export → import is a no-op.

### 25 — A same-title show first seen in a later run gets its own row; vanished rows retire — DONE

**Found by item 18, left there.** Item 18 keeps two same-title shows of one day apart **within one
batch** (different identity keys → the second gets `timed_hash`). If the late show appears only in
a **later** run (added after the early show was stored), `upsert_events` finds no row by its
identity key, then finds the early show's row by dedup hash (title + date + venue) and refreshes
it: the late show never gets a row, and the early row takes the late show's title and URL (its
start time stays, item 15). A show added later in the week at a comedy club is exactly this.

**Decision needed (owner):** on the dedup-hash match, when the stored row is from the **same
source** and carries a **different, non-null identity key**, (a) treat it as a different event
and store the new one under `timed_hash` (risk: a source that changes an event's URL leaves the
old row as a ghost until its date passes), or (b) as (a), and also retire a same-source row that
a later full scrape no longer returns (needs "seen in this run" tracking; overlaps item 15's
hash-fallback match), or (c) keep merging. *Recommended: (b); (a) alone if (b) waits on item 15.*
Cross-source matches (the same event from two sources) keep merging either way.

**Decided (owner, 2026-11-06): (b).** Spec:
1. On the dedup-hash match in `upsert_events`, when the stored row has the **same source** and a
   **different, non-null identity key**, the new event is a different event: stored under
   `timed_hash` with its own UID. Cross-source matches keep merging.
2. **Retiring:** after a source's scrape **succeeds and returns at least one event**, its upcoming
   rows (start in the future) that this scrape did not return are marked retired (a new nullable
   `retired_at`; `_migrate_schema`), not deleted. Feeds, the events page, the API and the digest
   skip retired rows. A retired row that a later scrape returns again is un-retired (same UID).
   A failed scrape or one that returns 0 events retires nothing (item 06's canary owns that case).
3. Matching for retirement uses the same identity as the upsert (identity key, else dedup hash),
   so an unchanged event is never retired.

**Overlap with item 15** (a re-scraped event keeps its first start time): 25(b) covers 15's
remaining cases in effect but **does not subsume it**. A `slot`-identity event whose time moves
within the day gets a new key, so under 25 it becomes a new row with a **new UID** and the old row
is retired: the time is right, but a calendar app sees a deletion and a new event, which item 15's
spec forbids (the feed UID must not change when a time does). Hash-fallback events (no usable key)
still merge as before. Item 15 stays READY for that; whichever of 15 and 25 runs second rebases on
the other, and 15 should prefer "same source, same title and day, only the time moved, nothing
else matched" as an update over 25's new row.

**Tests:** run 1 stores the early show; run 2 returns early + late: two rows, the early row
unchanged; run 3 returns only the late show: the early row is retired and leaves the feed; run 4
returns it again: un-retired, same UID; a failed scrape and a 0-event scrape retire nothing; a
cross-source duplicate still merges.

**Built.**
* **The rule, in one place:** `app/dedup.py` `is_different_event(row, key, source_id, batch_keys)`.
  `upsert_events` asks it on every dedup-hash match with a stored row; on "different" the event is
  looked up and stored under `timed_hash` with its own UID (the rule item 18 already applied within
  one batch). `batch_keys` (every identity key the batch returned, shared keys excluded) is passed
  for item 15 and not read here.
* **Retiring:** `Event.retired_at` (nullable; `_migrate_schema` adds it and logs
  `Schema: added events.retired_at`). `app/jobs.py` `_store` runs both scrape paths (the scheduled
  run and the admin's single-source scrape): after a scrape with at least one event,
  `dedup.retire_unreturned` sets `retired_at` on the source's rows that start after the scrape
  began and that the upsert did not resolve to. Matching is the upsert's own (`upsert_events(...,
  returned=)` collects the rows it resolved each event to: identity key, else dedup hash), so an
  unchanged event is never retired. In a full run the rows every earlier source returned are kept
  too: a row that another source merged into and still lists does not retire with the source that
  stored it. A single-source scrape knows only its own rows, so such a row can retire there and is
  un-retired by the next full run. Retiring changes nothing else, `last_seen` included; a row that
  any scrape returns again is un-retired by `_refresh`, keeping its UID. The admin's scrape result
  shows the retired count.
* **One filter:** `app/models.py` `shown_events(session, start, end)` is the only query of events a
  reader sees: the feeds and `/feeds`, the events page, its partial and calendar, `/api/events`, the
  category list and the digest. `test_every_event_window_query_goes_through_the_one_filter` fails on
  a `start_dt` filter written anywhere else in `app/`.
* **Digest:** a retired row leaves the digest; its fingerprint missing is a removal, so a retirement
  alone sends nothing (item 14's gate, unchanged). A retired row keeps every field.

**Failing first** (`tests/test_retire.py` against `origin/main` before the change): 9 failed, 7
passed. The failures: run 2 "the late show has no row of its own" (listed before the early show it
folded into it); "the late show folded into the early row" when it is listed alone; a retired row
returned by a full run; the four consumer tests and the migration test (`no such column:
retired_at`); the one-filter test (7 `start_dt` filters outside the helper). The 7 that passed are
the guards the mutants below need: a failed scrape, a 0-event scrape, a past row and an unchanged
event retire nothing; a cross-source duplicate merges; a row another source returned this run is
kept; a same-source row with no identity key still merges.

**Mutants** (each against the full suite, 221 tests, then reverted):
* retire on a 0-event scrape: fails `test_a_zero_event_scrape_retires_nothing` (1 failed).
* `is_different_event` always false (merging as before): fails `test_runs_one_to_four` and
  `test_a_late_show_first_seen_alone_gets_its_own_row` (2).
* the same-source condition removed: fails `test_a_cross_source_duplicate_still_merges` and
  `test_a_row_another_source_returned_this_run_is_not_retired` (2).
* `shown_events` ignoring `retired_at`: fails the feed, events-page, API and digest tests and run 3
  (5).
* the feed, the digest, or the events page/API querying around `shown_events`: fails that
  consumer's test and the one-filter test (3, 2 and 3).
* rows other sources returned this run not protected: fails
  `test_a_row_another_source_returned_this_run_is_not_retired` (1).
* a returned row not un-retired: fails run 4 and `test_a_retired_row_returned_by_a_full_run_keeps_its_uid` (2).

**Truncated lists** (read from each built-in type's code; a type that can stop listing a real
event retires it until it is listed again):

| Type | Can it truncate? | Why |
|---|---|---|
| `ticketmaster` | yes | Pages of 200 by date; the API stops at 1,000 results (item 22 measured a metro search losing its tail), so the furthest-out events drop once the near term fills. |
| `eventbrite` | yes | Four fixed scrolls of a lazy-loading page; it also keeps one event per title (first 40 characters) and day, so a second show that day is never returned. |
| `meetup` | yes | Three fixed scrolls of a lazy-loading search page. |
| `ical` | yes, by site | One GET, no paging. A WordPress events-calendar export can carry only the events of the list page it was exported from (that plugin's export follows the current view; not checked against a live feed here); a full calendar feed is complete. |
| `jsonld` | yes, by site | One page, no "next" link followed; a site that pages its list loses what is past page 1, and a new early event pushes the last one off it. |
| `playwright_jsonld` | yes, by site | One render, no scrolling: a lazy-loading list gives what the first view loads. |
| `squarespace` | yes, by site | One page of the events collection; a paginated collection loses its later pages. |
| `spothopper` | yes, by site | One page of the widget; both variants also keep one event per title and day. |
| `fathom` | no cap in code | One render of the releases page, every card read. Whether the page lists every release was not determined. |

Filed as **item 26**. Not a STOP: retirement is reversible and a retired row comes back, UID and
all, when it is listed again.

**Next:** item 15 builds on `is_different_event` and `batch_keys` (its note above).

### 26 — Scrapers that can return a truncated list retire events that are still on — NEEDS DECISION

**Found by item 25.** Item 25 retires a source's upcoming rows that a successful scrape (at least
one event) did not return. Every built-in type except `fathom` can return less than the source's
whole list (item 25's table): fixed scroll counts (`eventbrite`, `meetup`), a 1,000-result paging
cap (`ticketmaster`, item 22), one page with no paging (`ical` exports of a list view, `jsonld`,
`squarespace`, `spothopper`, `playwright_jsonld`), and per-title-per-day de-duplication inside
`eventbrite` and `spothopper` that drops a second show. Such a row leaves the feeds until a later
scrape lists it again; in a calendar app it disappears and comes back.

**Decision needed (owner):**
1. (a) A per-type flag (`COMPLETE_LIST = False`) on the types that cannot see the whole list, which
   then retire nothing; (b) fix each type to read the whole list (scroll until no new card, follow
   "next" links, item 22's date windows for Ticketmaster) and keep retiring everywhere; (c) retire
   only rows starting before the latest event the scrape returned (a list cut at its far end then
   retires nothing past the cut),
   which covers the paging and scroll caps but not the per-day de-duplication.
2. Whether a plugin type declares the same flag.
*Recommended: (c) for every type, plus removing the per-title-per-day de-duplication in
`eventbrite` and `spothopper` (item 18's `timed_hash` already keeps two shows apart).*

**Measure first if wanted:** the retired count per source over a few runs of a deployment says
which types truncate in practice; item 25's first deployment recorded one run.
