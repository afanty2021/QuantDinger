"""Standalone clean-environment worker for untrusted indicator code.

This module is launched with ``python -I`` and must stay independent from the
``app`` package: importing the application package would load ``.env`` before
the sandbox starts.  The parent passes and receives JSON only.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import math
import os
from datetime import date, datetime
from pathlib import Path
import sys
import types
from typing import Any


_TYPE_KEY = "__quantdinger_sandbox_type__"
_STRATEGY_TYPE_KEY = "__quantdinger_strategy_type__"


class _NullWriter(io.TextIOBase):
    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


def _install_safe_exec_import_stubs(base_dir: Path) -> None:
    """Load safe_exec without importing app/__init__.py and its dotenv hook."""
    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = []
    utils_pkg = types.ModuleType("app.utils")
    utils_pkg.__path__ = [str(base_dir)]
    logger_stub = types.ModuleType("app.utils.logger")
    logger_stub.get_logger = logging.getLogger

    thread_spec = importlib.util.spec_from_file_location(
        "app.utils.thread_capacity",
        base_dir / "thread_capacity.py",
    )
    if thread_spec is None or thread_spec.loader is None:
        raise RuntimeError("sandbox worker could not load thread capacity helper")
    thread_module = importlib.util.module_from_spec(thread_spec)
    thread_spec.loader.exec_module(thread_module)

    sys.modules.update({
        "app": app_pkg,
        "app.utils": utils_pkg,
        "app.utils.logger": logger_stub,
        "app.utils.thread_capacity": thread_module,
    })


def _load_safe_exec(base_dir: Path):
    _install_safe_exec_import_stubs(base_dir)
    spec = importlib.util.spec_from_file_location(
        "quantdinger_sandbox_safe_exec",
        base_dir / "safe_exec.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("sandbox worker could not load execution helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decode(value: Any, pd, np) -> Any:
    if isinstance(value, list):
        return [_decode(item, pd, np) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get(_TYPE_KEY)
    if kind == "datetime":
        return pd.Timestamp(value.get("value"))
    if kind == "dataframe":
        return pd.DataFrame(
            data=[[_decode(item, pd, np) for item in row] for row in value.get("data", [])],
            columns=[str(item) for item in value.get("columns", [])],
            index=[_decode(item, pd, np) for item in value.get("index", [])],
        )
    if kind == "series":
        return pd.Series(
            [_decode(item, pd, np) for item in value.get("data", [])],
            index=[_decode(item, pd, np) for item in value.get("index", [])],
            name=value.get("name"),
        )
    if kind == "ndarray":
        return np.asarray(value.get("data", []))
    return {str(key): _decode(item, pd, np) for key, item in value.items()}


def _encode(value: Any, pd, np, *, depth: int = 0) -> Any:
    if depth > 40:
        raise ValueError("sandbox result nesting is too deep")
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, np.generic):
        return _encode(value.item(), pd, np, depth=depth + 1)
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return {_TYPE_KEY: "datetime", "value": value.isoformat()}
    if isinstance(value, pd.DataFrame):
        return {
            _TYPE_KEY: "dataframe",
            "columns": [str(item) for item in value.columns],
            "index": [_encode(item, pd, np, depth=depth + 1) for item in value.index],
            "data": [
                [_encode(item, pd, np, depth=depth + 1) for item in row]
                for row in value.itertuples(index=False, name=None)
            ],
        }
    if isinstance(value, pd.Series):
        return {
            _TYPE_KEY: "series",
            "name": None if value.name is None else str(value.name),
            "index": [_encode(item, pd, np, depth=depth + 1) for item in value.index],
            "data": [_encode(item, pd, np, depth=depth + 1) for item in value.tolist()],
        }
    if isinstance(value, np.ndarray):
        return {
            _TYPE_KEY: "ndarray",
            "data": _encode(value.tolist(), pd, np, depth=depth + 1),
        }
    if isinstance(value, dict):
        return {
            str(key): _encode(item, pd, np, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_encode(item, pd, np, depth=depth + 1) for item in value]
    raise TypeError(f"sandbox result type is not allowed: {type(value).__name__}")


def _deny_network() -> None:
    """Disable Python socket creation as defense in depth inside the worker."""
    try:
        import socket

        def denied(*_args: Any, **_kwargs: Any):
            raise PermissionError("network access is disabled in the sandbox")

        socket.socket = denied
        socket.create_connection = denied
        socket.socketpair = denied
    except Exception:
        pass


def _apply_resource_limits(timeout: int, max_memory_mb: int) -> bool:
    """Apply limits inside the exec'd worker, before third-party imports."""
    if sys.platform == "win32":
        return False
    try:
        import resource

        resource.setrlimit(
            resource.RLIMIT_CPU,
            (max(1, timeout), max(2, timeout + 1)),
        )
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (32 * 1024 * 1024, 32 * 1024 * 1024),
        )
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        memory_limited = False
        if hasattr(resource, "RLIMIT_AS"):
            memory = max(128, max_memory_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            memory_limited = True
        return memory_limited
    except (ImportError, OSError, ValueError):
        # The parent still enforces a wall-clock timeout and output cap.
        return False


class _StrategyDiscoveryState:
    pass


class _StrategyDiscoveryLogger:
    def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    debug = __call__
    info = __call__
    warning = __call__
    warn = __call__
    error = __call__


def _strategy_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 30:
        raise ValueError("strategy discovery result nesting is too deep")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return {_STRATEGY_TYPE_KEY: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {_STRATEGY_TYPE_KEY: "date", "value": value.isoformat()}
    if isinstance(value, list):
        return [_strategy_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return {
            _STRATEGY_TYPE_KEY: "tuple",
            "items": [_strategy_json_value(item, depth=depth + 1) for item in value],
        }
    if isinstance(value, set):
        return {
            _STRATEGY_TYPE_KEY: "set",
            "items": [_strategy_json_value(item, depth=depth + 1) for item in value],
        }
    if isinstance(value, types.SimpleNamespace):
        return {
            _STRATEGY_TYPE_KEY: "namespace",
            "values": _strategy_json_value(vars(value), depth=depth + 1),
        }
    if isinstance(value, dict):
        return {
            str(key): _strategy_json_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    raise TypeError(
        f"strategy initialize state must be JSON-compatible, got {type(value).__name__}"
    )


class _StrategyDiscoveryContext:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.schedules: list[dict[str, Any]] = []
        self.current_dt = None
        self.previous_trading_date = None
        self.portfolio = types.SimpleNamespace(
            starting_cash=0.0,
            available_cash=0.0,
            total_value=0.0,
            positions={},
        )

    def _record(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.calls.append({
            "name": name,
            "args": _strategy_json_value(list(args)),
            "kwargs": _strategy_json_value(kwargs),
        })

    def set_universe(self, *args: Any, **kwargs: Any) -> None:
        self._record("set_universe", args, kwargs)

    def set_benchmark(self, *args: Any, **kwargs: Any) -> None:
        self._record("set_benchmark", args, kwargs)

    def subscribe(self, *args: Any, **kwargs: Any) -> None:
        self._record("subscribe", args, kwargs)

    def set_warmup(self, *args: Any, **kwargs: Any) -> None:
        self._record("set_warmup", args, kwargs)

    def allow_leverage(self, *args: Any, **kwargs: Any) -> None:
        self._record("allow_leverage", args, kwargs)

    def set_metadata(self, *args: Any, **kwargs: Any) -> None:
        self._record("set_metadata", args, kwargs)

    def schedule(self, frequency: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        callback = kwargs.get("callback")
        for value in args:
            if callable(value):
                callback = value
                break
        if not callable(callback):
            raise ValueError("strategyV2.scheduleCallbackRequired")
        self.schedules.append({
            "frequency": frequency,
            "callback": str(getattr(callback, "__name__", "scheduled")),
            "time": str(kwargs.get("time") or ""),
            "weekday": int(kwargs.get("weekday", 1)) if frequency == "weekly" else None,
            "monthday": int(kwargs.get("monthday", 1)) if frequency == "monthly" else None,
        })


def _execute_strategy_discovery(request: dict[str, Any], safe_exec) -> dict[str, Any]:
    code = str(request.get("code") or "")
    timeout = max(0.05, float(request.get("timeout") or 10))
    state = _StrategyDiscoveryState()
    context = _StrategyDiscoveryContext()
    logger = _StrategyDiscoveryLogger()
    position = types.SimpleNamespace(amount=0.0, avg_cost=0.0, last_price=0.0)
    namespace: dict[str, Any] = {
        "__builtins__": safe_exec.build_safe_builtins(),
        "g": state,
        "run_daily": lambda *args, **kwargs: context.schedule("daily", args, kwargs),
        "run_weekly": lambda *args, **kwargs: context.schedule("weekly", args, kwargs),
        "run_monthly": lambda *args, **kwargs: context.schedule("monthly", args, kwargs),
        "get_index_stocks": lambda value, **_kwargs: [value],
        "get_universe_stocks": lambda *_args, **_kwargs: [],
        "get_position": lambda *_args, **_kwargs: position,
        "get_positions": lambda *_args, **_kwargs: {},
        "get_order_status": lambda *_args, **_kwargs: {
            "status": "unknown",
            "filled_quantity": 0.0,
            "filled_notional": 0.0,
            "fee": 0.0,
        },
        "cancel_order": lambda *_args, **_kwargs: False,
        "consume_last_exit_reason": lambda *_args, **_kwargs: "",
        "get_history": lambda *_args, **_kwargs: [],
        "history": lambda *_args, **_kwargs: [],
        "is_trade": lambda *_args, **_kwargs: False,
        "log": logger,
    }
    module_result = safe_exec.safe_exec_with_validation(
        code,
        namespace,
        namespace,
        timeout=timeout,
        max_memory_mb=max(128, int(request.get("max_memory_mb") or 512)),
        pre_import="",
    )
    if not module_result.get("success"):
        return {
            "success": False,
            "error": str(module_result.get("error") or "strategyV2.compileFailed")[:4000],
            "result": None,
        }
    if not callable(namespace.get("initialize")):
        return {
            "success": False,
            "error": "strategyV2.initializeRequired",
            "result": None,
        }
    namespace["discovery_context"] = context
    try:
        initialize_result = safe_exec.safe_exec_code(
            "initialize(discovery_context)",
            namespace,
            namespace,
            timeout=timeout,
            max_memory_mb=max(128, int(request.get("max_memory_mb") or 512)),
        )
    finally:
        namespace.pop("discovery_context", None)
    if not initialize_result.get("success"):
        return {
            "success": False,
            "error": (
                "strategyV2.initializeFailed:"
                + str(initialize_result.get("error") or "strategyV2.compileFailed")[:3900]
            ),
            "result": None,
        }
    handlers = [
        name for name in (
            "initialize",
            "before_trading_start",
            "handle_data",
            "after_trading_end",
            "on_rebalance",
        )
        if callable(namespace.get(name))
    ]
    return {
        "success": True,
        "error": None,
        "result": {
            "calls": context.calls,
            "schedules": context.schedules,
            "state": _strategy_json_value(vars(state)),
            "handlers": handlers,
        },
    }


def _execute(request: dict[str, Any]) -> dict[str, Any]:
    _deny_network()
    base_dir = Path(__file__).resolve().parent
    safe_exec = _load_safe_exec(base_dir)
    if request.get("mode") == "strategy_discovery":
        return _execute_strategy_discovery(request, safe_exec)

    # pandas/numpy are imported only after the process has started with the
    # parent's clean environment.  They never observe API or broker secrets.
    import numpy as np
    import pandas as pd

    input_data = _decode(request.get("input") or {}, pd, np)
    if not isinstance(input_data, dict):
        raise TypeError("sandbox input must be an object")
    exec_env: dict[str, Any] = dict(input_data)
    exec_env.update({
        "pd": pd,
        "np": np,
        "math": math,
        "output": exec_env.get("output"),
        "__builtins__": safe_exec.build_safe_builtins(),
    })
    df = exec_env.get("df")
    if isinstance(df, pd.DataFrame):
        for column in ("open", "high", "low", "close", "volume"):
            if column in df.columns:
                exec_env[column] = df[column]

    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = _NullWriter()
    sys.stderr = _NullWriter()
    try:
        result = safe_exec.safe_exec_with_validation(
            code=str(request.get("code") or ""),
            exec_globals=exec_env,
            exec_locals=exec_env,
            timeout=max(1, int(request.get("timeout") or 20)),
            max_memory_mb=max(128, int(request.get("max_memory_mb") or 1024)),
        )
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr

    if not result.get("success"):
        return {
            "success": False,
            "error": str(result.get("error") or "sandbox execution failed")[:4000],
            "result": None,
        }
    return {
        "success": True,
        "error": None,
        "result": {
            "output": _encode(exec_env.get("output"), pd, np),
            "df": _encode(exec_env.get("df"), pd, np),
        },
    }


def main() -> int:
    # Popen already supplies a clean environment. Clear it again before any
    # third-party import so a future caller cannot accidentally weaken that
    # contract by changing only the parent-side launcher.
    os.environ.clear()
    os.environ.update({
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONNOUSERSITE": "1",
        "QD_SANDBOX_WORKER": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    })
    protocol_out = sys.stdout
    try:
        request = json.loads(sys.stdin.buffer.read())
        memory_limited = _apply_resource_limits(
            max(1, int(request.get("timeout") or 20)),
            max(128, int(request.get("max_memory_mb") or 1024)),
        )
        if (
            request.get("mode") == "strategy_discovery"
            and not memory_limited
            and not request.get("parent_memory_limit")
        ):
            raise RuntimeError("strategy sandbox memory limit could not be applied")
        response = _execute(request)
    except Exception as exc:
        response = {
            "success": False,
            "error": f"Sandbox worker error: {type(exc).__name__}: {str(exc)[:1000]}",
            "result": None,
        }
    protocol_out.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    protocol_out.flush()
    return 0 if response.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
