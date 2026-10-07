from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, List, Tuple

from app.services.usdt_payment_service import get_usdt_payment_service
from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)
_MONEY = Decimal("0.01")


def _enabled() -> bool:
    return str(os.getenv("REFERRAL_PROGRAM_ENABLED", "False")).strip().lower() in {"1", "true", "yes", "on"}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _money(value: Any) -> Decimal:
    return _decimal(value).quantize(_MONEY, rounding=ROUND_DOWN)


class ReferralRewardService:
    def enabled(self) -> bool:
        return _enabled()

    def rates(self) -> List[Decimal]:
        raw = str(os.getenv("REFERRAL_REWARD_RATES", "10,5,3") or "")
        tokens = [token.strip() for token in re.split(r"[,，;；\s]+", raw) if token.strip()]
        rates: List[Decimal] = []
        try:
            if len(tokens) > 3:
                raise ValueError("too_many_referral_levels")
            for token in tokens:
                token = token.strip().rstrip("%")
                rate = Decimal(token)
                if rate < 0 or rate > 100:
                    raise ValueError("rate_out_of_range")
                rates.append(rate)
            if sum(rates, Decimal("0")) > 100:
                raise ValueError("total_rate_exceeds_100")
        except (InvalidOperation, ValueError):
            logger.error("Invalid REFERRAL_REWARD_RATES=%r; no rewards will be issued", raw)
            return []
        return rates

    @staticmethod
    def _hold_days() -> int:
        try:
            return max(0, min(365, int(float(os.getenv("REFERRAL_REWARD_HOLD_DAYS", "7") or 7))))
        except (TypeError, ValueError):
            return 7

    @staticmethod
    def _minimum_withdrawal() -> Decimal:
        return max(_MONEY, _money(os.getenv("REFERRAL_MIN_WITHDRAWAL_USD", "10")))

    @staticmethod
    def _ensure_schema(cur) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS qd_referral_reward_accounts (
              user_id INTEGER PRIMARY KEY REFERENCES qd_users(id) ON DELETE CASCADE,
              available_balance DECIMAL(20,2) NOT NULL DEFAULT 0,
              pending_reward_balance DECIMAL(20,2) NOT NULL DEFAULT 0,
              pending_withdrawal_balance DECIMAL(20,2) NOT NULL DEFAULT 0,
              lifetime_earned DECIMAL(20,2) NOT NULL DEFAULT 0,
              lifetime_paid DECIMAL(20,2) NOT NULL DEFAULT 0,
              created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS qd_referral_withdrawals (
              id SERIAL PRIMARY KEY,
              user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
              currency VARCHAR(10) NOT NULL, chain VARCHAR(20) NOT NULL,
              address VARCHAR(160) NOT NULL, amount DECIMAL(20,2) NOT NULL,
              status VARCHAR(20) NOT NULL DEFAULT 'pending', tx_hash VARCHAR(160) NOT NULL DEFAULT '',
              review_note TEXT NOT NULL DEFAULT '', operator_id INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
              reviewed_at TIMESTAMP, paid_at TIMESTAMP, created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS qd_referral_reward_ledger (
              id SERIAL PRIMARY KEY,
              user_id INTEGER NOT NULL REFERENCES qd_users(id) ON DELETE CASCADE,
              action VARCHAR(40) NOT NULL, amount DECIMAL(20,2) NOT NULL,
              available_delta DECIMAL(20,2) NOT NULL DEFAULT 0,
              pending_reward_delta DECIMAL(20,2) NOT NULL DEFAULT 0,
              pending_withdrawal_delta DECIMAL(20,2) NOT NULL DEFAULT 0,
              available_balance_after DECIMAL(20,2) NOT NULL DEFAULT 0,
              pending_reward_balance_after DECIMAL(20,2) NOT NULL DEFAULT 0,
              pending_withdrawal_balance_after DECIMAL(20,2) NOT NULL DEFAULT 0,
              source_key VARCHAR(255) UNIQUE, source_user_id INTEGER REFERENCES qd_users(id) ON DELETE SET NULL,
              source_order_ref VARCHAR(255) NOT NULL DEFAULT '', plan_code VARCHAR(64) NOT NULL DEFAULT '',
              reward_level INTEGER, reward_rate DECIMAL(8,4), status VARCHAR(20) NOT NULL DEFAULT 'posted',
              available_at TIMESTAMP, withdrawal_id INTEGER REFERENCES qd_referral_withdrawals(id) ON DELETE SET NULL,
              remark TEXT NOT NULL DEFAULT '', created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )

    @staticmethod
    def _lock_account(cur, user_id: int) -> Dict[str, Any]:
        cur.execute(
            "INSERT INTO qd_referral_reward_accounts (user_id) VALUES (?) ON CONFLICT (user_id) DO NOTHING",
            (int(user_id),),
        )
        cur.execute("SELECT * FROM qd_referral_reward_accounts WHERE user_id=? FOR UPDATE", (int(user_id),))
        return cur.fetchone() or {}

    @staticmethod
    def _update_account(cur, user_id: int, available: Decimal, pending: Decimal, withdrawal: Decimal,
                        earned: Decimal = Decimal("0"), paid: Decimal = Decimal("0")) -> None:
        cur.execute(
            """
            UPDATE qd_referral_reward_accounts
            SET available_balance=available_balance+?, pending_reward_balance=pending_reward_balance+?,
                pending_withdrawal_balance=pending_withdrawal_balance+?, lifetime_earned=lifetime_earned+?,
                lifetime_paid=lifetime_paid+?, updated_at=NOW() WHERE user_id=?
            """,
            (available, pending, withdrawal, earned, paid, int(user_id)),
        )

    @staticmethod
    def _snapshot(cur, user_id: int) -> Dict[str, Any]:
        cur.execute("SELECT * FROM qd_referral_reward_accounts WHERE user_id=?", (int(user_id),))
        return cur.fetchone() or {}

    @staticmethod
    def _finish_ledger(cur, ledger_id: int, account: Dict[str, Any]) -> None:
        cur.execute(
            """
            UPDATE qd_referral_reward_ledger SET available_balance_after=?, pending_reward_balance_after=?,
              pending_withdrawal_balance_after=? WHERE id=?
            """,
            (account.get("available_balance") or 0, account.get("pending_reward_balance") or 0,
             account.get("pending_withdrawal_balance") or 0, int(ledger_id)),
        )

    def award_membership_rewards(self, cur, *, buyer_id: int, plan: Dict[str, Any], source_ref: str) -> None:
        if not self.enabled() or not bool(plan.get("referral_eligible")):
            return
        rates = self.rates()
        price = _money(plan.get("price_usd"))
        source_ref = str(source_ref or "").strip()
        if not rates or price <= 0 or not source_ref:
            return
        self._ensure_schema(cur)
        cur.execute("SELECT referred_by FROM qd_users WHERE id=?", (int(buyer_id),))
        next_user = (cur.fetchone() or {}).get("referred_by")
        seen = {int(buyer_id)}
        hold_days = self._hold_days()
        available_at = datetime.now(timezone.utc) + timedelta(days=hold_days)
        for level, rate in enumerate(rates, start=1):
            if not next_user or int(next_user) in seen:
                break
            recipient_id = int(next_user)
            seen.add(recipient_id)
            amount = _money(price * rate / Decimal("100"))
            if amount > 0:
                self._lock_account(cur, recipient_id)
                source_key = f"membership:{source_ref}:{recipient_id}:{level}"
                cur.execute(
                    """
                    INSERT INTO qd_referral_reward_ledger
                      (user_id, action, amount, available_delta, pending_reward_delta, source_key,
                       source_user_id, source_order_ref, plan_code, reward_level, reward_rate,
                       status, available_at, remark)
                    VALUES (?, 'membership_reward', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'membership_referral_reward')
                    ON CONFLICT (source_key) DO NOTHING RETURNING id
                    """,
                    (recipient_id, amount, amount if hold_days == 0 else 0, amount if hold_days else 0,
                     source_key, int(buyer_id), source_ref, str(plan.get("code") or plan.get("plan") or ""),
                     level, rate, "posted" if hold_days == 0 else "pending", available_at if hold_days else None),
                )
                inserted = cur.fetchone() or {}
                if inserted:
                    self._update_account(cur, recipient_id, amount if hold_days == 0 else Decimal("0"),
                                         amount if hold_days else Decimal("0"), Decimal("0"), earned=amount)
                    self._finish_ledger(cur, int(inserted["id"]), self._snapshot(cur, recipient_id))
            cur.execute("SELECT referred_by FROM qd_users WHERE id=?", (recipient_id,))
            next_user = (cur.fetchone() or {}).get("referred_by")

    def _release_matured(self, cur, user_id: int) -> None:
        self._lock_account(cur, user_id)
        cur.execute(
            """
            SELECT id, amount FROM qd_referral_reward_ledger
            WHERE user_id=? AND action='membership_reward' AND status='pending' AND available_at<=NOW()
            FOR UPDATE
            """,
            (int(user_id),),
        )
        rows = cur.fetchall() or []
        total = sum((_money(row.get("amount")) for row in rows), Decimal("0"))
        if total <= 0:
            return
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        cur.execute(f"UPDATE qd_referral_reward_ledger SET status='released' WHERE id IN ({placeholders})", tuple(ids))
        self._update_account(cur, user_id, total, -total, Decimal("0"))
        cur.execute(
            """
            INSERT INTO qd_referral_reward_ledger
              (user_id, action, amount, available_delta, pending_reward_delta, status, remark)
            VALUES (?, 'reward_release', ?, ?, ?, 'posted', 'reward_hold_released') RETURNING id
            """,
            (int(user_id), total, total, -total),
        )
        ledger_id = int((cur.fetchone() or {})["id"])
        self._finish_ledger(cur, ledger_id, self._snapshot(cur, user_id))

    @staticmethod
    def _public_chains() -> List[Dict[str, Any]]:
        service = get_usdt_payment_service()
        out: List[Dict[str, Any]] = []
        for currency in ("USDT", "USDC"):
            for row in service.list_chains(currency):
                out.append({"currency": currency, "code": row.get("code"), "label": row.get("label")})
        return out

    def summary(self, user_id: int, *, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        if not self.enabled():
            return {"enabled": False}
        page = max(1, int(page)); page_size = max(1, min(100, int(page_size)))
        with get_db_connection() as db:
            cur = db.cursor(); self._ensure_schema(cur); self._release_matured(cur, user_id)
            account = self._snapshot(cur, user_id)
            cur.execute("SELECT COUNT(*) AS count FROM qd_referral_reward_ledger WHERE user_id=?", (int(user_id),))
            total = int((cur.fetchone() or {}).get("count") or 0)
            cur.execute(
                "SELECT * FROM qd_referral_reward_ledger WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (int(user_id), page_size, (page - 1) * page_size),
            )
            ledger = cur.fetchall() or []
            cur.execute("SELECT * FROM qd_referral_withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 50", (int(user_id),))
            withdrawals = cur.fetchall() or []
            db.commit(); cur.close()
        return {
            "enabled": True,
            "account": self._serialize_account(account),
            "ledger": [self._serialize_row(row) for row in ledger],
            "ledger_total": total,
            "withdrawals": [self._serialize_row(row) for row in withdrawals],
            "channels": self._public_chains(),
            "minimum_withdrawal": float(self._minimum_withdrawal()),
            "rates": [float(rate) for rate in self.rates()],
        }

    @staticmethod
    def _valid_address(chain: str, address: str) -> bool:
        if chain in {"BEP20", "ERC20"}:
            return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", address))
        if chain == "TRC20":
            return bool(re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", address))
        if chain == "SOL":
            return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address))
        return False

    def create_withdrawal(self, user_id: int, payload: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        if not self.enabled():
            return False, "referral_program_disabled", {}
        currency = str(payload.get("currency") or "USDT").strip().upper()
        chain = str(payload.get("chain") or "").strip().upper()
        address = str(payload.get("address") or "").strip()
        amount = _money(payload.get("amount"))
        allowed = {(row["currency"], row["code"]) for row in self._public_chains()}
        if (currency, chain) not in allowed:
            return False, "unsupported_withdrawal_channel", {}
        if not self._valid_address(chain, address):
            return False, "invalid_withdrawal_address", {}
        if amount < self._minimum_withdrawal():
            return False, "withdrawal_below_minimum", {"minimum_withdrawal": float(self._minimum_withdrawal())}
        with get_db_connection() as db:
            cur = db.cursor(); self._ensure_schema(cur); self._release_matured(cur, user_id)
            account = self._lock_account(cur, user_id)
            if _money(account.get("available_balance")) < amount:
                db.rollback(); cur.close()
                return False, "insufficient_reward_balance", {}
            cur.execute(
                """
                INSERT INTO qd_referral_withdrawals (user_id, currency, chain, address, amount)
                VALUES (?, ?, ?, ?, ?) RETURNING id
                """,
                (int(user_id), currency, chain, address, amount),
            )
            withdrawal_id = int((cur.fetchone() or {})["id"])
            self._update_account(cur, user_id, -amount, Decimal("0"), amount)
            cur.execute(
                """
                INSERT INTO qd_referral_reward_ledger
                  (user_id, action, amount, available_delta, pending_withdrawal_delta, withdrawal_id, status, remark)
                VALUES (?, 'withdrawal_requested', ?, ?, ?, ?, 'posted', 'withdrawal_balance_frozen') RETURNING id
                """,
                (int(user_id), amount, -amount, amount, withdrawal_id),
            )
            self._finish_ledger(cur, int((cur.fetchone() or {})["id"]), self._snapshot(cur, user_id))
            db.commit(); cur.close()
        return True, "success", {"withdrawal_id": withdrawal_id}

    def list_withdrawals(self, *, status: str = "", page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        page = max(1, int(page)); page_size = max(1, min(100, int(page_size)))
        where = ""; params: List[Any] = []
        if status:
            where = " WHERE w.status=?"; params.append(status)
        with get_db_connection() as db:
            cur = db.cursor(); self._ensure_schema(cur)
            cur.execute(
                """
                SELECT COUNT(*) AS total_requests,
                       COUNT(*) FILTER (WHERE status='pending') AS pending_requests,
                       COUNT(*) FILTER (WHERE status='processing') AS processing_requests,
                       COUNT(*) FILTER (WHERE status='paid') AS paid_requests,
                       COUNT(*) FILTER (WHERE status='rejected') AS rejected_requests,
                       COALESCE(SUM(amount) FILTER (WHERE status IN ('pending', 'processing')), 0) AS pending_amount,
                       COALESCE(SUM(amount) FILTER (WHERE status='paid'), 0) AS paid_amount
                FROM qd_referral_withdrawals
                """
            )
            summary = cur.fetchone() or {}
            cur.execute(f"SELECT COUNT(*) AS count FROM qd_referral_withdrawals w{where}", tuple(params))
            total = int((cur.fetchone() or {}).get("count") or 0)
            cur.execute(
                f"""SELECT w.*, u.username, u.nickname FROM qd_referral_withdrawals w
                    JOIN qd_users u ON u.id=w.user_id{where} ORDER BY w.id DESC LIMIT ? OFFSET ?""",
                tuple(params + [page_size, (page - 1) * page_size]),
            )
            rows = cur.fetchall() or []; db.commit(); cur.close()
        return {
            "items": [self._serialize_row(row) for row in rows],
            "total": total,
            "summary": self._serialize_row(summary),
        }

    def review_withdrawal(self, withdrawal_id: int, operator_id: int, payload: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        action = str(payload.get("action") or "").strip().lower()
        target = {"processing": "processing", "paid": "paid", "reject": "rejected", "rejected": "rejected"}.get(action)
        if not target:
            return False, "invalid_withdrawal_action", {}
        tx_hash = str(payload.get("tx_hash") or "").strip()[:160]
        note = str(payload.get("review_note") or "").strip()[:2000]
        if target == "paid" and not tx_hash:
            return False, "withdrawal_tx_hash_required", {}
        with get_db_connection() as db:
            cur = db.cursor(); self._ensure_schema(cur)
            cur.execute("SELECT * FROM qd_referral_withdrawals WHERE id=? FOR UPDATE", (int(withdrawal_id),))
            row = cur.fetchone() or {}
            if not row:
                db.rollback(); cur.close()
                return False, "withdrawal_not_found", {}
            current = str(row.get("status") or "")
            allowed = current == "pending" or (current == "processing" and target in {"paid", "rejected"})
            if not allowed:
                db.rollback(); cur.close()
                return False, "invalid_withdrawal_transition", {"status": current}
            user_id = int(row["user_id"]); amount = _money(row.get("amount")); self._lock_account(cur, user_id)
            if target == "paid":
                self._update_account(cur, user_id, Decimal("0"), Decimal("0"), -amount, paid=amount)
                deltas = (Decimal("0"), -amount); ledger_action = "withdrawal_paid"
            elif target == "rejected":
                self._update_account(cur, user_id, amount, Decimal("0"), -amount)
                deltas = (amount, -amount); ledger_action = "withdrawal_rejected"
            else:
                deltas = None; ledger_action = ""
            cur.execute(
                """UPDATE qd_referral_withdrawals SET status=?, tx_hash=?, review_note=?, operator_id=?,
                   reviewed_at=NOW(), paid_at=CASE WHEN ?='paid' THEN NOW() ELSE paid_at END, updated_at=NOW() WHERE id=?""",
                (target, tx_hash, note, int(operator_id), target, int(withdrawal_id)),
            )
            if deltas:
                cur.execute(
                    """INSERT INTO qd_referral_reward_ledger
                       (user_id, action, amount, available_delta, pending_withdrawal_delta, withdrawal_id, status, remark)
                       VALUES (?, ?, ?, ?, ?, ?, 'posted', ?) RETURNING id""",
                    (user_id, ledger_action, amount, deltas[0], deltas[1], int(withdrawal_id), note),
                )
                self._finish_ledger(cur, int((cur.fetchone() or {})["id"]), self._snapshot(cur, user_id))
            db.commit(); cur.close()
        return True, "success", {"id": int(withdrawal_id), "status": target}

    @staticmethod
    def _serialize_account(row: Dict[str, Any]) -> Dict[str, float]:
        return {key: float(row.get(key) or 0) for key in (
            "available_balance", "pending_reward_balance", "pending_withdrawal_balance", "lifetime_earned", "lifetime_paid"
        )}

    @staticmethod
    def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        for key, value in list(out.items()):
            if isinstance(value, Decimal):
                out[key] = float(value)
            elif isinstance(value, datetime):
                out[key] = value.isoformat()
        return out


_service = ReferralRewardService()


def get_referral_reward_service() -> ReferralRewardService:
    return _service
