"""Atomic pending-order claim operations."""

from __future__ import annotations

from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)


def claim_pending_order(order_id: int) -> bool:
    """Claim a pending order only while its optional runtime fence is current."""
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                UPDATE pending_orders AS pending
                SET status = 'processing',
                    attempts = COALESCE(attempts, 0) + 1,
                    processed_at = NOW(),
                    updated_at = NOW()
                WHERE pending.id = %s AND pending.status = 'pending'
                  AND (
                      COALESCE(pending.runtime_fencing_token, 0) = 0
                      OR EXISTS (
                          SELECT 1
                          FROM qd_strategy_runtime_leases AS lease
                          WHERE lease.strategy_id = pending.strategy_id
                            AND lease.fencing_token = pending.runtime_fencing_token
                            AND lease.lease_expires_at >= NOW()
                      )
                  )
                """,
                (int(order_id),),
            )
            claimed = getattr(cur, "rowcount", None)
            if claimed is not None and int(claimed) == 0:
                cur.execute(
                    """
                    UPDATE pending_orders AS pending
                    SET status = 'failed',
                        last_error = 'strategyRuntime.staleFencingToken',
                        dispatch_note = 'runtime_fence_rejected',
                        processed_at = NOW(), updated_at = NOW()
                    WHERE pending.id = %s AND pending.status = 'pending'
                      AND COALESCE(pending.runtime_fencing_token, 0) > 0
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qd_strategy_runtime_leases AS lease
                          WHERE lease.strategy_id = pending.strategy_id
                            AND lease.fencing_token = pending.runtime_fencing_token
                            AND lease.lease_expires_at >= NOW()
                      )
                    """,
                    (int(order_id),),
                )
            db.commit()
            cur.close()
        return claimed is None or int(claimed) > 0
    except Exception as exc:
        logger.warning("mark_processing failed: id=%s, err=%s", order_id, exc)
        return False


__all__ = ["claim_pending_order"]
