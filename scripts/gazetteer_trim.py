"""Trim the US Census Bureau Gazetteer files to what the offline location lookup reads (item 30).

    python3 -I scripts/gazetteer_trim.py <ZCTA .txt> <Places .txt> app/geodata

The inputs are the national files, unzipped (pipe-delimited, one header line):
``<year>_Gaz_zcta_national.txt`` and ``<year>_Gaz_place_national.txt`` from
``https://www2.census.gov/geo/docs/maps-data/data/gazetteer/<year>_Gazetteer/``.
The vintage, source URLs and checksums of the files bundled now are in
``app/geodata/SOURCE.md``; a refresh is a re-run of this script and an edit there.

Writes, tab-separated, no header:

* ``zcta.tsv``: ZIP code (ZCTA), latitude, longitude.
* ``places.tsv``: GEOID, name, state (USPS code), latitude, longitude, land area in km² (the
  lookup names the place a point lies in by it: a small enclave's centre can be nearer to a
  point downtown than the surrounding city's centre). The name loses its
  legal-status suffix ("New York city" -> "New York", "CDP", "(balance)"). One row per state
  and name: an active government (``FUNCSTAT`` A) wins over a statistical place, then the
  larger land area.

Coordinates are the internal point (``INTPTLAT``/``INTPTLONG``) rounded to 4 decimals (about
11 m), which is far below any search radius.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

# The legal/statistical description the Census appends to a place name: trailing lowercase
# words ("city", "town", "borough", "metro government"), "CDP", and "(balance)".
_SUFFIX = re.compile(r"(?:\s+\(balance\))?$")
_LSAD = re.compile(r"\s+(?:CDP|[a-z][a-z/-]*(?:\s+[a-z][a-z/-]*)*)$")


def place_name(raw: str) -> str:
    name = _SUFFIX.sub("", raw.strip())
    stripped = _LSAD.sub("", name)
    return stripped or name


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter="|"))
    # The national files pad the last header with spaces in some vintages.
    return [{k.strip(): (v or "").strip() for k, v in r.items()} for r in rows]


def _coord(v: str) -> str:
    return f"{float(v):.4f}"


def trim_zcta(src: Path) -> list[str]:
    out = []
    for r in _rows(src):
        out.append(f"{r['GEOID']}\t{_coord(r['INTPTLAT'])}\t{_coord(r['INTPTLONG'])}")
    return sorted(out)


def trim_places(src: Path) -> list[str]:
    best: dict[tuple[str, str], tuple[tuple, str]] = {}
    for r in _rows(src):
        name = place_name(r["NAME"])
        rank = (r.get("FUNCSTAT") == "A", int(r.get("ALAND") or 0))
        area = int(r.get("ALAND") or 0) / 1e6
        line = (
            f"{r['GEOID']}\t{name}\t{r['USPS']}\t{_coord(r['INTPTLAT'])}\t"
            f"{_coord(r['INTPTLONG'])}\t{area:.2f}"
        )
        k = (r["USPS"], name.lower())
        if k not in best or rank > best[k][0]:
            best[k] = (rank, line)
    return [line for _, (_, line) in sorted(best.items())]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    zcta, places, out = (Path(a) for a in argv)
    out.mkdir(parents=True, exist_ok=True)
    z = trim_zcta(zcta)
    p = trim_places(places)
    (out / "zcta.tsv").write_text("\n".join(z) + "\n", encoding="utf-8")
    (out / "places.tsv").write_text("\n".join(p) + "\n", encoding="utf-8")
    print(f"zcta.tsv: {len(z)} rows; places.tsv: {len(p)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
