"""Grid, random, and lightweight TPE parameter samplers."""

from __future__ import annotations

import itertools
import math
import random
from collections import Counter
from typing import Any

from .models import SearchParameter


class ParameterSampler:
    def __init__(self, parameters: list[SearchParameter], *, seed: int = 42) -> None:
        self.parameters = parameters
        self.random = random.Random(seed)

    def grid(self, limit: int) -> list[dict[str, Any]]:
        axes = [self._grid_values(item, limit) for item in self.parameters]
        combinations = itertools.product(*axes)
        reservoir: list[tuple[int, dict[str, Any]]] = []
        for index, values in enumerate(combinations):
            candidate = {item.name: value for item, value in zip(self.parameters, values)}
            if index < limit:
                reservoir.append((index, candidate))
            else:
                replace = self.random.randint(0, index)
                if replace < limit:
                    reservoir[replace] = (index, candidate)
        return [item[1] for item in sorted(reservoir, key=lambda row: row[0])]

    def grid_size(self, limit: int) -> int:
        size = 1
        for item in self.parameters:
            size *= len(self._grid_values(item, limit))
            if size >= limit:
                return limit
        return size

    def random_candidate(self) -> dict[str, Any]:
        return {item.name: self._random_value(item) for item in self.parameters}

    def tpe_candidate(self, history: list[tuple[dict[str, Any], float]], *, samples: int = 48) -> dict[str, Any]:
        if len(history) < max(5, len(self.parameters) + 1):
            return self.random_candidate()
        ranked = sorted(history, key=lambda row: row[1], reverse=True)
        split = max(3, int(len(ranked) * 0.25))
        good = [row[0] for row in ranked[:split]]
        bad = [row[0] for row in ranked[split:]]
        best = None
        best_ratio = -math.inf
        for _ in range(samples):
            candidate = {item.name: self._tpe_value(item, good) for item in self.parameters}
            ratio = sum(self._density_ratio(item, candidate[item.name], good, bad) for item in self.parameters)
            if ratio > best_ratio:
                best, best_ratio = candidate, ratio
        return best or self.random_candidate()

    def _grid_values(self, item: SearchParameter, limit: int) -> list[Any]:
        if item.kind == "boolean":
            return [False, True]
        if item.choices:
            return list(item.choices)
        low = float(item.minimum or 0)
        high = float(item.maximum if item.maximum is not None else low)
        step = float(item.step or ((high - low) / max(1, min(limit, 10) - 1)) or 1)
        count = max(1, min(limit, int(math.floor((high - low) / step)) + 1))
        values = [min(high, low + index * step) for index in range(count)]
        if values[-1] < high and len(values) < limit:
            values.append(high)
        return [self._cast(item, value) for value in values]

    def _random_value(self, item: SearchParameter) -> Any:
        if item.kind == "boolean":
            return bool(self.random.getrandbits(1))
        if item.choices:
            return self.random.choice(item.choices)
        low = float(item.minimum or 0)
        high = float(item.maximum if item.maximum is not None else low)
        value = self.random.uniform(low, high)
        if item.step:
            value = low + round((value - low) / item.step) * item.step
        return self._cast(item, min(high, max(low, value)))

    def _tpe_value(self, item: SearchParameter, good: list[dict[str, Any]]) -> Any:
        if item.kind == "boolean" or item.choices:
            values = [row.get(item.name, item.default) for row in good]
            counts = Counter(values)
            population = list(counts)
            weights = [counts[value] + 1 for value in population]
            return self.random.choices(population, weights=weights, k=1)[0]
        values = [float(row[item.name]) for row in good if row.get(item.name) is not None]
        if not values:
            return self._random_value(item)
        center = self.random.choice(values)
        low = float(item.minimum or 0)
        high = float(item.maximum if item.maximum is not None else low)
        bandwidth = max((high - low) / max(6.0, math.sqrt(len(values)) * 3), float(item.step or 0))
        value = self.random.gauss(center, bandwidth)
        if item.step:
            value = low + round((value - low) / item.step) * item.step
        return self._cast(item, min(high, max(low, value)))

    def _density_ratio(self, item: SearchParameter, value: Any, good: list[dict[str, Any]], bad: list[dict[str, Any]]) -> float:
        if item.kind == "boolean" or item.choices:
            good_count = sum(1 for row in good if row.get(item.name) == value) + 1
            bad_count = sum(1 for row in bad if row.get(item.name) == value) + 1
            return math.log(good_count / (len(good) + 2)) - math.log(bad_count / (len(bad) + 2))
        low = float(item.minimum or 0)
        high = float(item.maximum if item.maximum is not None else low)
        bandwidth = max((high - low) / 12, float(item.step or 1e-9))
        return math.log(self._kernel_density(float(value), good, item.name, bandwidth) + 1e-12) - math.log(
            self._kernel_density(float(value), bad, item.name, bandwidth) + 1e-12
        )

    @staticmethod
    def _kernel_density(value: float, rows: list[dict[str, Any]], name: str, bandwidth: float) -> float:
        values = [float(row[name]) for row in rows if row.get(name) is not None]
        if not values:
            return 1e-12
        return sum(math.exp(-0.5 * ((value - item) / bandwidth) ** 2) for item in values) / (len(values) * bandwidth)

    @staticmethod
    def _cast(item: SearchParameter, value: float) -> Any:
        if item.kind == "integer":
            return int(round(value))
        return round(float(value), 10)
