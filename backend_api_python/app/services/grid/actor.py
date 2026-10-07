"""Durable grid actor ownership, state, and execution-event mailbox."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable

from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GridActorEvent:
    id: int
    execution_event_id: int
    strategy_id: int
    grid_order_id: int
    attempts: int
    fencing_token: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "GridActorEvent":
        return cls(
            id=int(row.get("id") or 0),
            execution_event_id=int(row.get("execution_event_id") or 0),
            strategy_id=int(row.get("strategy_id") or 0),
            grid_order_id=int(row.get("grid_order_id") or 0),
            attempts=int(row.get("attempts") or 0),
            fencing_token=int(row.get("fencing_token") or 0),
        )


class GridActorRepository:
    """PostgreSQL-backed mailbox routed through the strategy runtime lease."""

    def enqueue(
        self,
        *,
        execution_event_id: int,
        strategy_id: int,
        grid_order_id: int,
    ) -> bool:
        event_id = int(execution_event_id or 0)
        sid = int(strategy_id or 0)
        order_id = int(grid_order_id or 0)
        if event_id <= 0 or sid <= 0 or order_id <= 0:
            raise ValueError("gridActor.invalidEventRoute")
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_grid_actor_events
                        (execution_event_id, strategy_id, grid_order_id, status, available_at)
                    VALUES (%s, %s, %s, 'pending', NOW())
                    ON CONFLICT (execution_event_id) DO NOTHING
                    RETURNING id
                    """,
                    (event_id, sid, order_id),
                )
                inserted = cur.fetchone() is not None
                db.commit()
                return inserted
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def claim_for_owner(
        self,
        *,
        owner_id: str,
        strategy_ids: Iterable[int],
        limit: int = 100,
        lease_seconds: int = 30,
        max_attempts: int = 8,
    ) -> list[GridActorEvent]:
        ids = sorted({int(value) for value in strategy_ids if int(value) > 0})
        if not ids:
            return []
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    WITH exhausted AS (
                      UPDATE qd_grid_actor_events
                      SET status = 'dead', completed_at = NOW(),
                          lease_expires_at = NULL,
                          error_message = CASE
                            WHEN error_message = '' THEN 'gridActor.maxAttemptsExceeded'
                            ELSE error_message
                          END,
                          updated_at = NOW()
                      WHERE attempts >= %s
                        AND strategy_id = ANY(%s)
                        AND status IN ('pending', 'failed', 'processing')
                        AND (lease_expires_at IS NULL OR lease_expires_at < NOW())
                      RETURNING execution_event_id, attempts, error_message
                    )
                    UPDATE qd_execution_events AS execution
                    SET process_attempts = GREATEST(execution.process_attempts, exhausted.attempts),
                        process_error = exhausted.error_message
                    FROM exhausted
                    WHERE execution.id = exhausted.execution_event_id
                    """,
                    (int(max_attempts), ids),
                )
                cur.execute(
                    """
                    WITH ready AS (
                        SELECT event.id
                        FROM qd_grid_actor_events AS event
                        JOIN qd_strategy_runtime_leases AS lease
                          ON lease.strategy_id = event.strategy_id
                        WHERE event.strategy_id = ANY(%s)
                          AND lease.owner_id = %s
                          AND lease.lease_expires_at >= NOW()
                          AND event.attempts < %s
                          AND event.available_at <= NOW()
                          AND (
                            event.status IN ('pending', 'failed')
                            OR (
                              event.status = 'processing'
                              AND event.lease_expires_at < NOW()
                            )
                          )
                        ORDER BY event.id
                        FOR UPDATE OF event SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE qd_grid_actor_events AS event
                    SET status = 'processing',
                        attempts = event.attempts + 1,
                        claimed_by = %s,
                        fencing_token = lease.fencing_token,
                        lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                        error_message = '',
                        updated_at = NOW()
                    FROM ready, qd_strategy_runtime_leases AS lease
                    WHERE event.id = ready.id
                      AND lease.strategy_id = event.strategy_id
                      AND lease.owner_id = %s
                      AND lease.lease_expires_at >= NOW()
                    RETURNING event.*
                    """,
                    (
                        ids,
                        str(owner_id),
                        int(max_attempts),
                        max(1, int(limit)),
                        str(owner_id),
                        max(10, int(lease_seconds)),
                        str(owner_id),
                    ),
                )
                rows = [GridActorEvent.from_row(dict(row)) for row in (cur.fetchall() or [])]
                db.commit()
                return rows
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def complete(self, event: GridActorEvent, *, owner_id: str) -> bool:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_grid_actor_events AS event
                    SET status = 'completed', completed_at = NOW(),
                        lease_expires_at = NULL, updated_at = NOW()
                    WHERE event.id = %s
                      AND event.status = 'processing'
                      AND event.claimed_by = %s
                      AND event.fencing_token = %s
                      AND EXISTS (
                        SELECT 1
                        FROM qd_strategy_runtime_leases AS lease
                        WHERE lease.strategy_id = event.strategy_id
                          AND lease.owner_id = %s
                          AND lease.fencing_token = %s
                          AND lease.lease_expires_at >= NOW()
                      )
                    """,
                    (
                        event.id,
                        str(owner_id),
                        event.fencing_token,
                        str(owner_id),
                        event.fencing_token,
                    ),
                )
                completed = cur.rowcount == 1
                if completed:
                    cur.execute(
                        """
                        UPDATE qd_execution_events
                        SET processed_at = NOW(), process_error = ''
                        WHERE id = %s
                        """,
                        (event.execution_event_id,),
                    )
                db.commit()
                return completed
            finally:
                cur.close()

    def lock_ownership(self, event: GridActorEvent, *, owner_id: str) -> None:
        """Fence one projection against a concurrent runtime-owner handoff."""
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT lease.strategy_id
                    FROM qd_strategy_runtime_leases AS lease
                    JOIN qd_grid_actor_events AS actor_event
                      ON actor_event.strategy_id = lease.strategy_id
                    WHERE actor_event.id = %s
                      AND actor_event.status = 'processing'
                      AND actor_event.claimed_by = %s
                      AND actor_event.fencing_token = %s
                      AND lease.owner_id = %s
                      AND lease.fencing_token = %s
                      AND lease.lease_expires_at >= NOW()
                    FOR SHARE OF lease
                    """,
                    (
                        event.id,
                        str(owner_id),
                        event.fencing_token,
                        str(owner_id),
                        event.fencing_token,
                    ),
                )
                if cur.fetchone() is None:
                    raise RuntimeError("gridActor.runtimeLeaseLost")
            finally:
                cur.close()

    def fail(
        self,
        event: GridActorEvent,
        *,
        owner_id: str,
        error: str,
        max_attempts: int = 8,
    ) -> None:
        terminal = int(event.attempts) >= max(1, int(max_attempts))
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_grid_actor_events
                    SET status = %s,
                        available_at = CASE
                          WHEN %s THEN available_at
                          ELSE NOW() + LEAST(60, POWER(2, LEAST(attempts, 6))) * INTERVAL '1 second'
                        END,
                        lease_expires_at = NULL,
                        error_message = %s,
                        completed_at = CASE WHEN %s THEN NOW() ELSE NULL END,
                        updated_at = NOW()
                    WHERE id = %s AND claimed_by = %s AND fencing_token = %s
                    """,
                    (
                        "dead" if terminal else "failed",
                        terminal,
                        str(error or "")[:2000],
                        terminal,
                        event.id,
                        str(owner_id),
                        event.fencing_token,
                    ),
                )
                cur.execute(
                    """
                    UPDATE qd_execution_events
                    SET process_attempts = GREATEST(process_attempts, %s),
                        process_error = %s,
                        next_attempt_at = CASE
                          WHEN %s THEN next_attempt_at
                          ELSE NOW() + LEAST(60, POWER(2, LEAST(%s, 6))) * INTERVAL '1 second'
                        END
                    WHERE id = %s
                    """,
                    (
                        event.attempts,
                        str(error or "")[:2000],
                        terminal,
                        event.attempts,
                        event.execution_event_id,
                    ),
                )
                db.commit()
            finally:
                cur.close()

    def load_state(self, *, strategy_id: int, strategy_run_id: int) -> dict[str, Any]:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT state_json
                    FROM qd_grid_actor_state
                    WHERE strategy_id = %s AND strategy_run_id = %s
                    """,
                    (int(strategy_id), int(strategy_run_id)),
                )
                row = cur.fetchone() or {}
            finally:
                cur.close()
        raw = row.get("state_json") if isinstance(row, dict) else {}
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    def checkpoint(
        self,
        *,
        strategy_id: int,
        strategy_run_id: int,
        state: dict[str, Any],
        status: str = "running",
        last_execution_event_id: int = 0,
    ) -> bool:
        safe = json.loads(json.dumps(state or {}, default=str))
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_grid_actor_state
                        (strategy_id, strategy_run_id, owner_id, fencing_token,
                         status, state_json, last_execution_event_id,
                         heartbeat_at, version, updated_at)
                    SELECT %s, %s, lease.owner_id, lease.fencing_token,
                           %s, %s::jsonb, %s, NOW(), 1, NOW()
                    FROM qd_strategy_runtime_leases AS lease
                    WHERE lease.strategy_id = %s
                      AND lease.owner_id <> ''
                      AND lease.lease_expires_at >= NOW()
                    ON CONFLICT (strategy_id) DO UPDATE
                    SET strategy_run_id = EXCLUDED.strategy_run_id,
                        owner_id = EXCLUDED.owner_id,
                        fencing_token = EXCLUDED.fencing_token,
                        status = EXCLUDED.status,
                        state_json = EXCLUDED.state_json,
                        last_execution_event_id = CASE
                          WHEN qd_grid_actor_state.strategy_run_id = EXCLUDED.strategy_run_id
                            THEN GREATEST(
                              qd_grid_actor_state.last_execution_event_id,
                              EXCLUDED.last_execution_event_id
                            )
                          ELSE EXCLUDED.last_execution_event_id
                        END,
                        heartbeat_at = NOW(),
                        version = qd_grid_actor_state.version + 1,
                        updated_at = NOW()
                    WHERE qd_grid_actor_state.fencing_token <= EXCLUDED.fencing_token
                    RETURNING strategy_id
                    """,
                    (
                        int(strategy_id),
                        int(strategy_run_id),
                        str(status or "running"),
                        json.dumps(safe, ensure_ascii=False),
                        int(last_execution_event_id or 0),
                        int(strategy_id),
                    ),
                )
                saved = cur.fetchone() is not None
                db.commit()
                return saved
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def snapshot(self) -> dict[str, int]:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE status IN ('pending', 'failed')) AS queued,
                      COUNT(*) FILTER (WHERE status = 'processing') AS processing,
                      COUNT(*) FILTER (WHERE status = 'dead') AS dead
                    FROM qd_grid_actor_events
                    """
                )
                row = cur.fetchone() or {}
            finally:
                cur.close()
        return {
            "grid_actor_events_queued": int(row.get("queued") or 0),
            "grid_actor_events_processing": int(row.get("processing") or 0),
            "grid_actor_events_dead": int(row.get("dead") or 0),
        }


class GridActorMailbox:
    """Execute routed fills only inside the worker that owns the strategy lease."""

    def __init__(self, repository: GridActorRepository | None = None, processor: Any = None) -> None:
        self.repository = repository or GridActorRepository()
        self.processor = processor
        self.max_attempts = max(1, int(os.getenv("GRID_ACTOR_MAX_ATTEMPTS", "8")))
        self.lease_seconds = max(10, int(os.getenv("GRID_ACTOR_EVENT_LEASE_SEC", "30")))
        self.claimed_events = 0
        self.completed_events = 0
        self.failed_events = 0

    def drain_owned(
        self,
        *,
        owner_id: str,
        strategy_ids: Iterable[int],
        limit: int = 100,
    ) -> int:
        from app.services.execution_streams.processor import ExecutionEventProcessor
        from app.services.grid.runner import get_runner

        events = self.repository.claim_for_owner(
            owner_id=owner_id,
            strategy_ids=strategy_ids,
            limit=limit,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        if not events:
            return 0
        self.claimed_events += len(events)
        processor = self.processor or ExecutionEventProcessor(grid_actors=self.repository)
        completed = 0
        for event in events:
            try:
                runner = get_runner(event.strategy_id)
                if runner is None:
                    raise RuntimeError("gridActor.ownerRuntimeNotReady")
                processor.process_grid_actor_event(
                    event,
                    runner=runner,
                    owner_id=owner_id,
                )
                if not self.repository.complete(event, owner_id=owner_id):
                    raise RuntimeError("gridActor.leaseChangedBeforeCommit")
                completed += 1
                self.completed_events += 1
            except Exception as exc:
                self.failed_events += 1
                self.repository.fail(
                    event,
                    owner_id=owner_id,
                    error=str(exc),
                    max_attempts=self.max_attempts,
                )
                logger.warning(
                    "Grid actor event failed: strategy=%s event=%s owner=%s",
                    event.strategy_id,
                    event.execution_event_id,
                    owner_id,
                    exc_info=True,
                )
        return completed

    def snapshot(self) -> dict[str, int]:
        return {
            "grid_actor_events_claimed": self.claimed_events,
            "grid_actor_events_completed": self.completed_events,
            "grid_actor_events_failed": self.failed_events,
        }


__all__ = ["GridActorEvent", "GridActorMailbox", "GridActorRepository"]
