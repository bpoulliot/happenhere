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

**Next free item number: 18.** Always re-read it here before filing.

## Items

| # | Tier | Item | Status |
|---|---|---|---|
| 01 | 0 | Green CI; blocking CodeQL; Chromium leak; bcrypt; SSRF guard; fail-closed auth | **DONE** |
| 02 | 1 | OpenID Connect admin sign-in (group-gated, local login disabled); every HTML page rendering | **DONE** |
| 03 | 1 | Feeds by tag and by venue, stable event UIDs, "add to calendar" links | **READY** |
| 04 | 1 | Personal feeds behind a long random token; an "artists I listen to" feed | **NEEDS MEASUREMENT** |
| 05 | 1 | Idempotent source import/export from a YAML file | **DONE** |
| 06 | 2 | Per-source canary: a source that returns zero events or keeps failing raises one alert | **READY** |
| 07 | 0 | Deprecations that will break: `datetime.utcnow()`, `ORJSONResponse`, authlib/Starlette on `httpx` | **READY** |
| 08 | 2 | Release workflow without a personal access token, compatible with a PR-only `main` | **NEEDS DECISION** |
| 09 | 0 | Dependency bumps, low risk: CI actions and patch-level libraries (Dependabot) | **READY** |
| 10 | 0 | Web-framework bump: FastAPI and uvicorn (Starlette moves with them) | **READY** |
| 11 | 3 | Facebook and Instagram as event sources; reading event flyers with a vision model | **NEEDS DECISION** |
| 12 | 1 | Ticketing platforms as source options: Ticketmaster-by-venue type, wider JSON-LD, platform recognition | **READY** |
| 13 | 1 | Local scraper plugins: extra source types loaded from a directory outside the repo | **DONE** |
| 14 | 1 | Configurable notification digest that never repeats an unchanged list | **DONE** |
| 15 | 0 | A re-scraped event keeps its first start time: a changed time never reaches the feed or the digest | **READY** |
| 16 | 1 | Eventbrite: the regional scraper is against the site's terms; an API token or the partner program instead | **NEEDS DECISION** |
| 17 | 0 | Hard-coded `America/Denver`: parsed times and the ICS feed ignore the configured timezone | **READY** |

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

### 09 — Dependency bumps, low risk — READY

The open Dependabot PRs that touch CI actions or patch-level libraries (not FastAPI, Starlette or
uvicorn: item 10). Each passed CI alone; together they edit the same files. One combined PR with
exactly the versions Dependabot proposed, after reading the release notes of every **major**
action bump (inputs, environment variables, permissions, runtime) and adjusting the workflow if a
note requires it. Local checks, a local `docker build`, and CI green on the combined head. The
superseded Dependabot PRs are closed with a pointer to the combined one. A workflow that only runs
on a tag or by hand (`release.yml`) is named in the PR as not exercised.

### 10 — FastAPI and uvicorn — READY

FastAPI's and uvicorn's Dependabot PRs together, because FastAPI moves Starlette, which is not
pinned here, and Starlette changes have broken every page before (item 02). Read the changelogs
between the current and proposed versions of FastAPI, Starlette and uvicorn; list each breaking or
deprecation note that touches code here. **Pin Starlette** to the version the new FastAPI
resolves, so it never moves silently again. The full test suite (every page has a render test),
the count of `DeprecationWarning`s before and after, and a smoke run of a scratch container built
from the branch (`AUTH_MODE=local`): `/health`, `/events`, `/events/calendar`, `/feed/events.ics`,
`/admin` redirect, a local sign-in round trip. Item 07 runs after this one (some of its
deprecations may change with the bump).

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
  only after a successful post. A fingerprint is a hash of `event_identity(ev)` (today the dedup
  hash; item 03's stable UID replaces that one function), title, start time and venue. Sends iff at
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
