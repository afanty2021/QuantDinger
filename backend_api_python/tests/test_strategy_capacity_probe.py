from __future__ import annotations

from app.commands.strategy_capacity_probe import run_probe


def test_capacity_probe_is_safe_and_counts_all_strategies():
    result = run_probe(
        strategy_count=256,
        shard_count=32,
        batch_size=25,
        worker_count=4,
        timeout=15,
    )

    assert result["safe_mode"] is True
    assert result["external_io"] is False
    assert result["orders_submitted"] == 0
    assert result["strategies"] == 256
    assert result["largest_shard"] == 8
    assert result["evaluation_batches"] == 32
    assert [stage["name"] for stage in result["stages"]] == [
        "scheduler_start",
        "scheduler_wake",
        "event_dispatch",
        "evaluator_fanout",
    ]
    assert all(stage["operations"] == 256 for stage in result["stages"])
