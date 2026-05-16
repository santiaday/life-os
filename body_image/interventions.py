"""CRUD for body_image_intervention.

Interventions are discrete behavior changes you want overlaid on every
dashboard trend chart — vertical markers at the date, hover for context.
Examples: "started tret 0.025% 2026-06-01", "fresh haircut 2026-05-25".
The dashboard reads /api/interventions; ad-hoc SQL can JOIN them to
mart_body_image_daily for lag analysis.
"""

from __future__ import annotations

from datetime import date

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lifeos_core.db import tx

from .schemas import Intervention, InterventionCreate


def create_intervention(user_id: str, req: InterventionCreate) -> Intervention:
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO body_image_intervention
              (user_id, intervention_key, event, occurred_on, metadata)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            [
                user_id, req.intervention_key, req.event,
                req.occurred_on, Jsonb(req.metadata or {}),
            ],
        )
        row = cur.fetchone()
    assert row is not None
    return _row_to_model(row)


def list_interventions(
    user_id: str,
    *,
    intervention_key: str | None = None,
    since: date | None = None,
) -> list[Intervention]:
    sql = [
        "SELECT * FROM body_image_intervention",
        "WHERE user_id = %s",
    ]
    params: list = [user_id]
    if intervention_key:
        sql.append("AND intervention_key = %s")
        params.append(intervention_key)
    if since:
        sql.append("AND occurred_on >= %s")
        params.append(since)
    sql.append("ORDER BY occurred_on DESC, id DESC")
    with tx() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("\n".join(sql), params)
        rows = cur.fetchall()
    return [_row_to_model(r) for r in rows]


def delete_intervention(user_id: str, intervention_id: int) -> bool:
    with tx() as c, c.cursor() as cur:
        cur.execute(
            """
            DELETE FROM body_image_intervention
             WHERE user_id = %s AND id = %s
             RETURNING id
            """,
            [user_id, intervention_id],
        )
        return cur.fetchone() is not None


def _row_to_model(row: dict) -> Intervention:
    return Intervention(
        id=row["id"],
        intervention_key=row["intervention_key"],
        event=row["event"],
        occurred_on=row["occurred_on"],
        metadata=row.get("metadata") or {},
        created_at=row["created_at"],
    )
