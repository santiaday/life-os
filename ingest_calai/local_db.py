"""Read Cal AI's on-device CoreData store (Model.sqlite, extracted from an iOS
backup) and feed its diary into the SAME pipeline as the Firestore path.

Cal AI is local-first: the authoritative daily diary lives in the app's CoreData
store as ZFOODENTITY rows (Firestore only mirrors a few saved foods). Each row
carries full macros + an explicit meal category + the ingredient breakdown
(NSKeyedArchiver-wrapped JSON). We normalize each row to the exact "Firestore
diary document" dict shape that ingest.ingest_diary_entries() already consumes,
so raw_calai_food / fact_food_log / fact_food_daily all populate unchanged and
idempotently (keyed on the entry UUID, which equals the meal photo's id).

This is the historical-backfill + recovery path. Once the live Firestore
collection path is known, ongoing sync uses ingest.run_all(); both write the
same rows keyed on the same entry id, so they converge with zero duplication.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ingest_calai.ingest import ingest_diary_entries
from lifeos_core.logging import get_logger
from lifeos_core.runs import ingestion_run

log = get_logger(__name__)

CALAI_DOMAIN = "com.viraldevelopment.calai"
MOBILESYNC = Path.home() / "Library/Application Support/MobileSync/Backup"


def extract_model_sqlite(dest_dir: str | None = None,
                         backup_root: str | None = None) -> str:
    """Locate the newest UNENCRYPTED iOS backup, pull Cal AI's Model.sqlite (plus
    -wal/-shm for a consistent read), and return the local path.

    backup_root defaults to the Finder/iTunes MobileSync dir (TCC-protected —
    needs Full Disk Access). Point it at a custom dir (e.g. an `idevicebackup2
    backup --target` location, or $CALAI_BACKUP_ROOT) to avoid the FDA grant and
    run fully unattended.
    """
    root = Path(backup_root or os.environ.get("CALAI_BACKUP_ROOT") or MOBILESYNC)
    if not root.exists():
        raise RuntimeError(f"No iOS backups at {root}. Make a Finder backup first.")
    backups = [d for d in root.iterdir() if (d / "Manifest.db").exists()]
    # idevicebackup2 nests under <root>/<udid>/; also accept root itself being a backup.
    if (root / "Manifest.db").exists():
        backups.append(root)
    if not backups:
        raise RuntimeError(
            f"No unencrypted backups under {root} (encrypted backups have no "
            "plaintext Manifest.db — make an unencrypted backup).")
    bdir = max(backups, key=lambda d: (d / "Info.plist").stat().st_mtime
               if (d / "Info.plist").exists() else d.stat().st_mtime)
    dest = Path(dest_dir or tempfile.mkdtemp(prefix="calai_model_"))
    dest.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(bdir / "Manifest.db"))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT fileID, relativePath FROM Files WHERE domain LIKE ? "
            "AND (relativePath='Model.sqlite' OR relativePath LIKE 'Model.sqlite-%')",
            (f"%{CALAI_DOMAIN}%",)).fetchall()
    finally:
        con.close()
    main = None
    for r in rows:
        src = bdir / r["fileID"][:2] / r["fileID"]
        if src.exists():
            out = dest / r["relativePath"]
            shutil.copy2(src, out)
            if r["relativePath"] == "Model.sqlite":
                main = str(out)
    if not main:
        raise RuntimeError(
            "Cal AI Model.sqlite not found in the newest backup. Open Cal AI once, "
            "re-backup, and retry (or the backup may be encrypted).")
    log.info("calai.model_extracted", backup=bdir.name, path=main)
    return main

# CoreData NSDate epoch (2001-01-01 UTC) -> Unix epoch.
_NSDATE_EPOCH = 978307200.0


def _nsdate_to_dt(v) -> datetime | None:
    if v is None:
        return None
    try:
        return datetime.fromtimestamp(float(v) + _NSDATE_EPOCH, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _uuid_from_blob(b) -> str | None:
    if isinstance(b, (bytes, bytearray)) and len(b) == 16:
        return str(uuid.UUID(bytes=bytes(b)))
    if isinstance(b, str) and b:
        return b
    return None


def _unarchive_json(blob):
    """CoreData stores Cal AI's ingredient/factor arrays as an NSKeyedArchiver
    bplist whose payload is a JSON string. Return the parsed JSON (list/dict) or
    None. Robust to either a bytes or str JSON object inside $objects."""
    if not blob:
        return None
    try:
        pl = plistlib.loads(blob)
    except Exception:
        return None
    objs = pl.get("$objects") if isinstance(pl, dict) else None
    if not isinstance(objs, list):
        return None
    for o in objs:
        s = None
        if isinstance(o, (bytes, bytearray)):
            s = bytes(o).decode("utf-8", "replace")
        elif isinstance(o, str):
            s = o
        if s and s.lstrip()[:1] in "[{":
            try:
                return json.loads(s)
            except Exception:
                continue
    return None


def _health_rating(row: sqlite3.Row) -> dict | None:
    tips = {k: row[k] for k in ("ZTIP1", "ZTIP2", "ZTIP3")
            if k in row.keys() and row[k]}
    rating = row["ZRATING"] if "ZRATING" in row.keys() else None
    if not tips and rating in (None, 0):
        return None
    out = {}
    if rating is not None:
        out["rating"] = rating
    for src, dst in (("ZTIP1", "tip1"), ("ZTIP2", "tip2"), ("ZTIP3", "tip3")):
        if src in row.keys() and row[src]:
            out[dst] = row[src]
    return out or None


def _row_to_entry(row: sqlite3.Row) -> dict | None:
    """Normalize one ZFOODENTITY row -> a Firestore-diary-document-shaped dict.

    Emits the diary shape (servingCalories + quantity), NOT the /v6 analysis
    shape, so transform_food_object scales per-serving macros by quantity.
    Deliberately omits `servings` (ZSERVINGS) so `quantity` (ZQUANTITY) is the
    eaten-multiplier the app actually displays.
    """
    entry_id = _uuid_from_blob(row["ZID"]) if "ZID" in row.keys() else None
    logged_at = _nsdate_to_dt(row["ZDATE"])
    name = row["ZNAME"] if "ZNAME" in row.keys() else None
    if not entry_id or logged_at is None or not name:
        return None
    ingredients = _unarchive_json(row["ZINGREDIENTS"]) if "ZINGREDIENTS" in row.keys() else None

    entry = {
        "id": entry_id,
        "_name": f"localdb/ZFOODENTITY/{entry_id}",
        "date": logged_at.isoformat().replace("+00:00", "Z"),
        "name": name,
        "servingCalories": row["ZSERVINGCALORIES"] if "ZSERVINGCALORIES" in row.keys() else None,
        "quantity": row["ZQUANTITY"] if "ZQUANTITY" in row.keys() else None,
        "protein": row["ZPROTEIN"] if "ZPROTEIN" in row.keys() else None,
        "carbs": row["ZCARBS"] if "ZCARBS" in row.keys() else None,
        "fats": row["ZFATS"] if "ZFATS" in row.keys() else None,
        "fiber": row["ZFIBER"] if "ZFIBER" in row.keys() else None,
        "sugar": row["ZSUGAR"] if "ZSUGAR" in row.keys() else None,
        "sodium": row["ZSODIUM"] if "ZSODIUM" in row.keys() else None,
        "ingredients": ingredients if isinstance(ingredients, list) else [],
        "ethanolCarbRatio": row["ZETHANOLCARBRATIO"] if "ZETHANOLCARBRATIO" in row.keys() else None,
        "traceId": row["ZTRACEID"] if "ZTRACEID" in row.keys() else None,
        "image": row["ZIMAGE"] if "ZIMAGE" in row.keys() else None,
        "brand": row["ZBRAND"] if "ZBRAND" in row.keys() else None,
        "barcode": row["ZBARCODE"] if "ZBARCODE" in row.keys() else None,
        "mealCategory": row["ZMEALCATEGORY"] if "ZMEALCATEGORY" in row.keys() else None,
        "healthRating": _health_rating(row),
        "_source": "calai_local_coredata",
    }
    return entry


def read_food_entities(sqlite_path: str) -> list[dict]:
    """Return every ZFOODENTITY row as a Firestore-diary-shaped entry dict."""
    con = sqlite3.connect(sqlite_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM ZFOODENTITY ORDER BY ZDATE").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        e = _row_to_entry(r)
        if e is not None:
            out.append(e)
    return out


def run_local(sqlite_path: str | None = None, *, from_backup: bool = False,
              user_id: str | None = None) -> dict:
    """Backfill the warehouse from Cal AI's CoreData store. Idempotent.

    Pass an explicit Model.sqlite path, or from_backup=True to auto-locate and
    extract it from the newest local iOS backup.
    """
    if from_backup or not sqlite_path:
        sqlite_path = extract_model_sqlite()
    entries = read_food_entities(sqlite_path)
    with ingestion_run("calai", "diary_local",
                       source_path=sqlite_path, entries=len(entries)) as run:
        run.fetched(len(entries))
        n = ingest_diary_entries(entries, user_id=user_id)
        run.upserted(n)
    log.info("calai.local_backfill", entries=len(entries), written=n)
    return {"source": sqlite_path, "read": len(entries), "written": n}
