"""Helpers for the `ingestion_runs` log table.

Every ingester opens a run row at start, closes it at end with status, row
counts, and any error. Pattern:

    with ingestion_run("whoop", "recovery") as run:
        rows = fetch_and_upsert(...)
        run.fetched(len(rows))
        run.upserted(len(rows))
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from lifeos_core.db import tx
from lifeos_core.logging import get_logger

log = get_logger(__name__)


class Run:
    def __init__(self, run_id: int, source: str, data_type: str) -> None:
        self.id = run_id
        self.source = source
        self.data_type = data_type
        self._fetched = 0
        self._upserted = 0
        self._metadata: dict = {}

    def fetched(self, n: int) -> None:
        self._fetched = n

    def upserted(self, n: int) -> None:
        self._upserted = n

    def add_metadata(self, **kwargs) -> None:
        self._metadata.update(kwargs)


@contextmanager
def ingestion_run(source: str, data_type: str, **metadata) -> Iterator[Run]:
    """Open a run, hand it to the caller, close on exit. Always closes —
    failure or success."""
    log.info("ingest.start", source=source, data_type=data_type)

    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_runs (source, data_type, status, metadata)
            VALUES (%s, %s, 'running', %s::jsonb)
            RETURNING id
            """,
            [source, data_type, _to_jsonb(metadata)],
        )
        run_id = cur.fetchone()["id"]

    run = Run(run_id, source, data_type)
    if metadata:
        run.add_metadata(**metadata)

    error: BaseException | None = None
    try:
        yield run
    except BaseException as e:
        error = e
        raise
    finally:
        status = "failure" if error is not None else "success"
        err_msg = (
            f"{type(error).__name__}: {error}"[:8000] if error is not None else None
        )
        with tx() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE ingestion_runs SET
                  finished_at = now(),
                  status = %s,
                  rows_fetched = %s,
                  rows_upserted = %s,
                  error_message = %s,
                  metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                WHERE id = %s
                """,
                [
                    status,
                    run._fetched,
                    run._upserted,
                    err_msg,
                    _to_jsonb(run._metadata),
                    run.id,
                ],
            )
        log.info(
            "ingest.end",
            source=source,
            data_type=data_type,
            status=status,
            fetched=run._fetched,
            upserted=run._upserted,
            error=err_msg,
        )


def last_successful_run(source: str, data_type: str | None = None) -> dict | None:
    """Most recent successful ingestion_runs row for (source[, data_type])."""
    with tx() as c, c.cursor() as cur:
        if data_type is not None:
            cur.execute(
                """
                SELECT * FROM ingestion_runs
                WHERE source = %s AND data_type = %s AND status = 'success'
                ORDER BY started_at DESC LIMIT 1
                """,
                [source, data_type],
            )
        else:
            cur.execute(
                """
                SELECT * FROM ingestion_runs
                WHERE source = %s AND status = 'success'
                ORDER BY started_at DESC LIMIT 1
                """,
                [source],
            )
        return cur.fetchone()


def _to_jsonb(d: dict) -> str:
    import json

    return json.dumps(d, default=str)
