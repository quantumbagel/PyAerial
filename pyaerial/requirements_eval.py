"""
Safe evaluation of zone requirement expressions (boolean combinations of component names).
Replaces eval() with an AST-limited evaluator.
"""
from __future__ import annotations

import ast
from typing import Mapping


class RequirementError(ValueError):
    pass


def _check_allowed(node: ast.AST) -> None:
    if isinstance(node, ast.Expression):
        _check_allowed(node.body)
        return
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise RequirementError("only 'and' / 'or' are allowed")
        for v in node.values:
            _check_allowed(v)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.Not):
            raise RequirementError("only unary 'not' is allowed")
        _check_allowed(node.operand)
        return
    if isinstance(node, ast.Name):
        return
    if isinstance(node, ast.Constant):
        if node.value is True or node.value is False:
            return
        raise RequirementError("only True/False constants are allowed")
    if isinstance(node, ast.Load):
        return
    raise RequirementError(f"disallowed syntax in requirement expression: {type(node).__name__}")


def collect_component_names(expression: str) -> list[str]:
    """Return unique component identifiers referenced in the expression."""
    tree = ast.parse(expression, mode="eval")
    seen: dict[str, None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id not in ("True", "False"):
                seen.setdefault(node.id, None)
    return list(seen.keys())


def eval_requirement(expression: str, env: Mapping[str, bool]) -> bool:
    """
    Evaluate a boolean expression using only `and`, `or`, `not`, True/False, and variable names.
    Names are looked up in env (component name -> bool).
    """
    tree = ast.parse(expression, mode="eval")
    _check_allowed(tree)

    def eval_node(node: ast.AST) -> bool:
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(eval_node(v) for v in node.values)
            if isinstance(node.op, ast.Or):
                return any(eval_node(v) for v in node.values)
            raise RequirementError("unsupported boolean operator")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not eval_node(node.operand)
        if isinstance(node, ast.Name):
            if node.id == "True":
                return True
            if node.id == "False":
                return False
            if node.id not in env:
                raise RequirementError(f"unknown component or name {node.id!r} in requirement")
            return bool(env[node.id])
        if isinstance(node, ast.Constant):
            if node.value is True or node.value is False:
                return bool(node.value)
            raise RequirementError("invalid constant in requirement")
        raise RequirementError(f"unsupported node {type(node).__name__}")

    return eval_node(tree)
