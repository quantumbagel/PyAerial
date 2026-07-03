"""
Safe boolean-expression evaluation for zone level ``requirements``.

Requirements are small boolean expressions over component names, for example
``lenient`` or ``(eta & altitude) | critical``. The previous implementation used
:func:`eval`, which allowed arbitrary code execution from the configuration file.
This module parses the expression once and evaluates it against a mapping of
component name -> bool using a restricted AST walker (names, ``and``/``or``/``not``,
and the bitwise ``&``/``|``/``~`` spellings only).
"""
from __future__ import annotations

import ast
from functools import lru_cache


class RequirementError(ValueError):
    """Raised when a requirement expression is malformed or unsafe."""


_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.BinOp, ast.BitAnd, ast.BitOr, ast.UnaryOp, ast.Invert,
    ast.Name, ast.Load, ast.Constant,
)


@lru_cache(maxsize=256)
def _parse(requirement: str) -> ast.Expression:
    try:
        tree = ast.parse(requirement, mode="eval")
    except SyntaxError as exc:
        raise RequirementError(f"could not parse requirement {requirement!r}: {exc}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RequirementError(
                f"requirement {requirement!r} uses unsupported syntax: {type(node).__name__}"
            )
    return tree


def extract_component_names(requirement: str) -> set[str]:
    """Return the set of component names referenced by a requirement."""
    tree = _parse(requirement)
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def evaluate(requirement: str, components: dict[str, bool]) -> bool:
    """
    Evaluate ``requirement`` against a mapping of component name -> bool.

    :raises RequirementError: if the expression references an unknown component
        or contains unsupported syntax.
    """
    tree = _parse(requirement)
    return bool(_eval_node(tree.body, requirement, components))


def _eval_node(node: ast.AST, requirement: str, components: dict[str, bool]) -> bool:
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, requirement, components) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, requirement, components)
        right = _eval_node(node.right, requirement, components)
        if isinstance(node.op, ast.BitAnd):
            return left and right
        if isinstance(node.op, ast.BitOr):
            return left or right
        raise RequirementError(f"unsupported operator in requirement {requirement!r}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.Invert)):
        return not _eval_node(node.operand, requirement, components)
    if isinstance(node, ast.Name):
        if node.id not in components:
            raise RequirementError(
                f"requirement {requirement!r} references unknown component {node.id!r}"
            )
        return bool(components[node.id])
    if isinstance(node, ast.Constant):
        return bool(node.value)
    raise RequirementError(f"unsupported node in requirement {requirement!r}: {type(node).__name__}")
