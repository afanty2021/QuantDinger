"""Chronological walk-forward split construction from market observations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class WalkForwardFold:
    index: int
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    train_bars: int = 0
    validation_bars: int = 0
    embargo_bars: int = 0

    def metadata(self) -> dict[str, str | int]:
        return {
            "index": self.index,
            "trainStart": self.train_start.isoformat(),
            "trainEnd": self.train_end.isoformat(),
            "validationStart": self.validation_start.isoformat(),
            "validationEnd": self.validation_end.isoformat(),
            "trainBars": self.train_bars,
            "validationBars": self.validation_bars,
            "embargoBars": self.embargo_bars,
        }


@dataclass(frozen=True)
class WalkForwardPlan:
    folds: tuple[WalkForwardFold, ...]
    blind_start: datetime | None
    blind_end: datetime | None
    sample_count: int = 0
    development_bars: int = 0
    blind_bars: int = 0
    requested_folds: int = 0
    effective_folds: int = 0
    minimum_train_bars: int = 0
    minimum_validation_bars: int = 0
    embargo_bars: int = 0
    blind_omitted_reason: str | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "folds": [fold.metadata() for fold in self.folds],
            "blindStart": self.blind_start.isoformat() if self.blind_start else None,
            "blindEnd": self.blind_end.isoformat() if self.blind_end else None,
            "sampleCount": self.sample_count,
            "developmentBars": self.development_bars,
            "blindBars": self.blind_bars,
            "requestedFolds": self.requested_folds,
            "effectiveFolds": self.effective_folds,
            "minimumTrainBars": self.minimum_train_bars,
            "minimumValidationBars": self.minimum_validation_bars,
            "embargoBars": self.embargo_bars,
            "blindOmittedReason": self.blind_omitted_reason,
        }


def build_walk_forward_plan(
    start: datetime,
    end: datetime,
    *,
    folds: int,
    train_ratio: float,
    blind_ratio: float,
    observations: Sequence[datetime] | None = None,
    warmup_bars: int = 0,
    embargo_bars: int | None = None,
    embargo_days: int | None = None,
) -> WalkForwardPlan:
    start = _as_datetime(start)
    end = _as_datetime(end)
    if end <= start:
        raise ValueError("strategyEvolution.dateRangeInvalid")
    timestamps = _observation_times(start, end, observations)
    sample_count = len(timestamps)
    lookback = max(1, int(warmup_bars or 0))
    minimum_train_bars = max(40, lookback * 5)
    minimum_validation_bars = max(10, lookback // 2)
    gap_bars = max(0, int(embargo_bars if embargo_bars is not None else (embargo_days or 0)))
    requested_folds = max(2, int(folds or 2))
    minimum_development_bars = minimum_train_bars + 2 * (minimum_validation_bars + gap_bars)
    if sample_count < minimum_development_bars:
        raise ValueError("strategyEvolution.observationRangeTooShort")

    blind_count = max(0, int(sample_count * max(0.0, float(blind_ratio or 0.0))))
    blind_start = None
    blind_end = None
    blind_omitted_reason = None
    development_limit = sample_count
    if blind_count:
        maximum_blind = sample_count - minimum_development_bars - gap_bars
        if maximum_blind >= minimum_validation_bars:
            blind_count = min(max(blind_count, minimum_validation_bars), maximum_blind)
            blind_index = sample_count - blind_count
            blind_start = timestamps[blind_index]
            blind_end = timestamps[-1]
            development_limit = blind_index - gap_bars
        else:
            blind_count = 0
            blind_omitted_reason = "insufficientObservations"

    development_bars = development_limit
    maximum_initial_train = development_bars - 2 * (minimum_validation_bars + gap_bars)
    initial_train_bars = min(
        maximum_initial_train,
        max(minimum_train_bars, int(development_bars * float(train_ratio))),
    )
    remaining_bars = development_bars - initial_train_bars
    effective_folds = min(
        requested_folds,
        remaining_bars // (minimum_validation_bars + gap_bars),
    )
    if effective_folds < 2:
        raise ValueError("strategyEvolution.walkForwardUnavailable")

    fold_span = remaining_bars // effective_folds
    output: list[WalkForwardFold] = []
    for index in range(effective_folds):
        block_start = initial_train_bars + index * fold_span
        block_end = (
            development_bars - 1
            if index == effective_folds - 1
            else initial_train_bars + (index + 1) * fold_span - 1
        )
        validation_start_index = block_start + gap_bars
        train_end_index = block_start - 1
        validation_bars = block_end - validation_start_index + 1
        if train_end_index < 0 or validation_bars < minimum_validation_bars:
            continue
        output.append(
            WalkForwardFold(
                index=index + 1,
                train_start=timestamps[0],
                train_end=timestamps[train_end_index],
                validation_start=timestamps[validation_start_index],
                validation_end=timestamps[block_end],
                train_bars=train_end_index + 1,
                validation_bars=validation_bars,
                embargo_bars=gap_bars,
            )
        )
    if len(output) < 2:
        raise ValueError("strategyEvolution.walkForwardUnavailable")
    return WalkForwardPlan(
        folds=tuple(output),
        blind_start=blind_start,
        blind_end=blind_end,
        sample_count=sample_count,
        development_bars=development_bars,
        blind_bars=blind_count,
        requested_folds=requested_folds,
        effective_folds=len(output),
        minimum_train_bars=minimum_train_bars,
        minimum_validation_bars=minimum_validation_bars,
        embargo_bars=gap_bars,
        blind_omitted_reason=blind_omitted_reason,
    )


def _observation_times(
    start: datetime,
    end: datetime,
    observations: Sequence[datetime] | None,
) -> tuple[datetime, ...]:
    if observations is None:
        total_days = (end.date() - start.date()).days + 1
        return tuple(start + timedelta(days=index) for index in range(total_days))
    normalized = (_as_datetime(value) for value in observations)
    return tuple(sorted({value for value in normalized if start <= value <= end}))


def _as_datetime(value: datetime) -> datetime:
    converted = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    if not isinstance(converted, datetime):
        raise TypeError("strategyEvolution.observationTimestampInvalid")
    if converted.tzinfo is not None:
        converted = converted.astimezone(timezone.utc).replace(tzinfo=None)
    return converted
