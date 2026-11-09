"""Idempotent source import/export.

    python -m app.sources export > sources.yml
    python -m app.sources import sources.yml [--apply] [--disable-missing]

Sources are keyed by ``name``. An import is a dry run unless ``--apply`` is given, validates
every entry before it writes anything (one bad entry refuses the whole file) and writes in one
transaction. It never deletes: a source absent from the file is reported, and disabled only
with ``--disable-missing``. Every URL passes ``netguard``, as it does in the admin form.

A *disabled* entry whose URL the guard refuses is not a refused file (item 24): it is stored
disabled, the refusal in its ``last_error``, and the plan shows a warning. Enabling it, in the
admin or by a later import, runs the guard again and is refused while it still fails. A regional
``ticketmaster`` entry whose URL still carries an area (item 22's form) is judged the same way:
since item 30 the area is a search location.

Search locations (item 30) travel in the same file, keyed by label, and are added or updated,
never deleted::

    locations:
      - label: Midtown
        where: "10001"          # a US ZIP code, "City, ST", or "lat,lon"
        radius: 25
        unit: miles             # or km
    sources:
      - name: ticketmaster
        type: ticketmaster
        url: https://app.ticketmaster.com/discovery/v2/events
        locations: [Midtown]    # or "all" (the default for a new source); absent = unchanged
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from . import locations as loc
from . import logredact
from .models import SearchLocation, Source
from .netguard import NonPublicURL, require_public_url
from .scrapers import ticketmaster
from .scrapers.registry import is_location_aware, scraper_types

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[\w.-]+$")
_TAG_RE = re.compile(r"^[a-z0-9-]+$")
_FIELDS = {"name", "display_name", "type", "url", "categories", "tags", "enabled", "locations"}
_LOCATION_FIELDS = {"label", "where", "radius", "unit", "meetup_location", "eventbrite_region"}
ALL = "all"  # a source's ``locations:`` value for every location


class ImportRefused(ValueError):
    """The file is not importable; the message names every bad entry."""


# A ``last_error`` written by a failed import/admin check (not by a scraper) starts with one of
# these marks, so a later pass can tell its own record from a scraper's and clear only its own.
GUARD_REFUSAL_MARK = "url refused: "
LOCATION_IN_URL_MARK = "location in url: "  # item 30: a regional Ticketmaster URL with an area
LOCATION_MISSING_MARK = "location missing: "  # item 22's mark; cleared by app.locations.migrate
CHECK_ERROR_MARKS: tuple[str, ...] = (
    GUARD_REFUSAL_MARK,
    LOCATION_IN_URL_MARK,
    LOCATION_MISSING_MARK,
)


def is_check_error(last_error: str | None) -> bool:
    return bool(last_error) and last_error.startswith(CHECK_ERROR_MARKS)


def failed_check(
    enabled: bool, label: str, errors: list[str], message: str, last_error: str
) -> str | None:
    """The one place a failed per-entry check is judged (item 24; item 22 reuses it).

    An **enabled** entry that fails refuses the file: ``message`` joins ``errors`` and None comes
    back. A **disabled** entry is not refused: the ``last_error`` to store for it comes back, and
    the plan shows it as a warning. ``last_error`` must start with a mark in ``CHECK_ERROR_MARKS``
    so that a later import, once the check passes, clears it and only it.
    """
    if enabled:
        errors.append(f"{label}: {message}")
        return None
    return last_error


# --- Rules shared with the admin source form (app/web/routes.py) ------------------------


def check_source_url(url: str) -> None:
    """An empty URL is allowed (regional aggregators); anything else must be public http(s)."""
    if url:
        require_public_url(url)


def location_refusal(stype: str, url: str) -> str | None:
    """Fixed text when a source of this type cannot run from this URL, else None.

    Only the regional ``ticketmaster`` type has such a rule: its URL must not carry an area of
    its own (item 30: the areas are the search locations).
    """
    if stype == "ticketmaster":
        return ticketmaster.location_problem(url)
    return None


def split_categories(categories: str) -> list[str]:
    return [c.strip() for c in categories.split(",") if c.strip()]


def split_tags(tags: str) -> list[str]:
    """The admin form's comma-separated tags, lowercased; ValueError names a non-slug."""
    out: list[str] = []
    for t in (t.strip().lower() for t in tags.split(",")):
        if not t:
            continue
        if not _TAG_RE.match(t):
            raise ValueError(t)
        if t not in out:
            out.append(t)
    return out


def source_tags(src: Source) -> list[str]:
    try:
        tags = json.loads(src.tags or "[]")
    except ValueError:
        return []
    return [t for t in tags if isinstance(t, str)]


# --- Validation --------------------------------------------------------------------------


def _validate_entry(raw: object, label: str) -> tuple[dict | None, list[str]]:
    if not isinstance(raw, dict):
        return None, [f"{label}: must be a mapping, got {type(raw).__name__}"]
    errors: list[str] = []
    name = raw.get("name")
    if isinstance(name, str):
        name = name.strip()
    if not name or not isinstance(name, str):
        errors.append(f"{label}: name is required")
        name = None
    elif not _NAME_RE.match(name):
        errors.append(f"{label}: name {name!r} may only contain letters, digits, '_', '-' and '.'")
    label = f"entry {name!r}" if name else label

    for key in sorted(set(raw) - _FIELDS):
        errors.append(f"{label}: unknown field {key!r}")

    stype = raw.get("type")
    known = {k for k, _ in scraper_types()}
    if stype not in known:
        errors.append(f"{label}: unknown type {stype!r} (known: {', '.join(sorted(known))})")

    display = raw.get("display_name", name or "")
    if not isinstance(display, str):
        errors.append(f"{label}: display_name must be a string")
        display = ""

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append(f"{label}: enabled must be true or false")
        enabled = True

    refusal: str | None = None
    url = raw.get("url", "")
    url = "" if url is None else url
    if not isinstance(url, str):
        errors.append(f"{label}: url must be a string")
        url = ""
    else:
        url = url.strip()
        try:
            check_source_url(url)
        except NonPublicURL as exc:
            refusal = failed_check(
                enabled,
                label,
                errors,
                f"url refused: {exc} (must be http(s) and non-public hosts are refused)",
                f"{GUARD_REFUSAL_MARK}{exc}",
            )
        if refusal is None and isinstance(stype, str):
            problem = location_refusal(stype, url)
            if problem:
                refusal = failed_check(
                    enabled, label, errors, problem, f"{LOCATION_IN_URL_MARK}{problem}"
                )

    cats = raw.get("categories", [])
    cats = [] if cats is None else cats
    if not isinstance(cats, list) or not all(isinstance(c, str) for c in cats):
        errors.append(f"{label}: categories must be a list of strings")
        cats = []
    cats = [c.strip() for c in cats if c.strip()]

    tags = raw.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(
            isinstance(t, str) and _TAG_RE.match(t) for t in tags
        ):
            errors.append(f"{label}: tags must be a list of slugs ([a-z0-9-])")
            tags = None

    locs = raw.get("locations")
    if locs is not None and locs != ALL:
        if not isinstance(locs, list) or not all(isinstance(x, str) and x.strip() for x in locs):
            errors.append(f"{label}: locations must be 'all' or a list of location labels")
            locs = None
        else:
            locs = [" ".join(x.split()) for x in locs]

    if errors:
        return None, errors
    entry = {
        "name": name,
        "display_name": display or name,
        "type": stype,
        "url": url,
        "categories": cats,
        "enabled": enabled,
    }
    if tags is not None:
        entry["tags"] = list(tags)
    if "locations" in raw and locs is not None:
        entry["locations"] = ALL if locs == ALL else list(locs)
    if refusal:
        entry["refusal"] = refusal  # not a column: build_plan turns it into last_error + a warning
    return entry, []


def _validate_location(raw: object, label: str) -> tuple[dict | None, list[str]]:
    """A ``locations:`` entry, resolved offline (item 30)."""
    if not isinstance(raw, dict):
        return None, [f"{label}: must be a mapping, got {type(raw).__name__}"]
    errors = [f"{label}: unknown field {k!r}" for k in sorted(set(raw) - _LOCATION_FIELDS)]
    name = raw.get("label")
    if isinstance(name, str) and name.strip():
        label = f"location {' '.join(name.split())!r}"
    texts = {}
    for k in ("label", "where", "unit", "meetup_location", "eventbrite_region"):
        v = raw.get(k, "")
        v = "" if v is None else v
        if not isinstance(v, str):
            errors.append(f"{label}: {k} must be a string")
            v = ""
        texts[k] = v
    radius = raw.get("radius", 25)
    if isinstance(radius, bool) or not isinstance(radius, int | float):
        errors.append(f"{label}: radius must be a number")
        radius = 25
    if errors:
        return None, errors
    try:
        cols = loc.clean_fields(
            texts["label"],
            texts["where"],
            radius,
            texts["unit"] or "miles",
            texts["meetup_location"],
            texts["eventbrite_region"],
        )
    except loc.LocationInvalid as exc:
        return None, [f"{label}: {exc}"]
    return cols, []


def parse_document(text: str) -> list[dict]:
    """Parse and validate a whole file's sources; raise ImportRefused naming every bad entry."""
    return parse_full(text)[1]


def parse_full(text: str) -> tuple[list[dict], list[dict]]:
    """Parse and validate a whole file: (locations, sources). Raise ImportRefused naming every
    bad entry; nothing is written before this has passed."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ImportRefused(f"not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("sources"), list):
        raise ImportRefused("expected a mapping with a 'sources:' list")
    errors: list[str] = []
    places: list[dict] = []
    raw_locations = doc.get("locations", []) or []
    if not isinstance(raw_locations, list):
        errors.append("'locations:' must be a list")
        raw_locations = []
    labels: set[str] = set()
    for i, raw in enumerate(raw_locations, 1):
        cols, errs = _validate_location(raw, f"location #{i}")
        errors.extend(errs)
        if cols:
            if cols["label"].lower() in labels:
                errors.append(f"location {cols['label']!r}: duplicate label in file")
            labels.add(cols["label"].lower())
            places.append(cols)
    entries: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(doc["sources"], 1):
        entry, errs = _validate_entry(raw, f"entry #{i}")
        errors.extend(errs)
        if entry:
            if entry["name"] in seen:
                errors.append(f"entry {entry['name']!r}: duplicate name in file")
            seen.add(entry["name"])
            entries.append(entry)
    if errors:
        raise ImportRefused(f"{len(errors)} problem(s), nothing written:\n  " + "\n  ".join(errors))
    return places, entries


# --- Plan --------------------------------------------------------------------------------


def _current(src: Source, labels: dict[int, str] | None = None) -> dict:
    out = {
        "name": src.name,
        "display_name": src.display_name or "",
        "type": src.type or "",
        "url": src.url or "",
        "categories": json.loads(src.categories or "[]"),
        "tags": source_tags(src),
        "enabled": bool(src.enabled),
    }
    ids = loc.assigned_ids(src)
    if ids is None:
        out["locations"] = ALL
    else:
        labels = labels or {}
        out["locations"] = [labels[i] for i in ids if i in labels]
    return out


_LOC_COMPARED = ("entry", "radius", "unit", "meetup_location", "eventbrite_region")


@dataclass
class Plan:
    added: list[dict] = field(default_factory=list)
    updated: list[dict] = field(default_factory=list)
    unchanged: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    would_disable: list[str] = field(
        default_factory=list
    )  # missing, still enabled, --disable-missing
    disabled: list[str] = field(default_factory=list)  # ... and actually disabled (--apply)
    changes: dict[str, list[str]] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)  # stored anyway, name -> reason
    applied: bool = False
    # Item 30: the file's search locations, by label.
    locations_added: list[dict] = field(default_factory=list)
    locations_updated: list[dict] = field(default_factory=list)
    locations_unchanged: list[dict] = field(default_factory=list)
    location_changes: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> str:
        out = (
            f"{len(self.added)} added, {len(self.updated)} updated, "
            f"{len(self.unchanged)} unchanged, {len(self.missing)} missing"
        )
        n_locs = len(self.locations_added) + len(self.locations_updated)
        if n_locs or self.locations_unchanged:
            out += (
                f"; locations: {len(self.locations_added)} added, "
                f"{len(self.locations_updated)} updated, "
                f"{len(self.locations_unchanged)} unchanged"
            )
        return out + (f", {len(self.warnings)} warning(s)" if self.warnings else "")

    def render(self) -> str:
        lines = []
        for c in self.locations_added:
            lines.append(f"  location add        {c['label']}")
        for c in self.locations_updated:
            changed = ", ".join(self.location_changes[c["label"]])
            lines.append(f"  location update     {c['label']}: {changed}")
        for c in self.locations_unchanged:
            lines.append(f"  location unchanged  {c['label']}")
        for e in self.added:
            lines.append(f"  add        {e['name']}")
        for e in self.updated:
            lines.append(f"  update     {e['name']}: {', '.join(self.changes[e['name']])}")
        for e in self.unchanged:
            lines.append(f"  unchanged  {e['name']}")
        for n, reason in self.warnings.items():
            lines.append(f"  warning    {n}: stored disabled, {reason}")
        for n in self.missing:
            if n in self.disabled:
                note = "disabled"
            elif n in self.would_disable:
                note = "would disable"
            else:
                note = "left alone"
            lines.append(f"  missing    {n} (not in file; {note})")
        head = self.summary() + (
            "" if self.applied else "  [dry run: nothing written; use --apply]"
        )
        return "\n".join([*lines, head])


def _plan_locations(session: Session, plan: Plan, places: list[dict]) -> None:
    for cols in places:
        row = loc.by_label(session, cols["label"])
        if row is None:
            plan.locations_added.append(cols)
            continue
        diff = [
            k
            for k in _LOC_COMPARED
            if (cols[k] or None) != (getattr(row, k) or None)
            and not (k == "radius" and float(cols[k]) == float(row.radius))
        ]
        if row.label != cols["label"]:
            diff.append("label")
        if diff:
            plan.locations_updated.append(cols)
            plan.location_changes[cols["label"]] = diff
        else:
            plan.locations_unchanged.append(cols)


def build_plan(
    session: Session,
    entries: list[dict],
    disable_missing: bool = False,
    places: list[dict] | None = None,
) -> Plan:
    plan = Plan()
    _plan_locations(session, plan, places or [])
    known = {r.label.lower() for r in session.query(SearchLocation).all()}
    known |= {c["label"].lower() for c in places or []}
    labels = {r.id: r.label for r in session.query(SearchLocation).all()}
    bad = [
        f"entry {e['name']!r}: unknown location {x!r} (add it under 'locations:')"
        for e in entries
        if isinstance(e.get("locations"), list)
        for x in e["locations"]
        if x.lower() not in known
    ]
    if bad:
        raise ImportRefused(f"{len(bad)} problem(s), nothing written:\n  " + "\n  ".join(bad))
    rows = {s.name: s for s in session.query(Source).all()}
    in_file = {e["name"] for e in entries}
    for e in entries:
        e = dict(e)
        if isinstance(e.get("locations"), list):  # compare by the labels as stored
            stored = {lbl.lower(): lbl for lbl in labels.values()}
            stored |= {c["label"].lower(): c["label"] for c in places or []}
            e["locations"] = [stored[x.lower()] for x in e["locations"]]
        refusal = e.pop("refusal", None)
        if refusal:
            plan.warnings[e["name"]] = refusal
        src = rows.get(e["name"])
        if src is None:
            if refusal:
                e["last_error"] = refusal
            plan.added.append(e)
            continue
        cur = _current(src, labels)
        diff = [k for k in e if k != "name" and e[k] != cur.get(k)]
        if refusal and src.last_error != refusal:
            e["last_error"] = refusal
            diff.append("last_error")
        elif not refusal and is_check_error(src.last_error):
            e["last_error"] = None  # the check passes now: clear our own record, never a scraper's
            diff.append("last_error")
        if diff:
            plan.updated.append(e)
            plan.changes[e["name"]] = diff
        else:
            plan.unchanged.append(e)
    for name in sorted(set(rows) - in_file):
        plan.missing.append(name)
        if disable_missing and rows[name].enabled:
            plan.would_disable.append(name)
    return plan


def _write(session: Session, plan: Plan) -> None:
    for cols in plan.locations_added:
        session.add(SearchLocation(**cols))
    for cols in plan.locations_updated:
        row = loc.by_label(session, cols["label"])
        for k, v in cols.items():
            setattr(row, k, v)
    session.flush()
    ids = {r.label.lower(): r.id for r in session.query(SearchLocation).all()}
    for e in plan.added:
        src = Source(**_columns(e))
        _assign(src, e, ids)
        session.add(src)
    for e in plan.updated:
        src = session.query(Source).filter_by(name=e["name"]).one()
        for k, v in _columns(e).items():
            setattr(src, k, v)
        _assign(src, e, ids)
    for name in plan.would_disable:
        session.query(Source).filter_by(name=name).one().enabled = False
    session.commit()  # one transaction: all of it or none of it


def _assign(src: Source, e: dict, ids: dict[str, int]) -> None:
    if "locations" not in e:
        return  # absent: an added source gets every location, an updated one keeps its own
    if e["locations"] == ALL:
        loc.assign(src, None)
    else:
        loc.assign(src, [ids[x.lower()] for x in e["locations"]])


def _columns(e: dict) -> dict:
    cols = {
        k: v for k, v in e.items() if k not in ("categories", "tags", "locations")
    }  # incl. last_error
    cols["categories"] = json.dumps(e["categories"])
    if "tags" in e:
        cols["tags"] = json.dumps(e["tags"])
    return cols


def run_import(
    session: Session, text: str, apply: bool = False, disable_missing: bool = False
) -> Plan:
    places, entries = parse_full(text)  # raises before anything is written
    plan = build_plan(session, entries, disable_missing, places)
    if apply:
        try:
            _write(session, plan)
        except BaseException:
            session.rollback()
            raise
        plan.applied = True
        plan.disabled = list(plan.would_disable)
        plan.would_disable = []
    return plan


def export_yaml(session: Session) -> str:
    places = session.query(SearchLocation).order_by(SearchLocation.label).all()
    labels = {r.id: r.label for r in places}
    entries = []
    for s in session.query(Source).order_by(Source.name).all():
        e = _current(s, labels)
        if not is_location_aware(s.type or ""):
            del e["locations"]  # a per-venue source searches no area
        entries.append(e)
    doc: dict = {}
    if places:
        doc["locations"] = [loc.export_entry(r) for r in places]
    doc["sources"] = entries
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


# --- CLI ---------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.sources", description=__doc__.split("\n")[0])
    ap.add_argument("--db", help="database path (default: the configured data path)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export", help="print every source as YAML")
    imp = sub.add_parser(
        "import", help="add/update sources from a YAML file (dry run unless --apply)"
    )
    imp.add_argument("file")
    imp.add_argument(
        "--apply", action="store_true", help="write the plan; the default is a dry run"
    )
    imp.add_argument(
        "--disable-missing",
        action="store_true",
        help="disable (never delete) sources absent from the file",
    )
    args = ap.parse_args(argv)

    from .config import get_config
    from .db import get_session, init_db
    from .plugins import load_plugins

    init_db(args.db)
    load_plugins(get_config().plugins_dir)  # a plugin's source type is valid in the file
    if args.cmd == "export":
        with get_session() as session:
            sys.stdout.write(export_yaml(session))
        return 0
    try:
        text = Path(args.file).read_text()
    except OSError as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    try:
        with get_session() as session:
            plan = run_import(session, text, apply=args.apply, disable_missing=args.disable_missing)
    except ImportRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(plan.render())
    return 0


if __name__ == "__main__":
    logredact.install()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
