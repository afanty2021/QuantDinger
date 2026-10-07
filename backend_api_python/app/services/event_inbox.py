"""Durable Kafka inbox claims and strategy-shard fencing leases."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from app.events.protocol import EventEnvelope
from app.utils.db import get_db_connection


InboxClaimState = Literal["acquired", "completed", "dead", "busy"]


@dataclass(frozen=True, slots=True)
class InboxClaim:
    state: InboxClaimState
    attempts: int = 0


class EventInboxRepository:
    def claim(
        self,
        *,
        consumer_group: str,
        event: EventEnvelope,
        owner_id: str,
        lease_seconds: int,
    ) -> InboxClaim:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_event_inbox
                        (consumer_group, event_id, event_type, partition_key,
                         status, attempts, claimed_by, lease_expires_at)
                    VALUES (%s, %s, %s, %s, 'processing', 1, %s,
                            NOW() + (%s * INTERVAL '1 second'))
                    ON CONFLICT (consumer_group, event_id) DO UPDATE
                    SET status = 'processing',
                        attempts = qd_event_inbox.attempts + 1,
                        claimed_by = EXCLUDED.claimed_by,
                        lease_expires_at = EXCLUDED.lease_expires_at,
                        error_message = '',
                        updated_at = NOW()
                    WHERE qd_event_inbox.status IN ('processing', 'failed')
                      AND (
                          qd_event_inbox.claimed_by = EXCLUDED.claimed_by
                          OR qd_event_inbox.lease_expires_at IS NULL
                          OR qd_event_inbox.lease_expires_at < NOW()
                      )
                    RETURNING attempts
                    """,
                    (
                        str(consumer_group),
                        event.event_id,
                        event.event_type,
                        event.partition_key,
                        str(owner_id),
                        max(1, int(lease_seconds)),
                    ),
                )
                row = cur.fetchone()
                if row:
                    db.commit()
                    return InboxClaim("acquired", int(row["attempts"]))
                cur.execute(
                    """
                    SELECT status, attempts
                    FROM qd_event_inbox
                    WHERE consumer_group = %s AND event_id = %s
                    """,
                    (str(consumer_group), event.event_id),
                )
                existing = cur.fetchone() or {}
                db.commit()
                status = str(existing.get("status") or "busy")
                state: InboxClaimState = (
                    status if status in {"completed", "dead"} else "busy"
                )
                return InboxClaim(state, int(existing.get("attempts") or 0))
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def complete(
        self,
        *,
        consumer_group: str,
        event_id: str,
        owner_id: str,
        result: dict[str, Any] | None = None,
    ) -> bool:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_event_inbox
                    SET status = 'completed', result_json = %s::jsonb,
                        lease_expires_at = NULL, completed_at = NOW(), updated_at = NOW()
                    WHERE consumer_group = %s AND event_id = %s
                      AND status = 'processing' AND claimed_by = %s
                    """,
                    (
                        json.dumps(result or {}, ensure_ascii=False),
                        str(consumer_group),
                        str(event_id),
                        str(owner_id),
                    ),
                )
                completed = cur.rowcount == 1
                db.commit()
                return completed
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def fail(
        self,
        *,
        consumer_group: str,
        event_id: str,
        owner_id: str,
        error: str,
        terminal: bool = False,
    ) -> bool:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_event_inbox
                    SET status = %s, error_message = %s, lease_expires_at = NULL,
                        completed_at = CASE WHEN %s THEN NOW() ELSE NULL END,
                        updated_at = NOW()
                    WHERE consumer_group = %s AND event_id = %s
                      AND status = 'processing' AND claimed_by = %s
                    """,
                    (
                        "dead" if terminal else "failed",
                        str(error)[:4000],
                        bool(terminal),
                        str(consumer_group),
                        str(event_id),
                        str(owner_id),
                    ),
                )
                failed = cur.rowcount == 1
                db.commit()
                return failed
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()


class StrategyShardLeaseRepository:
    def acquire(
        self,
        *,
        strategy_shard: int,
        owner_id: str,
        lease_seconds: int,
    ) -> int | None:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO qd_strategy_shard_leases
                        (strategy_shard, owner_id, fencing_token,
                         lease_expires_at, heartbeat_at)
                    VALUES (%s, %s, 1,
                            NOW() + (%s * INTERVAL '1 second'), NOW())
                    ON CONFLICT (strategy_shard) DO UPDATE
                    SET owner_id = EXCLUDED.owner_id,
                        fencing_token = CASE
                            WHEN qd_strategy_shard_leases.owner_id = EXCLUDED.owner_id
                                THEN qd_strategy_shard_leases.fencing_token
                            ELSE qd_strategy_shard_leases.fencing_token + 1
                        END,
                        lease_expires_at = EXCLUDED.lease_expires_at,
                        heartbeat_at = NOW(), updated_at = NOW()
                    WHERE qd_strategy_shard_leases.owner_id = EXCLUDED.owner_id
                       OR qd_strategy_shard_leases.lease_expires_at < NOW()
                    RETURNING fencing_token
                    """,
                    (
                        int(strategy_shard),
                        str(owner_id),
                        max(1, int(lease_seconds)),
                    ),
                )
                row = cur.fetchone()
                db.commit()
                return int(row["fencing_token"]) if row else None
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def release(
        self,
        *,
        strategy_shard: int,
        owner_id: str,
        fencing_token: int,
    ) -> bool:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_strategy_shard_leases
                    SET owner_id = '',
                        lease_expires_at = NOW() - INTERVAL '1 second',
                        heartbeat_at = NOW(),
                        updated_at = NOW()
                    WHERE strategy_shard = %s
                      AND owner_id = %s
                      AND fencing_token = %s
                    """,
                    (
                        int(strategy_shard),
                        str(owner_id),
                        int(fencing_token),
                    ),
                )
                released = cur.rowcount == 1
                db.commit()
                return released
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()

    def release_all(self, *, owner_id: str) -> int:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_strategy_shard_leases
                    SET owner_id = '',
                        lease_expires_at = NOW() - INTERVAL '1 second',
                        heartbeat_at = NOW(),
                        updated_at = NOW()
                    WHERE owner_id = %s
                    """,
                    (str(owner_id),),
                )
                released = int(cur.rowcount or 0)
                db.commit()
                return released
            except Exception:
                db.rollback()
                raise
            finally:
                cur.close()


__all__ = [
    "EventInboxRepository",
    "InboxClaim",
    "StrategyShardLeaseRepository",
]
