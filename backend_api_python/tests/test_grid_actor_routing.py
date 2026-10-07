from __future__ import annotations

from types import SimpleNamespace

from app.services.execution_streams.processor import ExecutionEventProcessor
from app.services.grid.actor import GridActorEvent, GridActorMailbox


class ActorRepository:
    def __init__(self, events=()):
        self.events = list(events)
        self.enqueued = []
        self.completed = []
        self.failed = []

    def enqueue(self, **values):
        self.enqueued.append(values)
        return True

    def claim_for_owner(self, **_values):
        return list(self.events)

    def complete(self, event, *, owner_id):
        self.completed.append((event.id, owner_id, event.fencing_token))
        return True

    def lock_ownership(self, event, *, owner_id):
        del event, owner_id

    def fail(self, event, *, owner_id, error, max_attempts):
        self.failed.append((event.id, owner_id, error, max_attempts))


def test_execution_processor_routes_grid_fill_to_durable_actor(monkeypatch):
    monkeypatch.setenv("GRID_ACTOR_ROUTING_ENABLED", "true")
    actors = ActorRepository()
    processor = ExecutionEventProcessor(repository=object(), grid_actors=actors)

    routed = processor._route_grid_event(
        {"id": 91},
        {"strategy_id": 17, "owner_id": 23},
    )

    assert routed is True
    assert actors.enqueued == [
        {
            "execution_event_id": 91,
            "strategy_id": 17,
            "grid_order_id": 23,
        }
    ]


def test_grid_actor_mailbox_projects_only_with_local_owner_runner(monkeypatch):
    event = GridActorEvent(1, 91, 17, 23, 1, 7)
    actors = ActorRepository([event])
    projected = []
    processor = SimpleNamespace(
        process_grid_actor_event=lambda item, *, runner, owner_id: projected.append(
            (item.execution_event_id, runner.strategy_id)
        )
    )
    mailbox = GridActorMailbox(repository=actors, processor=processor)
    runner = SimpleNamespace(strategy_id=17)
    monkeypatch.setattr("app.services.grid.runner.get_runner", lambda strategy_id: runner)

    assert mailbox.drain_owned(owner_id="worker-a", strategy_ids=[17]) == 1
    assert projected == [(91, 17)]
    assert actors.completed == [(1, "worker-a", 7)]
    assert actors.failed == []


def test_grid_actor_mailbox_retries_when_owner_runtime_is_not_ready(monkeypatch):
    event = GridActorEvent(1, 91, 17, 23, 1, 7)
    actors = ActorRepository([event])
    mailbox = GridActorMailbox(repository=actors, processor=object())
    monkeypatch.setattr("app.services.grid.runner.get_runner", lambda strategy_id: None)

    assert mailbox.drain_owned(owner_id="worker-a", strategy_ids=[17]) == 0
    assert actors.completed == []
    assert actors.failed[0][:2] == (1, "worker-a")
    assert "ownerRuntimeNotReady" in actors.failed[0][2]
