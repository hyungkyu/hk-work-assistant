from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from fastapi import FastAPI, HTTPException, Query
from psycopg.rows import dict_row


app = FastAPI(
    title="RLWRLD Worklog",
    description="Read-only local activity timeline API",
    version="0.1.0",
)


@contextmanager
def database() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    with psycopg.connect(url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        yield connection


@app.get("/healthz")
def health() -> dict[str, str]:
    try:
        with database() as connection:
            connection.execute("SELECT 1").fetchone()
    except Exception as error:
        raise HTTPException(status_code=503, detail="database unavailable") from error
    return {"status": "ok"}


@app.get("/api/v1/timeline")
def timeline(
    limit: int = Query(default=100, ge=1, le=500),
    source: str | None = Query(default=None, pattern="^(slack|google_calendar|github)$"),
    actor: str | None = None,
    container: str | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    parameters: dict[str, Any] = {"limit": limit}
    if source:
        clauses.append("source = %(source)s")
        parameters["source"] = source
    if actor:
        clauses.append("actor_external_id = %(actor)s")
        parameters["actor"] = actor
    if container:
        clauses.append("container_id = %(container)s")
        parameters["container"] = container
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT event_id, source, event_type, external_id, actor_external_id,
               occurred_at, updated_at, container_id, thread_id, permalink,
               classifications, payload,
               COALESCE(
                   (
                       SELECT jsonb_agg(
                           jsonb_build_object(
                               'target_id', m.target_external_id,
                               'kind', m.kind,
                               'direction', m.direction,
                               'priority', m.priority
                           ) ORDER BY m.priority DESC, m.id
                       )
                       FROM mentions m
                       WHERE m.event_id = timeline_events.event_id
                   ),
                   '[]'::jsonb
               ) AS mentions,
               COALESCE(
                   (
                       SELECT jsonb_agg(
                           jsonb_build_object(
                               'kind', a.kind,
                               'confidence', a.confidence,
                               'state', a.state,
                               'rule_id', a.rule_id
                           ) ORDER BY a.kind, a.id
                       )
                       FROM action_candidates a
                       WHERE a.event_id = timeline_events.event_id
                   ),
                   '[]'::jsonb
               ) AS action_candidates
        FROM timeline_events
        {where}
        ORDER BY occurred_at DESC, event_id DESC
        LIMIT %(limit)s
    """
    with database() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return {"items": rows, "count": len(rows)}
