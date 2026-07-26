"""Cal AI "Summary Report" PDF loader.

Cal AI has no export API; the only way out is the in-app PDF report. It is,
however, complete: per-item name, macros, and a logged time for every day in
the requested range.

Layout, per day section:

    June 3, 2026
    Foods Calories Protein Carbs Fat Fiber Sugar Sodium Time
    Grilled Chicken Salad with
    Avocado, Arugula, Pico de Gallo,
    and Vinaigrette
    529 35g 15g 37g 7g 4g 979mg 12:45am
    Apple 72 0g 19g 0g 3g 14g 1mg 4:37pm
    TOTAL Calories eaten: 2419 Calories burned: 0 Net (eaten-burned): 2419

Food names wrap across lines and the numeric row may or may not share the last
name line, so the parser accumulates text until it sees a numeric tail and
treats everything accumulated as the name.

Times after midnight belong to the section's calendar day as Cal AI groups
them -- a 12:32am entry under "June 7" is a late-night meal the app filed
under the 7th, and re-dating it to the 6th would disagree with the app's own
daily totals.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

from lifeos_core.db import tx
from lifeos_core.logging import configure_logging, get_logger
from lifeos_core.runs import ingestion_run
from unify.common import hash_uid, jsonb, upsert

log = get_logger(__name__)

SOURCE = "cal_ai"

_DAY_RE = re.compile(r"^([A-Z][a-z]+ \d{1,2}, \d{4})$")
# kcal, protein, carbs, fat, fiber, sugar (all grams), sodium (mg), clock time
_ROW_RE = re.compile(
    r"(?P<kcal>\d[\d,]*)\s+"
    r"(?P<protein>[\d.]+)g\s+(?P<carbs>[\d.]+)g\s+(?P<fat>[\d.]+)g\s+"
    r"(?P<fiber>[\d.]+)g\s+(?P<sugar>[\d.]+)g\s+(?P<sodium>[\d.]+)mg\s+"
    r"(?P<time>\d{1,2}:\d{2}\s*[ap]m)\s*$",
    re.IGNORECASE,
)
_TOTAL_RE = re.compile(
    r"TOTAL Calories eaten:\s*(?P<eaten>[\d,]+)\s*"
    r"Calories burned:\s*(?P<burned>[\d,]+)", re.IGNORECASE)
_WEIGHT_RE = re.compile(
    r"^(?P<lb>[\d.]+)\s*lbs?\s+(?P<when>[A-Z][a-z]+ \d{1,2}, \d{4})$")
_HEADER_RE = re.compile(
    r"^(Foods\s+Calories|Santiago|Start:|Week\s+Calories|Summary for|"
    r"AVERAGE FOR|USER TARGET|Weight History|Weight Date|Weight Progress|"
    r"Overview|Daily Average|Protein Carbs Fats)", re.IGNORECASE)


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def _parse_day(s: str) -> date | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(day: date, clock: str) -> datetime:
    t = datetime.strptime(clock.replace(" ", "").upper(), "%I:%M%p").time()
    return datetime.combine(day, t)


def extract_text(pdf_path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse(text: str) -> dict:
    """-> {'entries': [...], 'totals': {day: {...}}, 'weights': [...]}"""
    entries: list[dict] = []
    totals: dict[str, dict] = {}
    weights: list[dict] = []

    current_day: date | None = None
    name_buf: list[str] = []
    seq = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        wm = _WEIGHT_RE.match(line)
        if wm:
            d = _parse_day(wm.group("when"))
            if d:
                weights.append({"day": d.isoformat(), "weight_lb": _num(wm.group("lb"))})
            continue

        dm = _DAY_RE.match(line)
        if dm:
            d = _parse_day(dm.group(1))
            if d:
                current_day = d
                name_buf = []
                seq = 0
            continue

        tm = _TOTAL_RE.search(line)
        if tm and current_day:
            totals[current_day.isoformat()] = {
                "kcal_eaten": _num(tm.group("eaten")),
                "kcal_burned": _num(tm.group("burned")),
            }
            name_buf = []
            continue

        if _HEADER_RE.match(line):
            name_buf = []
            continue

        rm = _ROW_RE.search(line)
        if rm and current_day:
            # Whatever precedes the numeric tail on this line is the last
            # fragment of the food name.
            head = line[: rm.start()].strip()
            parts = [*name_buf, head] if head else list(name_buf)
            name = " ".join(p for p in parts if p).strip(" ,;")
            entries.append({
                "day": current_day.isoformat(),
                "seq": seq,
                "food_name": name or "Unnamed item",
                "energy_kcal": _num(rm.group("kcal")),
                "protein_g": _num(rm.group("protein")),
                "carbs_g": _num(rm.group("carbs")),
                "fat_g": _num(rm.group("fat")),
                "fiber_g": _num(rm.group("fiber")),
                "sugar_g": _num(rm.group("sugar")),
                "sodium_mg": _num(rm.group("sodium")),
                "eaten_at": _parse_time(current_day, rm.group("time")).isoformat(),
                "clock": rm.group("time"),
            })
            seq += 1
            name_buf = []
            continue

        if current_day:
            name_buf.append(line)

    return {"entries": entries, "totals": totals, "weights": weights}


def load(pdf_path: str) -> dict[str, int]:
    path = Path(pdf_path).expanduser()
    parsed = parse(extract_text(path))

    out = {}
    with ingestion_run(SOURCE, "pdf_report", file=path.name) as run, tx() as conn:
        rows = [{
            "source": SOURCE, "entity": "food_entry",
            "natural_key": hash_uid("e", e["day"], e["seq"], e["food_name"],
                                    e["energy_kcal"], e["clock"]),
            "occurred_on": e["day"], "payload": jsonb(e), "file_name": path.name,
        } for e in parsed["entries"]]
        out["entries"] = upsert(conn, "raw_import", rows,
                                conflict=["source", "entity", "natural_key"],
                                update=["occurred_on", "payload", "file_name", "imported_at"])

        trows = [{
            "source": SOURCE, "entity": "daily_total", "natural_key": day,
            "occurred_on": day, "payload": jsonb(t), "file_name": path.name,
        } for day, t in parsed["totals"].items()]
        out["totals"] = upsert(conn, "raw_import", trows,
                               conflict=["source", "entity", "natural_key"],
                               update=["occurred_on", "payload", "file_name", "imported_at"])

        wrows = [{
            "source": SOURCE, "entity": "weight", "natural_key": w["day"],
            "occurred_on": w["day"], "payload": jsonb(w), "file_name": path.name,
        } for w in parsed["weights"]]
        out["weights"] = upsert(conn, "raw_import", wrows,
                                conflict=["source", "entity", "natural_key"],
                                update=["occurred_on", "payload", "file_name", "imported_at"])

        run.fetched(len(parsed["entries"]))
        run.upserted(sum(out.values()))
    log.info("calai_pdf.load", **out)
    return out


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(description="Load a Cal AI Summary Report PDF.")
    p.add_argument("--pdf", required=True)
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and print a summary without writing.")
    args = p.parse_args(argv)
    if args.dry_run:
        parsed = parse(extract_text(Path(args.pdf).expanduser()))
        days = sorted({e["day"] for e in parsed["entries"]})
        print(json.dumps({
            "entries": len(parsed["entries"]),
            "days": len(days),
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "totals": len(parsed["totals"]),
            "weights": parsed["weights"],
            "sample": parsed["entries"][:3],
        }, indent=2))
        return 0
    print(json.dumps(load(args.pdf), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
