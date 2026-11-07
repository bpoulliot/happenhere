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
``ticketmaster`` entry without a location in its URL is judged the same way (item 22).
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

from . import logredact
from .models import Source
from .netguard import NonPublicURL, require_public_url
from .scrapers import ticketmaster
from .scrapers.registry import scraper_types

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[\w.-]+$")
_TAG_RE = re.compile(r"^[a-z0-9-]+$")
_FIELDS = {"name", "display_name", "type", "url", "categories", "tags", "enabled"}


class ImportRefused(ValueError):
    """The file is not importable; the message names every bad entry."""


# A ``last_error`` written by a failed import/admin check (not by a scraper) starts with one of
# these marks, so a later pass can tell its own record from a scraper's and clear only its own.
GUARD_REFUSAL_MARK = "url refused: "
LOCATION_MISSING_MARK = "location missing: "  # item 22: a regional Ticketmaster URL
CHECK_ERROR_MARKS: tuple[str, ...] = (GUARD_REFUSAL_MARK, LOCATION_MISSING_MARK)


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
    """Fixed text when a source of this type cannot run from this URL (item 22), else None.

    Only the regional ``ticketmaster`` type has such a rule: its URL carries its location.
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
                    enabled, label, errors, problem, f"{LOCATION_MISSING_MARK}{problem}"
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
    if refusal:
        entry["refusal"] = refusal  # not a column: build_plan turns it into last_error + a warning
    return entry, []


def parse_document(text: str) -> list[dict]:
    """Parse and validate a whole file; raise ImportRefused naming every bad entry."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ImportRefused(f"not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("sources"), list):
        raise ImportRefused("expected a mapping with a 'sources:' list")
    entries: list[dict] = []
    errors: list[str] = []
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
    return entries


# --- Plan --------------------------------------------------------------------------------


def _current(src: Source) -> dict:
    return {
        "name": src.name,
        "display_name": src.display_name or "",
        "type": src.type or "",
        "url": src.url or "",
        "categories": json.loads(src.categories or "[]"),
        "tags": source_tags(src),
        "enabled": bool(src.enabled),
    }


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

    def summary(self) -> str:
        out = (
            f"{len(self.added)} added, {len(self.updated)} updated, "
            f"{len(self.unchanged)} unchanged, {len(self.missing)} missing"
        )
        return out + (f", {len(self.warnings)} warning(s)" if self.warnings else "")

    def render(self) -> str:
        lines = []
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


def build_plan(session: Session, entries: list[dict], disable_missing: bool = False) -> Plan:
    plan = Plan()
    rows = {s.name: s for s in session.query(Source).all()}
    in_file = {e["name"] for e in entries}
    for e in entries:
        e = dict(e)
        refusal = e.pop("refusal", None)
        if refusal:
            plan.warnings[e["name"]] = refusal
        src = rows.get(e["name"])
        if src is None:
            if refusal:
                e["last_error"] = refusal
            plan.added.append(e)
            continue
        cur = _current(src)
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
    for e in plan.added:
        session.add(Source(**_columns(e)))
    for e in plan.updated:
        src = session.query(Source).filter_by(name=e["name"]).one()
        for k, v in _columns(e).items():
            setattr(src, k, v)
    for name in plan.would_disable:
        session.query(Source).filter_by(name=name).one().enabled = False
    session.commit()  # one transaction: all of it or none of it


def _columns(e: dict) -> dict:
    cols = {k: v for k, v in e.items() if k not in ("categories", "tags")}  # incl. last_error
    cols["categories"] = json.dumps(e["categories"])
    if "tags" in e:
        cols["tags"] = json.dumps(e["tags"])
    return cols


def run_import(
    session: Session, text: str, apply: bool = False, disable_missing: bool = False
) -> Plan:
    entries = parse_document(text)  # raises before anything is written
    plan = build_plan(session, entries, disable_missing)
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
    entries = [_current(s) for s in session.query(Source).order_by(Source.name).all()]
    return yaml.safe_dump({"sources": entries}, sort_keys=False, allow_unicode=True)


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
