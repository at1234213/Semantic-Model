"""Parsing the two expression grammars into a tiny AST, and emitting SQL from it.

Substituting strings would be simpler and wrong. `${a} / ${b}` has to become
`a / NULLIF(b, 0)`, and knowing which subexpression is a divisor means knowing
the structure — a regex cannot tell the `/` in `${a} / (${b} + ${c})` from the
one in `(${a} / ${b}) + ${c}`.

The grammars are small enough to parse exhaustively, which is what lets the
compiler emit only shapes it constructed itself.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.services.expressions import ExpressionError

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Number:
    value: str


@dataclass(frozen=True)
class Atom:
    """A column (measure grammar) or a ${reference} (metric grammar)."""

    name: str


@dataclass(frozen=True)
class Negate:
    operand: "Node"


@dataclass(frozen=True)
class BinaryOp:
    operator: str
    left: "Node"
    right: "Node"


Node = Number | Atom | Negate | BinaryOp


def _tokenize(expression: str, atom_pattern: re.Pattern[str], *, group: int) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        char = expression[position]
        if char.isspace():
            position += 1
            continue
        if char in "+-*/()":
            tokens.append(char)
            position += 1
            continue
        match = atom_pattern.match(expression, position)
        if match:
            tokens.append(f"@{match.group(group)}")
            position = match.end()
            continue
        match = _NUMBER.match(expression, position)
        if match:
            tokens.append(f"#{match.group()}")
            position = match.end()
            continue
        raise ExpressionError(f"Unexpected character {char!r} in {expression!r}")
    if not tokens:
        raise ExpressionError(f"{expression!r} is empty")
    return tokens


class _Parser:
    def __init__(self, tokens: list[str], expression: str) -> None:
        self._tokens = tokens
        self._position = 0
        self._expression = expression

    def _peek(self) -> str | None:
        return self._tokens[self._position] if self._position < len(self._tokens) else None

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise ExpressionError(f"{self._expression!r} ends unexpectedly")
        self._position += 1
        return token

    def parse(self) -> Node:
        node = self._expr()
        if self._peek() is not None:
            raise ExpressionError(
                f"Unexpected trailing input in {self._expression!r}"
            )
        return node

    def _expr(self) -> Node:
        node = self._term()
        while self._peek() in ("+", "-"):
            operator = self._take()
            node = BinaryOp(operator, node, self._term())
        return node

    def _term(self) -> Node:
        node = self._factor()
        while self._peek() in ("*", "/"):
            operator = self._take()
            node = BinaryOp(operator, node, self._factor())
        return node

    def _factor(self) -> Node:
        token = self._take()
        if token == "-":
            return Negate(self._factor())
        if token == "(":
            node = self._expr()
            if self._take() != ")":
                raise ExpressionError(f"Unbalanced parentheses in {self._expression!r}")
            return node
        if token.startswith("@"):
            return Atom(token[1:])
        if token.startswith("#"):
            return Number(token[1:])
        raise ExpressionError(f"Unexpected {token!r} in {self._expression!r}")


def parse_measure_expression(expression: str) -> Node:
    """Arithmetic over unqualified columns of one entity."""
    return _Parser(_tokenize(expression, _IDENTIFIER, group=0), expression).parse()


def parse_metric_expression(expression: str) -> Node:
    """Arithmetic over ${references} to measures and other metrics."""
    return _Parser(_tokenize(expression, _REFERENCE, group=1), expression).parse()


def emit(node: Node, resolve_atom: Callable[[str], str]) -> str:
    """Render a node as SQL, guarding every division against a zero divisor."""
    if isinstance(node, Number):
        return node.value
    if isinstance(node, Atom):
        return resolve_atom(node.name)
    if isinstance(node, Negate):
        return f"-({emit(node.operand, resolve_atom)})"

    left = emit(node.left, resolve_atom)
    right = emit(node.right, resolve_atom)
    if node.operator == "/":
        # The modeller writes ${a} / ${b}; they should not have to think about
        # a period with no orders in it.
        return f"({left} / NULLIF({right}, 0))"
    return f"({left} {node.operator} {right})"
