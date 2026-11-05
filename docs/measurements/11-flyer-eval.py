#!/usr/bin/env python3
"""Roadmap 11: score a local vision model on event flyers (title, date, start time, venue, price).

Usage:
    11-flyer-eval.py run   --endpoint URL --model NAME --variant A|B --flyers DIR --out FILE.jsonl [--today YYYY-MM-DD]
    11-flyer-eval.py score --truth TRUTH.json OUT.jsonl [OUT.jsonl ...]
    11-flyer-eval.py selftest

The flyer images and their truth stay outside this repository. TRUTH.json is a list of
{"id": "f01", "title": [..], "date": [..], "start_time": [..], "venue": [..], "price": [..],
 "on_image": {"date": bool, ...}} where each field lists every acceptable answer (None = "not stated").

Scoring per field: "correct" (matches an acceptable answer), "missing" (model gave null where a
value was expected), "wrong" (any other non-null answer, including a value the source does not state).
A wrong date is reported separately: it puts an event on the wrong day, which is worse than none.

`run` talks to an Ollama-compatible endpoint; before every request it reads /api/ps and waits
while a different model is loaded (a shared box), giving up after 30 minutes.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import statistics
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

FIELDS = ("title", "date", "start_time", "venue", "price")

PROMPTS = {
    "A": (
        "This image is an event flyer. Extract the event as JSON with exactly these keys: "
        '"title" (string), "date" (YYYY-MM-DD), "start_time" (HH:MM, 24-hour), "venue" (string), '
        '"price" (number in dollars, 0 if free). Use null for anything you cannot determine.'
    ),
    "B": (
        "Today is {today}. This image is an event flyer. Extract the event as JSON with exactly these "
        'keys: "title" (string), "date" (YYYY-MM-DD), "start_time" (HH:MM, 24-hour, the show or event '
        'start, not doors), "venue" (string), "price" (number in dollars for one ticket, 0 if it says '
        "free). Only report what is printed on the flyer: if a field is not printed on it, use null. "
        "Do not guess. If the flyer gives a month and day without a year, use the next such date on or "
        "after today."
    ),
}


# ---------------------------------------------------------------- run


def _get(url: str, timeout: float = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec B310
        return json.load(r)


def _post(url: str, body: dict, timeout: float = 900) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
        return json.load(r)


def wait_until_free(endpoint: str, model: str, limit_s: float = 1800) -> float:
    """Block while another model is loaded; return seconds waited. Raise after limit_s."""
    t0 = time.monotonic()
    while True:
        loaded = [
            m.get("name") or m.get("model") for m in _get(f"{endpoint}/api/ps").get("models", [])
        ]
        if not loaded or all(n == model for n in loaded):
            return time.monotonic() - t0
        if time.monotonic() - t0 > limit_s:
            raise SystemExit(f"BUSY: {loaded} still loaded after {limit_s:.0f}s")
        print(f"busy: {loaded}; waiting", file=sys.stderr, flush=True)
        time.sleep(60)


def run(args: argparse.Namespace) -> None:
    prompt = PROMPTS[args.variant].format(today=args.today)
    files = sorted(
        p
        for p in Path(args.flyers).iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    with open(args.out, "a") as out:
        for p in files:
            waited = wait_until_free(args.endpoint, args.model)
            body = {
                "model": args.model,
                "prompt": prompt,
                "images": [base64.b64encode(p.read_bytes()).decode()],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0, "seed": 1},
                "keep_alive": "5m",
            }
            t0 = time.monotonic()
            resp = _post(f"{args.endpoint}/api/generate", body)
            wall = time.monotonic() - t0
            rec = {
                "id": p.stem,
                "model": args.model,
                "variant": args.variant,
                "wall_s": round(wall, 2),
                "load_s": round(resp.get("load_duration", 0) / 1e9, 2),
                "waited_s": round(waited, 1),
                "raw": resp.get("response", ""),
            }
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(p.stem, rec["wall_s"], rec["raw"][:120].replace("\n", " "), flush=True)


# ---------------------------------------------------------------- score


def _norm_text(s) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(the|a|an)\b", " ", s)
    return " ".join(s.split())


def _text_match(got, want) -> bool:
    g, w = _norm_text(got), _norm_text(want)
    if not g or not w:
        return False
    # equivalent: one names the other (a flyer's "Spinphony" for a listing's "SPINPHONY Holiday Show")
    return g == w or g in w or w in g


def _norm_date(v):
    if v is None:
        return None
    m = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})", str(v))
    return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}" if m else f"unparsed:{v}"


def _norm_time(v):
    if v is None:
        return None
    s = str(v).strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        return f"unparsed:{v}"
    h, mi, ap = int(m[1]), int(m[2] or 0), m[3]
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}"


def _norm_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d+(?:\.\d+)?", str(v))
    if m:
        return float(m[0])
    return 0.0 if "free" in str(v).lower() else f"unparsed:{v}"


def _null(v) -> bool:
    return v is None or (
        isinstance(v, str)
        and v.strip().lower() in {"", "null", "none", "n/a", "unknown", "tbd", "tba"}
    )


def judge(field: str, got, accept: list) -> str:
    if _null(got):
        return "correct" if None in accept else "missing"
    wants = [a for a in accept if a is not None]
    if field in ("title", "venue"):
        ok = any(_text_match(got, w) for w in wants)
    elif field == "date":
        ok = _norm_date(got) in wants
    elif field == "start_time":
        ok = _norm_time(got) in wants
    else:
        ok = _norm_price(got) in [float(w) for w in wants]
    return "correct" if ok else "wrong"


def parse(raw: str):
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return None
        try:
            d = json.loads(m[0])
        except json.JSONDecodeError:
            return None
    if isinstance(d, list) and d and isinstance(d[0], dict):
        d = d[0]
    return d if isinstance(d, dict) else None


def score(truth: list[dict], recs: list[dict]) -> dict:
    by_id = {t["id"]: t for t in truth}
    res = {
        "n": 0,
        "parse_fail": 0,
        "fields": {f: {"correct": 0, "missing": 0, "wrong": 0} for f in FIELDS},
    }
    res["date_wrong_on_dated"] = res["date_wrong_on_undated"] = res["n_dated"] = res[
        "n_undated"
    ] = 0
    walls, per = [], []
    for r in recs:
        t = by_id[r["id"]]
        res["n"] += 1
        walls.append(r["wall_s"] - r.get("load_s", 0))
        d = parse(r["raw"])
        if d is None:
            res["parse_fail"] += 1
            d = {}
        verdicts = {f: judge(f, d.get(f), t[f]) for f in FIELDS}
        for f, v in verdicts.items():
            res["fields"][f][v] += 1
        dated = t.get("on_image", {}).get("date", True)
        res["n_dated" if dated else "n_undated"] += 1
        if verdicts["date"] == "wrong":
            res["date_wrong_on_dated" if dated else "date_wrong_on_undated"] += 1
        per.append({"id": r["id"], **verdicts, "got": d})
    res["median_s"] = round(statistics.median(walls), 2) if walls else None
    res["max_load_s"] = max((r.get("load_s", 0) for r in recs), default=0)
    res["per_flyer"] = per
    return res


# ---------------------------------------------------------------- self-test


def selftest() -> None:
    truth = [
        {
            "id": "x1",
            "title": ["Night Show"],
            "date": ["2030-01-02"],
            "start_time": ["19:00"],
            "venue": ["Example Hall"],
            "price": [20],
            "on_image": {"date": True},
        },
        {
            "id": "x2",
            "title": ["Day Thing"],
            "date": ["2030-02-03"],
            "start_time": [None],
            "venue": ["Example Hall"],
            "price": [None],
            "on_image": {"date": False},
        },
    ]
    perfect = [
        {
            "id": "x1",
            "wall_s": 2,
            "raw": '{"title":"NIGHT SHOW","date":"2030-01-02","start_time":"7 PM","venue":"The Example Hall","price":"$20"}',
        },
        {
            "id": "x2",
            "wall_s": 4,
            "raw": '{"title":"Day Thing","date":"2030-02-03","start_time":null,"venue":"Example Hall","price":null}',
        },
    ]
    bad = [
        {
            "id": "x1",
            "wall_s": 2,
            "raw": '{"title":"Other","date":"2030-01-03","start_time":"20:00","venue":"Elsewhere","price":25}',
        },
        {"id": "x2", "wall_s": 4, "raw": "not json"},
    ]
    g = score(truth, perfect)
    assert all(g["fields"][f]["correct"] == 2 for f in FIELDS), g["fields"]
    assert g["parse_fail"] == 0 and g["date_wrong_on_dated"] == 0 and g["median_s"] == 3
    b = score(truth, bad)
    assert all(b["fields"][f]["correct"] == 0 or f in ("start_time", "price") for f in FIELDS), b[
        "fields"
    ]
    assert b["fields"]["date"] == {"correct": 0, "missing": 1, "wrong": 1}, b["fields"]["date"]
    assert b["parse_fail"] == 1 and b["date_wrong_on_dated"] == 1
    assert (
        judge("date", "2030-01-02", [None]) == "wrong"
    )  # a date the source does not state is wrong
    assert judge("price", None, [20]) == "missing"
    assert judge("start_time", "19:00", [None, "19:00"]) == "correct"
    print("selftest ok")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--endpoint", required=True)
    r.add_argument("--model", required=True)
    r.add_argument("--variant", choices=sorted(PROMPTS), required=True)
    r.add_argument("--flyers", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--today", default=time.strftime("%Y-%m-%d"))
    s = sub.add_parser("score")
    s.add_argument("--truth", required=True)
    s.add_argument("--per-flyer", action="store_true")
    s.add_argument("outs", nargs="+")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "selftest":
        selftest()
    elif a.cmd == "run":
        run(a)
    else:
        selftest()
        truth = json.loads(Path(a.truth).read_text())
        for o in a.outs:
            recs = [json.loads(line) for line in Path(o).read_text().splitlines() if line.strip()]
            res = score(truth, recs)
            if not a.per_flyer:
                res.pop("per_flyer")
            print(o)
            print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
