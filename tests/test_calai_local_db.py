"""Tests for the Cal AI CoreData (Model.sqlite) reader."""
from __future__ import annotations

import plistlib
import sqlite3
import uuid

from ingest_calai.ingest import _extract
from ingest_calai.local_db import (
    _nsdate_to_dt,
    _unarchive_json,
    _uuid_from_blob,
    read_food_entities,
)
from ingest_calai.transforms import food_to_log_row


def test_nsdate_to_dt():
    # CoreData NSDate (secs since 2001-01-01 UTC) -> the 2026-06-15 dinner entry.
    dt = _nsdate_to_dt(803256330.665779)
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 6, 15, 22, 45)
    assert _nsdate_to_dt(None) is None


def test_uuid_from_blob():
    u = uuid.UUID("7d617096-8080-4859-bdfe-bc6c945840c7")
    assert _uuid_from_blob(u.bytes) == str(u)
    assert _uuid_from_blob(b"short") is None
    assert _uuid_from_blob(None) is None


def _nskeyed_json_blob(json_text: str) -> bytes:
    """A minimal NSKeyedArchiver bplist whose payload is a JSON string (the exact
    shape Cal AI stores ZINGREDIENTS / ZSERVINGTYPES in)."""
    return plistlib.dumps({
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": ["$null", json_text.encode("utf-8")],
    }, fmt=plistlib.FMT_BINARY)


def test_unarchive_json():
    blob = _nskeyed_json_blob('[{"name":"Salmon","calories":426,"protein":64}]')
    out = _unarchive_json(blob)
    assert isinstance(out, list) and out[0]["name"] == "Salmon"
    assert _unarchive_json(None) is None
    assert _unarchive_json(b"not a plist") is None


_DDL = """CREATE TABLE ZFOODENTITY (
  Z_PK INTEGER PRIMARY KEY, ZRATING INTEGER,
  ZCARBS FLOAT, ZDATE TIMESTAMP, ZETHANOLCARBRATIO FLOAT, ZFATS FLOAT, ZFIBER FLOAT,
  ZPROTEIN FLOAT, ZQUANTITY FLOAT, ZSERVINGCALORIES FLOAT, ZSERVINGS FLOAT,
  ZSODIUM FLOAT, ZSUGAR FLOAT, ZBARCODE VARCHAR, ZBRAND VARCHAR, ZIMAGE VARCHAR,
  ZMEALCATEGORY VARCHAR, ZNAME VARCHAR, ZTIP1 VARCHAR, ZTIP2 VARCHAR, ZTIP3 VARCHAR,
  ZTRACEID VARCHAR, ZID BLOB, ZINGREDIENTS BLOB )"""


def _make_db(path: str):
    con = sqlite3.connect(path)
    con.execute(_DDL)
    con.execute(
        "INSERT INTO ZFOODENTITY (Z_PK, ZRATING, ZCARBS, ZDATE, ZETHANOLCARBRATIO, "
        "ZFATS, ZFIBER, ZPROTEIN, ZQUANTITY, ZSERVINGCALORIES, ZSERVINGS, ZSODIUM, "
        "ZSUGAR, ZIMAGE, ZMEALCATEGORY, ZNAME, ZTIP1, ZTRACEID, ZID, ZINGREDIENTS) "
        "VALUES (1, 3, 54.0, 803256330.665779, -1.0, 24.0, 0.0, 57.0, 2.0, 658.0, 2.0, "
        "0.0, 0.0, '7D617096.jpg', 'dinner', 'Ground Turkey', 'tip', 'trace-1', ?, ?)",
        (uuid.UUID("7d617096-8080-4859-bdfe-bc6c945840c7").bytes,
         _nskeyed_json_blob('[{"name":"Turkey","calories":420,"protein":49.5}]')),
    )
    con.commit()
    con.close()


def test_read_food_entities_and_transform(tmp_path):
    db = tmp_path / "Model.sqlite"
    _make_db(str(db))
    entries = read_food_entities(str(db))
    assert len(entries) == 1
    e = entries[0]
    assert e["name"] == "Ground Turkey"
    assert e["mealCategory"] == "dinner"
    assert e["id"] == "7d617096-8080-4859-bdfe-bc6c945840c7"
    assert len(e["ingredients"]) == 1 and e["ingredients"][0]["name"] == "Turkey"

    # Full pipeline mapping: quantity (2) is the multiplier on per-serving 658.
    ex = _extract(e)
    assert ex["meal_group"] == "dinner"
    row = food_to_log_row(ex["food"], entry_id=ex["entry_id"], logged_at=ex["logged_at"],
                          meal_group=ex.get("meal_group"))
    assert row["energy_kcal"] == 658.0 * 2
    assert row["protein_g"] == 57.0 * 2
    assert row["meal_group"] == "dinner"
    # idempotency key derives from the stable entry id.
    assert row["source"] == "calai"
    assert row["source_row_hash"]
