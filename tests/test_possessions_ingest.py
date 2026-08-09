"""Batch ingest: discovery, hashing, and the idempotency that makes a re-run
safe.

The single most expensive bug this module can have is a re-run that duplicates
the inventory, so most of what's here is about the hash gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from possessions import ingest

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _write(root: Path, name: str, data: bytes = b"x") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_discovery_finds_images_recursively_and_ignores_everything_else(tmp_path):
    _write(tmp_path, "a.jpg")
    _write(tmp_path, "nested/b.PNG")
    _write(tmp_path, "notes.txt")
    _write(tmp_path, "nested/c.heic")
    found = {p.name for p in ingest.discover(tmp_path)}
    assert found == {"a.jpg", "b.PNG", "c.heic"}


def test_discovery_skips_appledouble_stubs(tmp_path):
    """macOS scatters ._foo.jpg files that are metadata, not images. Feeding
    one to the segmenter is a failed item and a wasted vision call."""
    _write(tmp_path, "a.jpg")
    _write(tmp_path, "._a.jpg")
    assert [p.name for p in ingest.discover(tmp_path)] == ["a.jpg"]


def test_discovery_order_is_stable(tmp_path):
    for name in ("c.jpg", "a.jpg", "b.jpg"):
        _write(tmp_path, name)
    assert [p.name for p in ingest.discover(tmp_path)] == ["a.jpg", "b.jpg", "c.jpg"]


def test_discovery_accepts_a_single_file(tmp_path):
    path = _write(tmp_path, "only.jpg")
    assert ingest.discover(path) == [path]


def test_discovery_on_a_missing_folder_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest.discover(tmp_path / "nope")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def test_hash_is_content_addressed_not_name_addressed(tmp_path):
    """Two copies of the same photo under different names are one item."""
    a = _write(tmp_path, "front.jpg", b"identical bytes")
    b = _write(tmp_path, "copy/front-2.jpg", b"identical bytes")
    assert ingest.hash_bytes(a.read_bytes()) == ingest.hash_bytes(b.read_bytes())


def test_different_content_hashes_differently():
    assert ingest.hash_bytes(b"one") != ingest.hash_bytes(b"two")


def test_existing_hashes_short_circuits_on_an_empty_input(monkeypatch):
    """No query should be issued for an empty batch — the DB isn't reachable
    in unit tests, so this also proves the guard fires before any connection."""
    def explode():
        raise AssertionError("should not have opened a connection")
    monkeypatch.setattr(ingest, "conn", explode)
    assert ingest.existing_hashes([]) == set()


# ---------------------------------------------------------------------------
# The idempotency gate
# ---------------------------------------------------------------------------

@pytest.fixture
def quiet_storage(monkeypatch):
    monkeypatch.setattr(ingest.storage, "ensure_bucket", lambda: False)


def test_already_ingested_photos_are_skipped_without_reprocessing(
        tmp_path, monkeypatch, quiet_storage):
    old = _write(tmp_path, "old.jpg", b"already have this")
    new = _write(tmp_path, "new.jpg", b"this one is new")
    old_hash = ingest.hash_bytes(old.read_bytes())

    monkeypatch.setattr(ingest, "existing_hashes", lambda _h: {old_hash})
    processed: list[str] = []

    def fake_process(path, **kwargs):
        processed.append(path.name)
        return ingest.ItemResult(path=str(path), status="created",
                                 possession_id="id", name=path.stem)
    monkeypatch.setattr(ingest, "process_one", fake_process)

    result = ingest.ingest_folder(tmp_path, workers=1)

    assert processed == ["new.jpg"]          # the expensive path ran once
    assert result.created == 1
    assert result.skipped == 1
    assert result.failed == 0
    assert new.name in processed[0]


def test_a_second_run_over_the_same_folder_creates_nothing(
        tmp_path, monkeypatch, quiet_storage):
    """The whole point of the hash key: re-running is a no-op, which is also
    how a batch that died halfway is resumed."""
    _write(tmp_path, "a.jpg", b"aaa")
    _write(tmp_path, "b.jpg", b"bbb")
    every_hash = {ingest.hash_bytes(b"aaa"), ingest.hash_bytes(b"bbb")}
    monkeypatch.setattr(ingest, "existing_hashes", lambda _h: every_hash)
    monkeypatch.setattr(ingest, "process_one",
                        lambda *a, **k: pytest.fail("nothing should be processed"))

    result = ingest.ingest_folder(tmp_path, workers=1)
    assert (result.created, result.skipped, result.failed) == (0, 2, 0)


def test_one_failing_item_does_not_stop_the_batch(
        tmp_path, monkeypatch, quiet_storage):
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        _write(tmp_path, name, name.encode())
    monkeypatch.setattr(ingest, "existing_hashes", lambda _h: set())

    def flaky(path, **kwargs):
        if path.name == "b.jpg":
            return ingest.ItemResult(path=str(path), status="failed",
                                     error="RuntimeError: segmentation blew up")
        return ingest.ItemResult(path=str(path), status="created",
                                 possession_id="id", name=path.stem)
    monkeypatch.setattr(ingest, "process_one", flaky)

    result = ingest.ingest_folder(tmp_path, workers=2)
    assert result.created == 2
    assert result.failed == 1
    assert any(i.error and "blew up" in i.error for i in result.items)


def test_process_one_reports_an_unreadable_file_instead_of_raising(tmp_path):
    missing = tmp_path / "gone.jpg"
    outcome = ingest.process_one(missing)
    assert outcome.status == "failed"
    assert "unreadable" in outcome.error


def test_limit_caps_the_batch(tmp_path, monkeypatch, quiet_storage):
    for i in range(5):
        _write(tmp_path, f"{i}.jpg", str(i).encode())
    monkeypatch.setattr(ingest, "existing_hashes", lambda _h: set())
    monkeypatch.setattr(
        ingest, "process_one",
        lambda path, **k: ingest.ItemResult(path=str(path), status="created"))
    result = ingest.ingest_folder(tmp_path, workers=1, limit=2)
    assert result.created == 2


def test_progress_callback_reports_every_item(tmp_path, monkeypatch, quiet_storage):
    for i in range(3):
        _write(tmp_path, f"{i}.jpg", str(i).encode())
    monkeypatch.setattr(ingest, "existing_hashes", lambda _h: set())
    monkeypatch.setattr(
        ingest, "process_one",
        lambda path, **k: ingest.ItemResult(path=str(path), status="created"))

    seen: list[tuple[int, int]] = []
    ingest.ingest_folder(tmp_path, workers=1,
                         on_progress=lambda done, total, _o: seen.append((done, total)))
    assert seen == [(1, 3), (2, 3), (3, 3)]
