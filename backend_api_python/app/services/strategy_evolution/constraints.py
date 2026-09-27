"""Safe, generic constraints for strategy evolution parameter candidates."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, TypeAlias


ExpressionTree: TypeAlias = tuple[Any, ...]


@dataclass(frozen=True)
class ParameterConstraint:
    expression: str
    tree: ExpressionTree
    source: str
    rejects_when_true: bool

    def accepts(self, params: dict[str, Any]) -> bool:
        try:
            value = bool(_evaluate(self.tree, params))
        except (ArithmeticError, KeyError, TypeError, ValueError):
            return True
        return not value if self.rejects_when_true else value

    def metadata(self) -> dict[str, str]:
        return {"expression": self.expression, "source": self.source}


def discover_parameter_constraints(code: str, parameter_names: set[str]) -> tuple[ParameterConstraint, ...]:
    """Find explicit declarations and parameter-only early-return guards."""

    constraints = _declared_constraints(code, parameter_names)
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return tuple(constraints)

    functions = (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    for function in functions:
        aliases = {name: name for name in parameter_names}
        for node in ast.walk(function):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            parameter = _parameter_get_name(node.value)
            if parameter in parameter_names:
                aliases[targets[0].id] = parameter

        for node in ast.walk(function):
            if not isinstance(node, ast.If) or not _is_bare_return(node.body):
                continue
            compiled = _compile_expression(node.test, aliases)
            if compiled is None:
                continue
            constraint = ParameterConstraint(_expression_text(node.test, aliases), compiled, "guard", True)
            if constraint not in constraints:
                constraints.append(constraint)
    return tuple(constraints)


def first_rejection(
    constraints: tuple[ParameterConstraint, ...],
    params: dict[str, Any],
) -> ParameterConstraint | None:
    return next((constraint for constraint in constraints if not constraint.accepts(params)), None)


def _declared_constraints(code: str, parameter_names: set[str]) -> list[ParameterConstraint]:
    aliases = {name: name for name in parameter_names}
    constraints: list[ParameterConstraint] = []
    for raw_line in code.splitlines():
        stripped = raw_line.strip()
        marker = "@constraint"
        if not stripped.startswith("#") or marker not in stripped:
            continue
        expression = stripped.split(marker, 1)[1].strip()
        if not expression:
            continue
        try:
            parsed = ast.parse(expression, mode="eval").body
        except SyntaxError:
            continue
        compiled = _compile_expression(parsed, aliases)
        if compiled is not None:
            constraints.append(ParameterConstraint(expression, compiled, "declaration", False))
    return constraints


def _compile_expression(node: ast.AST, aliases: dict[str, str]) -> ExpressionTree | None:
    if isinstance(node, ast.Name) and node.id in aliases:
        return ("param", aliases[node.id])
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return ("literal", node.value)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = [_compile_expression(item, aliases) for item in node.elts]
        return ("sequence", tuple(values)) if all(item is not None for item in values) else None
    if isinstance(node, ast.BoolOp):
        values = [_compile_expression(item, aliases) for item in node.values]
        operator = "and" if isinstance(node.op, ast.And) else "or" if isinstance(node.op, ast.Or) else ""
        return (operator, tuple(values)) if operator and all(item is not None for item in values) else None
    if isinstance(node, ast.UnaryOp):
        operand = _compile_expression(node.operand, aliases)
        operator = "not" if isinstance(node.op, ast.Not) else "neg" if isinstance(node.op, ast.USub) else "pos" if isinstance(node.op, ast.UAdd) else ""
        return (operator, operand) if operator and operand is not None else None
    if isinstance(node, ast.BinOp):
        left = _compile_expression(node.left, aliases)
        right = _compile_expression(node.right, aliases)
        operator = {
            ast.Add: "add",
            ast.Sub: "sub",
            ast.Mult: "mul",
            ast.Div: "div",
            ast.FloorDiv: "floordiv",
            ast.Mod: "mod",
            ast.Pow: "pow",
        }.get(type(node.op), "")
        return (operator, left, right) if operator and left is not None and right is not None else None
    if isinstance(node, ast.Compare):
        operands = [_compile_expression(node.left, aliases)] + [
            _compile_expression(item, aliases) for item in node.comparators
        ]
        operators = [_comparison_operator(item) for item in node.ops]
        if not all(item is not None for item in operands) or not all(operators):
            return None
        return ("compare", tuple(operators), tuple(operands))
    return None


def _evaluate(tree: ExpressionTree, params: dict[str, Any]) -> Any:
    kind = tree[0]
    if kind == "param":
        return params[tree[1]]
    if kind == "literal":
        return tree[1]
    if kind == "sequence":
        return tuple(_evaluate(item, params) for item in tree[1])
    if kind == "and":
        return all(bool(_evaluate(item, params)) for item in tree[1])
    if kind == "or":
        return any(bool(_evaluate(item, params)) for item in tree[1])
    if kind == "not":
        return not bool(_evaluate(tree[1], params))
    if kind == "neg":
        return -_evaluate(tree[1], params)
    if kind == "pos":
        return +_evaluate(tree[1], params)
    if kind in {"add", "sub", "mul", "div", "floordiv", "mod", "pow"}:
        left = _evaluate(tree[1], params)
        right = _evaluate(tree[2], params)
        return {
            "add": lambda: left + right,
            "sub": lambda: left - right,
            "mul": lambda: left * right,
            "div": lambda: left / right,
            "floordiv": lambda: left // right,
            "mod": lambda: left % right,
            "pow": lambda: left**right,
        }[kind]()
    if kind == "compare":
        values = [_evaluate(item, params) for item in tree[2]]
        return all(_compare(operator, left, right) for operator, left, right in zip(tree[1], values, values[1:]))
    raise ValueError("unsupported constraint expression")


def _compare(operator: str, left: Any, right: Any) -> bool:
    return {
        "lt": lambda: left < right,
        "le": lambda: left <= right,
        "gt": lambda: left > right,
        "ge": lambda: left >= right,
        "eq": lambda: left == right,
        "ne": lambda: left != right,
        "in": lambda: left in right,
        "not_in": lambda: left not in right,
    }[operator]()


def _parameter_get_name(node: ast.AST | None) -> str:
    current = node
    if (
        isinstance(current, ast.Call)
        and isinstance(current.func, ast.Name)
        and current.func.id in {"int", "float", "bool", "str"}
        and current.args
    ):
        current = current.args[0]
    if not isinstance(current, ast.Call) or not isinstance(current.func, ast.Attribute):
        return ""
    if current.func.attr != "get" or not current.args or not isinstance(current.args[0], ast.Constant):
        return ""
    owner = current.func.value
    if not isinstance(owner, ast.Attribute) or owner.attr != "params":
        return ""
    return str(current.args[0].value or "")


def _is_bare_return(body: list[ast.stmt]) -> bool:
    return len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is None


def _comparison_operator(operator: ast.cmpop) -> str:
    return {
        ast.Lt: "lt",
        ast.LtE: "le",
        ast.Gt: "gt",
        ast.GtE: "ge",
        ast.Eq: "eq",
        ast.NotEq: "ne",
        ast.In: "in",
        ast.NotIn: "not_in",
    }.get(type(operator), "")


def _expression_text(node: ast.AST, aliases: dict[str, str]) -> str:
    copied = ast.parse(ast.unparse(node), mode="eval")
    copied = ast.fix_missing_locations(_AliasNormalizer(aliases).visit(copied))
    return ast.unparse(copied.body)


class _AliasNormalizer(ast.NodeTransformer):
    def __init__(self, aliases: dict[str, str]) -> None:
        self.aliases = aliases

    def visit_Name(self, node: ast.Name) -> ast.Name:
        return ast.copy_location(ast.Name(id=self.aliases.get(node.id, node.id), ctx=node.ctx), node)
