# 12 — Ticketing platforms as built-in source options

Measured 2026-11-04. The doc and terms pages below were read on that date. The live probes were one
request per page: curl with a desktop browser user agent and no cookies. Only well-known public
listings outside any one deployment were probed. The raw responses are kept by the deployment, outside
this repository. "UNVERIFIED" marks a point that no primary source confirmed.

**Instrument for "does the app already read it":** each fetched page went through the app's own
`extract_jsonld_events` and `parse_jsonld_event` (`app/scrapers/_html_base.py`). The parser was
checked in both directions. It found 1 event on a page with a top-level `MusicEvent` and 0 events on a
page with no Event markup. "0" in the table therefore means the generic `jsonld` type would get
nothing from that page.

## Summary

| Platform | Sanctioned machine path | Key / terms | Plain HTTP fetch of a public page | Verdict |
|---|---|---|---|---|
| **Ticketmaster** | Discovery API v2, `events?venueId=` | Free self-serve key. 5,000 calls/day and 5/s. Cache only "for reasonable periods" | n/a (API) | **Build** `ticketmaster_venue` |
| **Eventbrite** | API v3: `venues/{id}/events/`, `organizations/{id}/events/` | Free private token (self-serve). Whether it reads events you do not own is UNVERIFIED. Reading many creators' public events needs the distribution partner program. The ToS forbids scraping | Organizer page 200, events only in a JS data blob. Event page 200 with `EducationEvent` JSON-LD, which the app does not match | **Decision** (item 16) |
| **DICE** | Partner GraphQL (own events only); widget `api/v2/events` with a partner's key | Partner-only. The ToU forbids crawling "any page" with scripts | Venue page 200 with 30 events in `Place.event` JSON-LD, which the app does not read | **Not buildable** on dice.fm. A venue's own page is covered by `jsonld` once G1/G2 are fixed |
| **Bandsintown** | `rest.bandsintown.com/artists/{name}/events` | `app_id` for artists' teams. Others must apply, with no student or educational use. No venue endpoint. Scraping forbidden | 403 Cloudflare challenge | **Not buildable** as a venue source. An artist-based feed belongs to item 04 |
| **See Tickets** (UK; US now EVENTIM) | none found | No public developer docs found | UK 403; US redirects to eventim.us, 403 | **Not buildable** |
| **Etix** | API v3 (`/public/events?venueId=`), OAuth2 | Needs an API Partner Agreement and a US-resident user. 20,000 req / 5 min per IP | 202 AWS WAF challenge | **Not buildable** without a partner agreement |
| **AXS** | none public (partners via AXS solutions) | UK terms forbid "any automatic means (e.g. robot, spider…)". NA terms page 403, UNVERIFIED | 403 bot page | **Not buildable** |

## Per platform

### Ticketmaster (Discovery API v2)

* Docs: <https://developer.ticketmaster.com/products-and-docs/apis/getting-started/>,
  <https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/>. The key is
  self-serve. *"The default quota is 5000 API calls per day and rate limitation of 5 requests per
  second."* Deep paging stops at `size * page < 1000`.
* `source` filter values: `ticketmaster`, `universe`, `frontgate`, `tmr`. Getting Started says the
  API covers TicketWeb as well, but TicketWeb is not a `source` value, so whether its events appear is
  UNVERIFIED.
* Terms (<https://developer.ticketmaster.com/support/terms-of-use/>, updated 2023-06-27). You may not
  *"Cache or store any Event Content other than for reasonable periods in order to provide the service
  you are providing"*, may not *"derive revenues from the use or provision of the Ticketmaster API"*,
  and may not build an app that *"replicates or attempts to replace the unique essential user
  experience of Ticketmaster.com"*. Ticketmaster may rate limit applications that make many calls not
  driven by users. No attribution clause was found (UNVERIFIED).
* **Live, 2 calls:** `venues.json?keyword=Madison Square Garden&countryCode=US` returned three
  venues. The first has `id` `KovZpZA7AAEA` and `url`
  `https://www.ticketmaster.com/madison-square-garden-tickets-new-york/venue/483329`. The other two
  have no `url`, which suggests duplicate records from other sources. `events.json?venueId=KovZpZA7AAEA`
  reported `totalElements` 393. Each event carries `id`,
  `dates.start.{localDate,localTime,dateTime}`, `dates.start.noSpecificTime` and `dates.timezone`
  (`America/New_York`). Response headers `rate-limit: 5000` and `rate-limit-available`.
* **The numeric id in a ticketmaster.com venue URL is not the Discovery `venueId`.** Venue search
  returns the page URL in `url`, which is the only bridge found between the two.

### Eventbrite

* The public event search was shut down: *"Access to this API was shut down at 11:59 pm PT on
  Thursday, December 12, 2019."* The changelog names the replacements, `GET /v3/venues/:venue_id/events/`
  and `GET /v3/organizations/:organization_id/events/`. It adds: *"If you're interested in retrieving
  public events on behalf of many Eventbrite creators, you can apply to our distribution partner
  program"* (<https://www.eventbrite.com/platform/docs/changelog>,
  <https://www.eventbrite.com/l/distribution-partnerships/>). `/v3/organizers/{id}/events/` is no
  longer in the reference.
* Auth: a private OAuth token from the account's API Keys page (self-serve), sent as
  `Authorization: Bearer`. No price is stated anywhere.
* Rate limits: the docs page says 2,000 calls/hour and 48,000/day
  (<https://www.eventbrite.com/platform/docs/rate-limits>). API Terms 3.5 says 1,000 calls/hour per
  token.
* API Terms (<https://www.eventbrite.com/help/en-us/articles/833731/eventbrite-api-terms-of-use/>,
  updated 2025-05-30):
  * 3.1: you may store content about future events, but *"you may not store any Site Content
    relating to events that have occurred in the past"*.
  * 3.2: *"your Application must display the event title and display a direct link to the
    Eventbrite webpage"*.
  * 4.2: access to another user's content needs *"prior, clear, express consent from each Eventbrite
    User whose Content you access"*.
* Terms of Service 13.1 (<https://www.eventbrite.com/tos>, updated 2025-08-20): *"You have no right to,
  and you agree not to, scrape, crawl, or employ any automated means to extract data from the
  Sites."* **The app's existing `eventbrite` type is exactly that.** It is a Playwright scraper of
  `eventbrite.com/d/<region>/events/`. See item 16.
* **Live, 2 pages:** an organizer page (`/o/<slug>-<id>`) returned 200 (20 KB). Its JSON-LD is a
  `ProfilePage` only. The upcoming events sit in the Next.js `__NEXT_DATA__` blob
  (`pageProps.upcomingEvents`). An event page (`/e/<slug>-tickets-<id>`) returned 200 with an
  `EducationEvent` JSON-LD item. `extract_jsonld_events` matched 0 items there (G1 below).
* Whether a personal token can read a venue's or organization's public events that it does not own
  could not be determined without an account. It is the question item 16 puts to the operator.

### DICE

* APIs: the Ticket Holders GraphQL API (<https://partners-endpoint.dice.fm/graphql/docs/index.html>)
  uses a token made in DICE's partner dashboard (MIO). It sees only the partner's own events through
  `viewer`. The Event Listing widget (<https://dice.fm/partners/widget>) needs a partner ID and a
  40-character API key, and calls `/api/v2/events` with `x-api-key` and filters such as
  `filter[venue_ids][]` and `filter[promoter_ids][]`. Neither is open to a third party.
* Terms of Use (US, updated 2026-03-10,
  <https://dicefm.zendesk.com/hc/en-gb/articles/4412973085841-United-States-Terms-of-Use>): users may
  not *"use software, devices, or other manual or automated processes to "crawl" any page of our
  website, App or Services, including but not limited to any use of any scripts or web crawlers"*.
  robots.txt allows `/` and disallows `/api/` for `*`, and blocks AI crawlers.
* **Live, 2 pages:** a venue page (`dice.fm/venue/<slug>-<4 chars>`, a London club) returned 200. Its
  JSON-LD holds a `Place` with **30 `Event` items nested in `Place.event`**, each with a `url` of the
  form `dice.fm/event/<24-hex id>`. `extract_jsonld_events` returns 0 for the page (G2 below). Every
  one of the 30 parses with `parse_jsonld_event` once handed over directly. The event page redirects
  to a slug URL (`dice.fm/event/<6 chars>-<title>-<date>-<venue>-tickets`). Its `MusicEvent` `url` is
  that slug, which contains the title.
* Verdict: the data is there, but the terms forbid the automated access needed to read it. A venue
  whose own website embeds its DICE listings as JSON-LD is read by the generic type, from the venue's
  site.

### Bandsintown

* API (<https://api.swaggerhub.com/apis/Bandsintown/PublicAPI/3.0.1>) has three endpoints, all
  artist-keyed: `/artists/{name}`, `/artists/id_{id}` and `/artists/{name}/events?app_id=&date=upcoming`.
  **There is no venue endpoint.**
* Who gets an `app_id`: *"API access is available only to artists and to teams authorized to act on
  an artist's behalf"*
  (<https://help.artists.bandsintown.com/en/articles/7053475-what-is-the-bandsintown-api>). Other
  people email partnerships, and *"Currently, we are not accepting API requests for student projects
  or educational purposes"*
  (<https://help.artists.bandsintown.com/en/articles/3372745-can-i-have-access-to-the-api-and-an-api-key-if-i-m-not-an-artist>).
* Data Applications Terms (<https://corp.bandsintown.com/data-applications-terms>) put the API
  *"solely"* in the hands of artists and their teams. They allow session caching only with notice to
  Bandsintown, forbid commercial use without approval, and require attribution. The fan terms forbid
  *"'spidering', 'screen scraping'"* (<https://corp.bandsintown.com/terms>). No rate limit is
  published.
* Event shape: `datetime` is venue-local with no offset and no timezone field. `venue` carries
  `latitude` and `longitude`.
* **Live, 2 requests:** the homepage and the sitemap both returned 403 (Cloudflare challenge).
* Verdict: no venue source. An "events of artists I follow" feed is item 04's question, and it would
  need an approved `app_id`.

### See Tickets

* `www.seetickets.us` now redirects (302) to `www.eventim.us`. See Tickets US was rebranded as EVENTIM
  after CTS Eventim acquired it (trade press, Pollstar 2025-03-04).
* No public developer API, widget feed or iCal export was found in official sources. A third-party
  mention of an affiliate feed is UNVERIFIED.
* **Live, 2 requests:** `seetickets.com` returned 403. `seetickets.us` redirected to `eventim.us`,
  which returned 403. Its page says *"You may see this page if you are accessing our website using
  automated software of any kind."*
* Verdict: not buildable. There is no sanctioned path, and the site refuses automated clients.

### Etix

* API v3 (<https://api.etix.com/v3/docs/index.html>, spec `openapi.json`) offers `GET /public/events`
  with `venueId` or `organizationId` (*"Only the public, sellable and upcoming performances will be
  returned"*), behind OAuth2. Without a token it returns 401. Rate limit: *"20,000 requests per 5
  minutes (300s) per IP"*.
* API Partner Terms (<https://www.etix.com/ticket/online3/apiPartnerTerms.jsp>, updated 2022-06-30):
  unless you *"execute an API Partner Agreement with Etix, you will not be entitled to use the Etix
  APIs"*. Users must be at least 18 and US residents. The terms forbid caching beyond reasonable
  periods and forbid building databases or permanent copies.
* robots.txt allows the public venue and organizer pages (`/ticket/v/*`, `/ticket/o/*`).
* **Live, 1 request:** the homepage answered 202 with a 2 KB AWS WAF challenge and no content. The
  claim that venue pages carry JSON-LD is UNVERIFIED.
* Verdict: not buildable without a partner agreement, which a self-hosted deployment's operator
  would have to sign.

### AXS

* No public developer API was found. Integrations such as Spotify's go through AXS partnerships
  (UNVERIFIED beyond press reports).
* UK terms (<https://www.axs.com/uk/about-terms-of-use_UK_v5.html>) forbid *"any automatic means (e.g.
  robot, spider, offline reader"* and data mining. The exception covers public search engines only.
  The North American terms page returned 403, so its wording is UNVERIFIED.
* robots.txt: `*` disallows `/users`, `/me`, `/search`, `/contributor` and `/*referrer=*`, with
  `Crawl-delay: 2`.
* **Live, 1 request:** the homepage returned 403, a 105 KB "AXS Access Info" bot page.
* Verdict: not buildable.

## What the app already covers

Read from `app/scrapers/` and `app/discovery.py`:

* `ticketmaster` (`TicketmasterScraper`) searches by `postalCode`. Discovery treats that as "venues in
  that ZIP", and the radius is ignored. No built-in type reads a single venue. A deployment needs a
  local plugin to read one.
* `eventbrite` (`EventbriteScraper`) is a Playwright scraper of the regional search page. It is
  against the ToS (item 16).
* `jsonld` and `playwright_jsonld` cover any page with **top-level** JSON-LD of `@type` `Event`,
  `MusicEvent` or `TheaterEvent`. That cover misses two kinds of page measured here:
  * **G1** — `extract_jsonld_events` compares `@type` to exactly those three strings. It drops the
    other schema.org Event subtypes (`EducationEvent` on an Eventbrite page, plus `ComedyEvent`,
    `SocialEvent`, `Festival`, `DanceEvent`, `ScreeningEvent`, `LiteraryEvent`, `SportsEvent`,
    `ExhibitionEvent`, `FoodEvent`, `ChildrensEvent`, `BusinessEvent`). It also drops an `@type` given
    as a list (`["Event", "…"]`).
  * **G2** — it does not look inside a `Place`, `Organization` or `MusicGroup` for an `event` /
    `events` property. That nesting is how a DICE venue page publishes its listings, and venue sites
    use it as well.
* Detection never recognises a platform from its URL. A pasted platform URL falls through the
  JSON-LD, iCal-probe and Playwright tiers. For dice.fm that means seven iCal probe requests to a site
  whose terms forbid it.

## Build spec (ROADMAP 12)

1. **Generic JSON-LD (G1, G2)** in `app/scrapers/_html_base.py`:
   * accept every `@type` that is `Event` or ends in `Event`, plus `Festival`, whether a string or a
     list;
   * collect items nested one level down in `event` / `events` (a dict or a list) of any top-level or
     `@graph` item;
   * de-duplicate by `url`, falling back to `(name, startDate)`, so a page that lists an event at top
     level and nested gives one event.

   Fixtures, synthetic on `example.com`: a `Place` with nested events, an `EducationEvent`, a
   list-valued `@type`, and a page carrying both forms of the same event.
2. **`ticketmaster_venue` source type.** This is the one platform type worth building. It promotes
   the local-plugin pattern to a built-in.
   * **Pasted URL:** either a Discovery URL (`https://app.ticketmaster.com/discovery/v2/events?venueId=A[,B]`)
     or a ticketmaster.com venue page (`https://www.ticketmaster.com/<slug>/venue/<n>`).
   * **Resolving a venue page:** call `venues.json?keyword=<slug words>` and keep the venue whose
     `url` ends `/venue/<n>`. Store the Discovery id in the source URL, so it resolves once and the
     scrape never repeats the search. If nothing matches, tell the admin to paste the Discovery id.
   * **Calls:** `events.json?venueId=<ids>&size=200&sort=date,asc&startDateTime=<now UTC>` with
     `apikey` from `TICKETMASTER_API_KEY`, never stored in the URL. Page through until `page.number`
     reaches `totalPages - 1` or `size * page` reaches 1000.
   * **Field mapping:**
     * `name` → `title`;
     * `dates.start.dateTime` (UTC) → `start_dt`, with `dates.start.localDate` alone when
       `noSpecificTime` is set;
     * `_embedded.venues[0].{name,address.line1,city.name,state.stateCode}` → `venue_name` and
       `venue_addr`;
     * `url` → `url`;
     * the `images` item with `ratio` `16_9` (else the first) → `image_url`;
     * `classifications[0].{segment,genre}.name` → `categories`, skipping "Undefined";
     * `id` → `raw_data["id"]`.
   * **Stable identity (item 03):** `ticketmaster:<event id>`. The event id does not change when the
     title does. The event page URL contains the title and the date.
   * **Detection rule:** host `app.ticketmaster.com` with a `venueId` parameter, or host
     `*.ticketmaster.com` with path `/venue/<digits>`. Detection runs before any fetch and makes no
     request to ticketmaster.com.
   * **Fixtures:** a two-page synthetic `events.json` response, one with `noSpecificTime`, and a
     `venues.json` response where one of three venues has a matching `url`.
3. **Platform recognition in `app/discovery.py`,** before any fetch, from the URL's host. The
   result names the platform and gives the reason it is not a source, and makes no request:
   * `dice.fm`, `bandsintown.com`, `axs.com`, `seetickets.com`, `seetickets.us`, `eventim.us`,
     `etix.com`: "this platform's terms (or bot wall) do not allow automated reading. Add the venue's
     own website instead";
   * `eventbrite.*`: as above until item 16 decides.

   Tests: one URL per host gives that note and records no HTTP request.
4. **Not built:** DICE, Bandsintown, See Tickets, Etix and AXS as source types, for the reasons in
   the table. Revisit Etix or DICE only if an operator obtains a partner agreement or key. That would
   be a new item.

**Cost of keys:** only Ticketmaster's is needed for the spec above. It is free and self-serve, and a
deployment already using the `ticketmaster` type already has one. The 5,000 calls/day quota covers
hundreds of venue sources at a daily scrape, at one or two pages each.
