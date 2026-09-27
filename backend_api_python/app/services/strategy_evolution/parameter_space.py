"""Automatic parameter-space contracts for strategy evolution."""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from typing import Any

from app.services.strategy_params import canonical_strategy_param_schema


_NUMERIC_TYPES = {"int", "integer", "float", "number", "percent"}
_BOOLEAN_TYPES = {"bool", "boolean"}
_ROBOT_TYPES = {"grid", "dca", "martingale", "layered_martingale"}
_RISK_NAME_TOKENS = (
    "allocation",
    "budget",
    "exposure",
    "leverage",
    "position",
    "risk",
    "size",
    "stop",
    "take_profit",
    "target_pct",
    "weight",
)


@dataclass(frozen=True)
class RobotParameterSpec:
    name: str
    constant: str
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None


_RISK_SPECS = (
    RobotParameterSpec("take_profit_pct", "TAKE_PROFIT", "percent", 0.005, 0.30, 0.005),
    RobotParameterSpec("hard_stop_pct", "HARD_STOP", "percent", 0.005, 0.30, 0.005),
    RobotParameterSpec("trailing_take_profit_enabled", "TRAILING_TAKE_PROFIT_ENABLED", "boolean"),
    RobotParameterSpec("trailing_activation_pct", "TRAILING_ACTIVATION", "percent", 0.005, 0.30, 0.005),
    RobotParameterSpec("trailing_callback_pct", "TRAILING_CALLBACK", "percent", 0.0025, 0.15, 0.0025),
    RobotParameterSpec("equity_take_profit_pct", "EQUITY_TAKE_PROFIT", "percent", 0.01, 0.50, 0.01),
    RobotParameterSpec("equity_stop_loss_pct", "EQUITY_STOP_LOSS", "percent", 0.01, 0.50, 0.01),
    RobotParameterSpec("equity_trailing_enabled", "EQUITY_TRAILING_ENABLED", "boolean"),
    RobotParameterSpec("equity_trailing_activation_pct", "EQUITY_TRAILING_ACTIVATION", "percent", 0.01, 0.50, 0.01),
    RobotParameterSpec("equity_trailing_callback_pct", "EQUITY_TRAILING_CALLBACK", "percent", 0.005, 0.25, 0.005),
    RobotParameterSpec("restart_after_stop", "RESTART_AFTER_STOP", "boolean"),
)

_DCA_SPECS = (
    RobotParameterSpec("dca_interval_minutes", "DCA_INTERVAL_MINUTES", "integer", 1, 43_200, 1),
    RobotParameterSpec("dca_max_orders", "DCA_MAX_ORDERS", "integer", 1, 100, 1),
    RobotParameterSpec("dca_total_budget_pct", "DCA_TOTAL_BUDGET_PCT", "percent", 0.05, 1.0, 0.05),
    RobotParameterSpec("dca_order_pct", "DCA_ORDER_PCT", "percent", 0.01, 1.0, 0.01),
    RobotParameterSpec("dca_price_filter_enabled", "DCA_PRICE_FILTER_ENABLED", "boolean"),
    RobotParameterSpec("dca_max_adverse_price_pct", "DCA_MAX_ADVERSE_PRICE_PCT", "percent", 0.0, 0.50, 0.01),
)


def build_parameter_space(source: dict[str, Any]) -> dict[str, Any]:
    """Return an authoritative, ready-to-sample parameter space for a source."""
    code = str(source.get("code") or "")
    strategy_class, robot_type = _strategy_class(source)
    schema = canonical_strategy_param_schema(code, source.get("param_schema") or {})
    declared = list(schema.get("params") or [])
    if declared:
        parameters = [_normalize_parameter(item) for item in declared]
        parameters = [item for item in parameters if item]
        return {
            "strategyClass": strategy_class,
            "robotType": robot_type,
            "scope": "declared",
            "parameters": parameters[:8],
            "totalParameters": len(parameters),
        }

    if strategy_class == "robot" and robot_type in _ROBOT_TYPES:
        parameters = _robot_parameters(code, robot_type)
        return {
            "strategyClass": strategy_class,
            "robotType": robot_type,
            "scope": "riskExecution",
            "parameters": parameters[:8],
            "totalParameters": len(parameters),
        }

    return {
        "strategyClass": strategy_class,
        "robotType": robot_type,
        "scope": "none",
        "parameters": [],
        "totalParameters": 0,
    }


def adapt_source_for_parameters(code: str, parameter_rows: list[dict[str, Any]]) -> str:
    """Bind supported robot constants to runtime parameters for backtests."""
    bindings = [
        (str(item.get("name") or ""), str(item.get("runtimeConstant") or ""), str(item.get("type") or ""))
        for item in parameter_rows
        if item.get("runtimeConstant")
    ]
    if not bindings:
        return code
    globals_line = ", ".join(constant for _, constant, _ in bindings)
    assignments = []
    for name, constant, kind in bindings:
        cast = "bool" if kind in _BOOLEAN_TYPES else "int" if kind in {"int", "integer"} else "float"
        assignments.append(
            f"    {constant} = {cast}(context.params.get({name!r}, {constant}))"
        )
    wrapper = [
        "",
        "_qd_evolution_handle_data = handle_data",
        "def handle_data(context, data):",
        f"    global {globals_line}",
        *assignments,
        "    return _qd_evolution_handle_data(context, data)",
        "",
    ]
    constraints = []
    names = {name for name, _, _ in bindings}
    if {"dca_order_pct", "dca_total_budget_pct"}.issubset(names):
        constraints.append("# @constraint dca_order_pct <= dca_total_budget_pct")
    if {"trailing_callback_pct", "trailing_activation_pct"}.issubset(names):
        constraints.append("# @constraint trailing_callback_pct <= trailing_activation_pct")
    if {"equity_trailing_callback_pct", "equity_trailing_activation_pct"}.issubset(names):
        constraints.append("# @constraint equity_trailing_callback_pct <= equity_trailing_activation_pct")
    return "\n".join([code.rstrip(), *constraints, *wrapper])


def _strategy_class(source: dict[str, Any]) -> tuple[str, str]:
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    last_run = metadata.get("last_run_config") if isinstance(metadata.get("last_run_config"), dict) else {}
    manifest = metadata.get("strategy_manifest") if isinstance(metadata.get("strategy_manifest"), dict) else {}
    template_key = str(source.get("template_key") or "").lower()
    robot_type = str(
        last_run.get("executor_type")
        or ((last_run.get("executor_config") or {}).get("bot_type") if isinstance(last_run.get("executor_config"), dict) else "")
        or template_key.removeprefix("robot_v2_")
    ).strip().lower().replace("-", "_")
    if template_key.startswith("robot_v2_") or robot_type in _ROBOT_TYPES:
        return "robot", robot_type
    asset_type = str(source.get("asset_type") or "").lower()
    if asset_type == "portfolio_strategy" or str(manifest.get("strategyType") or "").lower() == "portfolio":
        return "portfolio", ""
    return "cta", ""


def _normalize_parameter(raw: dict[str, Any]) -> dict[str, Any] | None:
    row = dict(raw or {})
    name = str(row.get("name") or "").strip()
    kind = str(row.get("type") or "number").lower()
    if not name:
        return None
    if kind in _BOOLEAN_TYPES:
        return {
            **row,
            "name": name,
            "type": "boolean",
            "choices": [False, True],
            "autoRange": True,
            "role": "regime",
            "defaultSelected": False,
            "optimizable": False,
        }
    choices = row.get("choices") or row.get("options") or row.get("values")
    if kind not in _NUMERIC_TYPES:
        if not isinstance(choices, list) or not choices:
            return None
        return {
            **row,
            "name": name,
            "choices": _choice_values(choices),
            "autoRange": True,
            "role": "regime",
            "defaultSelected": False,
            "optimizable": False,
        }

    minimum = _finite(row.get("min"))
    maximum = _finite(row.get("max"))
    step = _finite(row.get("step"))
    numeric_choices = _numeric_values(choices)
    if (minimum is None or maximum is None) and numeric_choices:
        minimum, maximum = min(numeric_choices), max(numeric_choices)
        step = step or _smallest_step(numeric_choices)
    default = _finite(row.get("default"))
    if minimum is None or maximum is None or minimum > maximum:
        minimum, maximum, inferred_step = _infer_numeric_range(name, kind, default)
        step = step or inferred_step
    normalized_type = "integer" if kind in {"int", "integer"} else "percent" if kind == "percent" or _is_percent_name(name) else "number"
    if normalized_type == "integer":
        minimum, maximum = int(math.floor(minimum)), int(math.ceil(maximum))
        step = max(1, int(round(step or 1)))
    else:
        step = step if step and step > 0 else _decimal_step(minimum, maximum)
    normalized = dict(row)
    normalized.pop("values", None)
    normalized.pop("options", None)
    normalized.pop("choices", None)
    if numeric_choices and not _is_regular_grid(numeric_choices, minimum, maximum, step):
        normalized["choices"] = numeric_choices
    role = _parameter_role(name)
    recommended_minimum, recommended_maximum = _recommended_numeric_range(
        minimum,
        maximum,
        step,
        default,
        normalized_type,
        name,
    )
    return {
        **normalized,
        "name": name,
        "type": normalized_type,
        "min": minimum,
        "max": maximum,
        "step": step,
        "autoRange": True,
        "role": role,
        "defaultSelected": role == "signal",
        "recommendedMin": recommended_minimum,
        "recommendedMax": recommended_maximum,
    }


def _robot_parameters(code: str, robot_type: str) -> list[dict[str, Any]]:
    constants = _literal_constants(code)
    specs = (*_DCA_SPECS, *_RISK_SPECS) if robot_type == "dca" else _RISK_SPECS
    rows = []
    for spec in specs:
        if spec.constant not in constants:
            continue
        default = constants[spec.constant]
        if spec.kind == "boolean":
            rows.append({
                "name": spec.name,
                "type": "boolean",
                "default": bool(default),
                "choices": [False, True],
                "runtimeConstant": spec.constant,
                "autoRange": True,
                "role": "regime",
                "defaultSelected": False,
                "optimizable": False,
            })
            continue
        minimum, maximum = _focused_robot_range(float(default), spec)
        rows.append({
            "name": spec.name,
            "type": spec.kind,
            "default": default,
            "min": minimum,
            "max": maximum,
            "step": spec.step,
            "runtimeConstant": spec.constant,
            "autoRange": True,
            "role": "execution" if spec.name.startswith("dca_") else "risk",
            "defaultSelected": True,
            "recommendedMin": minimum,
            "recommendedMax": maximum,
        })
    return rows


def _focused_robot_range(default: float, spec: RobotParameterSpec) -> tuple[float, float]:
    minimum = float(spec.minimum if spec.minimum is not None else default)
    maximum = float(spec.maximum if spec.maximum is not None else default)
    if default > 0:
        low = max(minimum, default * 0.5)
        high = min(maximum, max(default * 2.0, default + float(spec.step or 0)))
    else:
        low = minimum
        high = min(maximum, max(minimum + float(spec.step or 1), float(spec.step or 1) * 5))
    if spec.kind == "integer":
        return int(max(minimum, math.floor(low))), int(min(maximum, math.ceil(high)))
    return _rounded(low), _rounded(high)


def _infer_numeric_range(name: str, kind: str, default: float | None) -> tuple[float, float, float]:
    value = float(default or 0.0)
    lowered = name.lower()
    if kind in {"int", "integer"} or re.search(r"period|lookback|window|length|bars|count|orders|layers|levels", lowered):
        base = max(2, int(round(value or 10)))
        return max(1, base // 2), min(1000, max(base + 1, base * 2)), 1
    if _is_percent_name(lowered):
        if value <= 0:
            return 0.0, 0.10, 0.01
        return _rounded(max(0.0, value * 0.5)), _rounded(min(1.0, max(value * 1.5, value + 0.01))), _decimal_step(0.0, value)
    if value == 0:
        return -1.0, 1.0, 0.1
    low, high = sorted((value * 0.5, value * 1.5))
    return _rounded(low), _rounded(high), _decimal_step(low, high)


def _parameter_role(name: str) -> str:
    lowered = name.lower()
    return "risk" if any(token in lowered for token in _RISK_NAME_TOKENS) else "signal"


def _recommended_numeric_range(
    minimum: float,
    maximum: float,
    step: float,
    default: float | None,
    kind: str,
    name: str,
) -> tuple[float | int, float | int]:
    if default is None or not minimum <= default <= maximum:
        return minimum, maximum
    lowered = name.lower()
    period_like = bool(re.search(r"period|lookback|window|length|bars", lowered))
    if kind == "integer" and period_like:
        low_target, high_target = default * 0.5, default * 2.5
    elif kind == "percent" or _parameter_role(name) == "risk":
        low_target, high_target = default * 0.65, default * 1.15
    elif default == 0:
        span = max(step * 10, (maximum - minimum) * 0.25)
        low_target, high_target = -span, span
    else:
        low_target, high_target = default * 0.5, default * 1.5
    low = _snap_to_step(max(minimum, low_target), minimum, step, ceiling=False)
    high = _snap_to_step(min(maximum, high_target), minimum, step, ceiling=True)
    low, high = max(minimum, low), min(maximum, high)
    if high <= low:
        low, high = minimum, maximum
    if kind == "integer":
        return int(round(low)), int(round(high))
    return _rounded(low), _rounded(high)


def _snap_to_step(value: float, minimum: float, step: float, *, ceiling: bool) -> float:
    units = (value - minimum) / step
    snapped_units = math.ceil(units) if ceiling else math.floor(units)
    return minimum + snapped_units * step


def _literal_constants(code: str) -> dict[str, Any]:
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return {}
    output = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if len(node.targets) == 1 else None)
        value = node.value
        if not isinstance(target, ast.Name):
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError):
            continue
        if isinstance(literal, (bool, int, float)):
            output[target.id] = literal
    return output


def _numeric_values(values: Any) -> list[float]:
    if not isinstance(values, (list, tuple)):
        return []
    output = []
    for value in values:
        number = _finite(value)
        if number is not None:
            output.append(number)
    return sorted(set(output))


def _choice_values(values: list[Any]) -> list[Any]:
    return [item.get("value") if isinstance(item, dict) else item for item in values]


def _smallest_step(values: list[float]) -> float:
    differences = [right - left for left, right in zip(values, values[1:]) if right > left]
    return min(differences) if differences else 1.0


def _is_regular_grid(values: list[float], minimum: float, maximum: float, step: float) -> bool:
    if not values:
        return False
    tolerance = max(1e-10, abs(float(step)) * 1e-8)
    if not math.isclose(values[0], float(minimum), rel_tol=0.0, abs_tol=tolerance):
        return False
    if not math.isclose(values[-1], float(maximum), rel_tol=0.0, abs_tol=tolerance):
        return False
    return all(
        math.isclose(right - left, float(step), rel_tol=0.0, abs_tol=tolerance)
        for left, right in zip(values, values[1:])
    )


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_percent_name(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in ("pct", "percent", "ratio", "weight", "allocation", "fraction", "rate"))


def _decimal_step(low: float, high: float) -> float:
    span = abs(float(high) - float(low))
    if span <= 0.02:
        return 0.001
    if span <= 0.2:
        return 0.005
    if span <= 2:
        return 0.05
    return 0.1


def _rounded(value: float) -> float:
    return round(float(value), 8)
