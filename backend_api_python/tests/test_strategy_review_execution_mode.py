from app.services.strategy_review import StrategyReviewService
from app.services import virtual_trading


class _StrategyService:
    def __init__(self, execution_mode):
        self.execution_mode = execution_mode

    def get_strategy(self, strategy_id, user_id):
        return {
            "id": strategy_id,
            "user_id": user_id,
            "strategy_name": "Ledger selection test",
            "execution_mode": self.execution_mode,
            "trading_config": {"initial_capital": 1000},
        }


class _ReviewService(StrategyReviewService):
    def __init__(self, execution_mode):
        self.strategy_service = _StrategyService(execution_mode)
        self.trade_virtual_flags = []
        self.position_virtual_flags = []

    def _load_trades(self, *, strategy_id, bot_type, lang, virtual=False):
        self.trade_virtual_flags.append(virtual)
        return []

    def _load_positions(self, *, strategy_id, virtual=False):
        self.position_virtual_flags.append(virtual)
        return []

    def _load_recent_logs(self, *, strategy_id, since_ts):
        return []

    def _save_report(self, **kwargs):
        return None


def test_signal_review_uses_virtual_ledger_and_positions():
    service = _ReviewService("signal")

    report = service.build_report(
        strategy_id=100,
        user_id=1,
        include_ai=False,
    )

    assert service.trade_virtual_flags == [True]
    assert service.position_virtual_flags == [True]
    assert report["strategy"]["execution_mode"] == "signal"


def test_live_review_uses_live_ledger_and_positions():
    service = _ReviewService("live")

    report = service.build_report(
        strategy_id=101,
        user_id=1,
        include_ai=False,
    )

    assert service.trade_virtual_flags == [False]
    assert service.position_virtual_flags == [False]
    assert report["strategy"]["execution_mode"] == "live"


def test_virtual_trade_loader_preserves_entry_and_exit_semantics(monkeypatch):
    monkeypatch.setattr(
        virtual_trading,
        "list_virtual_trades",
        lambda strategy_id: [
            {
                "id": 2,
                "strategy_id": strategy_id,
                "symbol": "BTC/USDT",
                "type": "close_long",
                "price": 110,
                "amount": 1,
                "value": 110,
                "commission": 1,
                "profit": 10,
                "created_at": 200,
            },
            {
                "id": 1,
                "strategy_id": strategy_id,
                "symbol": "BTC/USDT",
                "type": "open_long",
                "price": 100,
                "amount": 1,
                "value": 100,
                "commission": 1,
                "profit": 0,
                "created_at": 100,
            },
        ],
    )
    service = StrategyReviewService.__new__(StrategyReviewService)

    trades = service._load_trades(
        strategy_id=100,
        bot_type="",
        lang="zh",
        virtual=True,
    )

    assert [trade["id"] for trade in trades] == [1, 2]
    assert trades[0]["profit"] is None
    assert trades[1]["profit_gross"] == 10
    assert trades[1]["profit"] == 8
