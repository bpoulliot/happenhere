# 11 — Facebook and Instagram as sources; event flyers through a local vision model

Measured 2026-11-04. Aggregate numbers and public facts only: the pages probed, the flyers, their
truth and each model answer are kept by the deployment, outside this repository.

## 1. Facebook

**Logged out, 2 requests** (one public Page's events tab, curl with a browser user agent, no cookies):

| Variant | Result |
|---|---|
| `www.facebook.com/<page>/events`, desktop UA | HTTP 400, a 1.5 KB "Sorry, something went wrong" page: no content, no event data |
| `m.facebook.com/<page>/events`, mobile UA | HTTP 302 to `m.facebook.com/login.php?next=…&refsrc=deprecated`: a login wall |

`mbasic.facebook.com` was not tried: the mobile site already sends a logged-out client to the
login page. Nothing was done to get past either answer.

A side finding for anyone deploying this: a common DNS blocklist setup (Pi-hole) answers `::` for
`facebook.com` and `instagram.com`, so on such a network a scraper could not reach either site at all.

**Graph API** (Meta developer docs, read 2026-11-04 through search results; the doc pages
themselves did not load from this host, so the wording below should be checked in a browser
before anything is built on it):

* Graph API is at v26.0 (released 2026-07-29) — <https://developers.facebook.com/docs/graph-api/changelog/versions/>.
* Event node — <https://developers.facebook.com/docs/graph-api/reference/event>: an app sees the
  events a person can see only with a user token and `user_events` (App Review), and events that the
  app or a Page it holds a token for created. The page states *"Page Event access is restricted to
  whitelisted partners at this time."*
* Page reference — <https://developers.facebook.com/docs/graph-api/reference/page>: reading a Page
  the app does not manage needs the **Page Public Content Access** feature plus
  `pages_read_engagement` / `pages_read_user_content`. Like Advanced Access generally, the feature
  requires App Review and Business Verification. The edges that the reference lists under that
  feature are feed, photos, videos and live_videos. It does not list `events`.
* Third-party projects that once read Page events by location record that "access to the Events API
  is no longer granted" (e.g. <https://github.com/tobilg/facebook-events-by-location>).
* **Calendar export:** a personal "upcoming events" iCal URL still appears to exist, in the form
  `facebook.com/events/ical/upcoming/?uid=…&key=…`. It sits behind an "Add to calendar" / "Export"
  dialog for a logged-in user (<https://smashballoon.com/doc/ical-url-for-the-facebook-events-feed/>).
  Meta's help centre does not document it, and some calendar clients fail to subscribe
  (<https://discussions.apple.com/thread/254963476>). It carries a personal secret `key`, so it is a
  credential, and this project does not hold anyone's social-media credentials. It was not tested.
* **Automated collection:** Meta's Automated Data Collection Terms
  (<https://developers.facebook.com/docs/development/terms-and-policies/automated-data-collection/>)
  make the platform APIs the only allowed programmatic access without Meta's prior written
  permission, logged in or not.

**Finding:** there is no sanctioned, logged-out way to read a third-party Page's events. The API
route needs a whitelisted-partner status that a self-hosted aggregator will not get, and scraping is
both walled and barred by the terms.

## 2. Instagram

**Logged out, 1 request** (one public business profile, desktop UA): HTTP 200, a 500 KB JavaScript
application shell. It contains no `og:description` or `og:image`, no post shortcodes, no captions and
no JSON-LD. The handle appears once, in a URL. A logged-out HTTP client gets nothing to parse.

**API:** Business Discovery
(<https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/>)
reads another **Business or Creator** account's media. It needs Facebook Login, `instagram_basic` and
`pages_read_engagement`, an Instagram professional account of one's own to query through, and
Advanced Access (App Review and Business Verification) to serve accounts the app does not own
(<https://developers.facebook.com/docs/instagram-platform/overview/>). The Basic Display API was
retired on 2024-12-04. A media object carries `caption`, `media_type`, `media_url`, `permalink` and
`timestamp`, which is the post time, not the event time
(<https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/>).

**Finding:** posts carry **no structured event data**, only an image and free-text caption. Even with
an approved app, an event would come from parsing the flyer or the caption.

## 3. Flyers through a local vision model

**Set:** 17 real event images from two public venue websites (no login). Truth for title, date,
start time, venue and price was hand-written from the same listing's text:

* **12 artist images with no date, time, venue or price printed on them.** These are typical of
  what a theatre posts.
* **5 flyers that print a date.** One prints only "New Year's Eve", with no year. Two also print a
  time. None prints a ticket price, but one prints drink prices.

Where a listing's text named the same event or place differently (a film's name and the tour's
name, a brewery and its taproom), both names were accepted. These alternates were added after the
model outputs were seen, and only from names the listing or flyer itself uses. Abbreviations the
listing does not use count as wrong.

**Models:** every installed model with the `vision` capability (`/api/show`) on the shared Ollama box.
That was one model, `gemma3:12b-it-qat` (11.8B, Q4_0). Nothing was pulled. Settings were
`format: json`, temperature 0, one pass per prompt variant, and the original images.

**Prompts** (the exact text is in `11-flyer-eval.py`):

* **A** asks for the five fields as JSON, with null when unknown.
* **B** adds today's date, "only report what is printed; do not guess", "start, not doors" and
  "a month and day without a year means the next such date".

| | Prompt A | Prompt B |
|---|---|---|
| Title correct | 13/17 | 13/17 |
| Date correct | 0/17 | 5/17 (all 5 dated flyers) |
| **Wrong date, flyers that print a date** | **4/5** (every year guessed as 2023) | **0/5** |
| **Wrong date, images with no date** | 0/12 | **8/12** (all 8: *today's* date) |
| Start time correct (truth incl. "not stated") | 3/17 (1 wrong) | 4/17 (0 wrong) |
| Venue correct | 1/17 (4 wrong) | 1/17 (4 wrong) |
| Price wrong (none printed) | 8/17 ("0" = free, or a drink price) | 3/17 (two "0", one drink price) |
| JSON parse failures | 0 | 0 |
| Median seconds per flyer (load excluded) | 4.6 | 5.0 |
| Model load (cold) | 7.2 s | warm |

Precision and sampling: 17 images from 2 sites, a convenience sample of whatever the two sites
listed, not a random one. Only 5 images carry a date, so the dated-flyer rates are 5-sample rates.
Each prompt had one pass at temperature 0, and the timings come from one shared box with no other
model loaded.

**What the numbers say:**

1. **The image alone does not settle the date.** Without "today" the model gets every printed
   date's year wrong. With "today" and an explicit "do not guess" it puts today's date on two thirds
   of undated images. Either way, an unattended pipeline publishes events on the wrong day. A
   rule-based guard cannot separate the two failures from the output alone.
2. **Titles read well (13/17). Venue and price do not.** The brewery's name was misspelled on 2 of the 3
   flyers where the model read it, in both prompts, and prices were taken from unrelated figures on the image.
3. **Speed is not the constraint:** about 5 s per flyer.
4. **The likely flyer sources are the walled ones (sections 1-2).** Venue websites that post
   images usually post the listing text too, and that text is the better source.

A better OCR-oriented model was not measured, because none is installed and none was pulled. Qwen2.5-VL 7B
(about 6 GB in the Ollama library; size not verified this session) would be the next candidate. It
would not change finding 1, which comes from the input: the image has no date to read.

## Re-running

`python3 docs/measurements/11-flyer-eval.py selftest` checks the scorer in both directions. A run is
`… run --endpoint URL --model NAME --variant A|B --flyers DIR --out FILE.jsonl --today YYYY-MM-DD`,
then `… score --truth TRUTH.json FILE.jsonl …`. It waits while another model is loaded on the
endpoint, and gives up after 30 minutes.
