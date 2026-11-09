"""Offline lookup of a search location (item 30): a US ZIP code, a US city and state, or
latitude/longitude typed by hand.

Nothing here touches the network: ZIP codes and cities resolve from the US Census Bureau
Gazetteer files bundled in the image (``app/geodata``, trimmed by
``scripts/gazetteer_trim.py``; vintage and source in ``SOURCE.md`` there). A location outside
the US is entered as latitude/longitude.

``GAZETTEER_DIR`` points the lookup at another directory (the tests use a small synthetic one).
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_DIR = Path(__file__).parent / "geodata"

# Fixed refusal text per reason (never the input echoed back into an error page).
MESSAGES = {
    "empty": 'Enter a US ZIP code, a US city with its state ("New York, NY"), '
    "or latitude,longitude.",
    "zip_unknown": "That ZIP code is not in the bundled US Census ZIP code list. "
    "Try a neighbouring ZIP code, the city and state, or latitude,longitude.",
    "city_unknown": "That city is not in the bundled US Census list of places for that state. "
    "Check the spelling and the two-letter state code, or enter a ZIP code or latitude,longitude.",
    "state_unknown": 'Use the two-letter state code after the city ("Los Angeles, CA").',
    "latlong_range": "Latitude must be between -90 and 90 and longitude between -180 and 180.",
    "unparsed": 'Enter a US ZIP code, a US city with its state ("New York, NY"), '
    "or latitude,longitude.",
}

_ZIP_RE = re.compile(r"^(\d{5})(?:-\d{4})?$")
_LATLONG_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$")
_CITY_RE = re.compile(r"^(.+?)\s*,\s*([A-Za-z]{2})$")


class LocationRefused(ValueError):
    """The entry does not resolve; ``code`` keys ``MESSAGES``."""

    def __init__(self, code: str):
        super().__init__(MESSAGES[code])
        self.code = code


@dataclass(frozen=True)
class Resolved:
    kind: str  # "zip" | "city" | "latlong"
    lat: float
    lon: float
    city: str = ""  # the place (named, or the nearest one) and its state; "" outside the US
    state: str = ""


def _dir() -> Path:
    return Path(os.environ.get("GAZETTEER_DIR") or DEFAULT_DIR)


def _norm(name: str) -> str:
    s = name.lower().replace(".", " ")
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"\bsainte\b", "ste", s)
    return re.sub(r"\s+", " ", s).strip()


def _aliases(name: str) -> list[str]:
    """Shorter names a place is also known by: "San Buenaventura (Ventura)" -> both halves;
    "Nashville-Davidson" -> "Nashville"; "Louisville/Jefferson County" -> "Louisville"."""
    out = []
    m = re.match(r"^(.*?)\s*\((.+)\)$", name)
    if m:
        out += [m.group(1), m.group(2)]
    for sep in ("-", "/"):
        if sep in name:
            out.append(name.split(sep, 1)[0])
    return [a for a in out if a.strip()]


@dataclass(frozen=True)
class _Data:
    zcta: dict[str, tuple[float, float]]
    places: dict[
        tuple[str, str], tuple[str, float, float]
    ]  # (state, norm name) -> (name, lat, lon)
    points: tuple[tuple[float, float, str, str, float], ...]  # lat, lon, name, state, km²


@lru_cache(maxsize=4)
def _load(directory: str) -> _Data:
    d = Path(directory)
    zcta: dict[str, tuple[float, float]] = {}
    with (d / "zcta.tsv").open(encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 3:
                zcta[parts[0]] = (float(parts[1]), float(parts[2]))
    places: dict[tuple[str, str], tuple[str, float, float]] = {}
    aliases: dict[tuple[str, str], tuple[str, float, float]] = {}
    points = []
    with (d / "places.tsv").open(encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 6:
                continue
            _geoid, name, state, lat, lon, area = parts
            row = (name, float(lat), float(lon))
            places.setdefault((state.upper(), _norm(name)), row)
            for alias in _aliases(name):
                aliases.setdefault((state.upper(), _norm(alias)), row)
            points.append((row[1], row[2], name, state.upper(), float(area)))
    for k, v in aliases.items():  # an exact name always wins over another place's alias
        places.setdefault(k, v)
    return _Data(zcta, places, tuple(points))


def data() -> _Data:
    return _load(str(_dir()))


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))


NEAREST_MAX_KM = 50.0  # a hand-typed point farther than this from any US place has no place


def nearest_place(lat: float, lon: float, max_km: float = NEAREST_MAX_KM) -> tuple[str, str]:
    """The (name, state) of the place a point lies in, or ("", "") if no bundled place is within
    ``max_km``. Used for the Meetup and Eventbrite searches, which take a city, and for the
    label of a location the migration creates.

    The files hold one point per place (its internal point) and its land area, not its outline,
    so "lies in" is approximated: the largest place whose equal-area circle around its point
    contains the point, else the place whose point is nearest. Nearest alone names a small
    enclave whose point happens to be closer to a downtown than the surrounding city's own.
    """
    nearest, nearest_d = ("", ""), max_km
    inside, inside_area = ("", ""), -1.0
    for plat, plon, name, state, area in data().points:
        if abs(plat - lat) > 1.0 or abs(plon - lon) > 1.5:  # cheap box before the haversine
            continue
        d = _km(lat, lon, plat, plon)
        if d <= nearest_d:
            nearest, nearest_d = (name, state), d
        if d <= math.sqrt(area / math.pi) and area > inside_area:
            inside, inside_area = (name, state), area
    return inside if inside[0] else nearest


def resolve(entry: str) -> Resolved:
    """Resolve what the admin typed, offline; raise ``LocationRefused`` when it does not."""
    text = (entry or "").strip()
    if not text:
        raise LocationRefused("empty")
    m = _ZIP_RE.match(text)
    if m:
        hit = data().zcta.get(m.group(1))
        if hit is None:
            raise LocationRefused("zip_unknown")
        city, state = nearest_place(*hit)
        return Resolved("zip", hit[0], hit[1], city, state)
    m = _LATLONG_RE.match(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise LocationRefused("latlong_range")
        city, state = nearest_place(lat, lon)
        return Resolved("latlong", lat, lon, city, state)
    m = _CITY_RE.match(text)
    if m:
        state = m.group(2).upper()
        if not any(s == state for s, _ in data().places):
            raise LocationRefused("state_unknown")
        hit = data().places.get((state, _norm(m.group(1))))
        if hit is None:
            raise LocationRefused("city_unknown")
        return Resolved("city", hit[1], hit[2], hit[0], state)
    if "," not in text and not any(ch.isdigit() for ch in text):
        raise LocationRefused("state_unknown")  # a bare city name: ask for its state
    raise LocationRefused("unparsed")
