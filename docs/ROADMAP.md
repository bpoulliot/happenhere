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

**Next free item number: 09.** Always re-read it here before filing.

## Items

| # | Tier | Item | Status |
|---|---|---|---|
| 01 | 0 | Green CI; blocking CodeQL; Chromium leak; bcrypt; SSRF guard; fail-closed auth | **DONE** |
| 02 | 1 | OpenID Connect admin sign-in (group-gated, local login disabled); every HTML page rendering | **DONE** |
| 03 | 1 | Feeds by tag and by venue, stable event UIDs, "add to calendar" links | **READY** |
| 04 | 1 | Personal feeds behind a long random token; an "artists I listen to" feed | **NEEDS MEASUREMENT** |
| 05 | 1 | Idempotent source import/export from a YAML file | **READY** |
| 06 | 2 | Per-source canary: a source that returns zero events or keeps failing raises one alert | **READY** |
| 07 | 0 | Deprecations that will break: `datetime.utcnow()`, `ORJSONResponse`, authlib/Starlette on `httpx` | **READY** |
| 08 | 2 | Release workflow without a personal access token, compatible with a PR-only `main` | **NEEDS DECISION** |

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

### 03 — Feeds by tag and by venue — READY

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

### 04 — Personal feeds and an "artists I listen to" feed — NEEDS MEASUREMENT

A feed that only lists events whose performer matches artists the owner listens to. Inputs to
measure before building: a ListenBrainz user's top artists and similar artists (public API), and
an optional Subsonic-compatible server's starred artists. Personal feeds sit behind a long random
token per feed (rotatable one at a time), because calendar apps cannot send credentials. Measure:
how many artists the inputs give, how event titles name performers across source types, and the
match rate on a synthetic set before choosing a matcher.

### 05 — Source import/export — READY

`python -m app.sources export > sources.yml` and `python -m app.sources import sources.yml
[--apply]` (dry run by default; prints the add/update/unchanged/would-disable plan). Keyed by
source `name`; fields: display name, type, URL, categories, tags (item 03 if landed, else ignored
with a warning), enabled. Import never deletes: a source missing from the file is reported, and
disabled only with `--disable-missing`. Every URL passes `netguard`. Round-trip test:
export → import is a no-op.

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
