"""The two expression grammars of the semantic layer.

They are deliberately different, and neither is SQL:

    measure   arithmetic over unqualified columns of ONE entity
              "quantity * unit_price"        aggregation lives in its own column

    metric    arithmetic over ${references} to measures and other metrics
              "${gross_revenue} / ${order_count}"

Both are small enough to validate exhaustively, which is what makes it possible
to say a metric cannot express an injection rather than merely that it is
unlikely to.
"""

import re

REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER = re.compile(r"\d+(\.\d+)?")
# Everything an expression may contain once names and numbers are removed.
OPERATORS = set("+-*/() \t")
# An identifier immediately followed by "(" is a function call.
FUNCTION_CALL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*\(")


class ExpressionError(ValueError):
    """The expression is not valid in its grammar."""


def _check_no_comment_marker(expression: str) -> None:
    """`--` parses as two minus signs here but starts a comment in SQL. The
    database CHECK rejects it; the validator must agree or the two layers
    disagree about what is legal."""
    if "--" in expression:
        raise ExpressionError(f"{expression!r} contains '--', which begins a SQL comment")


def _check_balanced(expression: str) -> None:
    depth = 0
    for char in expression:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ExpressionError("Unbalanced parentheses: a ')' has no opening '('")
    if depth:
        raise ExpressionError(f"Unbalanced parentheses: {depth} unclosed '('")


def _check_leftovers(remainder: str, expression: str) -> None:
    illegal = sorted({char for char in remainder if char not in OPERATORS})
    if illegal:
        rendered = " ".join(repr(char) for char in illegal)
        raise ExpressionError(
            f"{expression!r} contains characters that are not allowed here: {rendered}"
        )


def parse_measure_columns(expression: str) -> list[str]:
    """Column names referenced by a measure expression, in order, deduplicated."""
    return list(dict.fromkeys(IDENTIFIER.findall(expression)))


def validate_measure_expression(expression: str) -> list[str]:
    """Raise if invalid; otherwise return the columns it references."""
    if not expression.strip():
        raise ExpressionError("Measure expression is empty")
    _check_no_comment_marker(expression)
    if FUNCTION_CALL.search(expression):
        raise ExpressionError(
            f"{expression!r} looks like a function call. A measure's aggregation is a "
            "separate field, so the expression holds only columns and arithmetic."
        )
    _check_balanced(expression)

    remainder = NUMBER.sub(" ", IDENTIFIER.sub(" ", expression))
    _check_leftovers(remainder, expression)

    columns = parse_measure_columns(expression)
    if not columns:
        raise ExpressionError(f"{expression!r} references no columns")
    return columns


def parse_metric_references(expression: str) -> list[str]:
    """Names inside ${...}, in order, deduplicated."""
    return list(dict.fromkeys(REFERENCE.findall(expression)))


def validate_metric_expression(expression: str) -> list[str]:
    """Raise if invalid; otherwise return the names it references."""
    if not expression.strip():
        raise ExpressionError("Metric expression is empty")
    _check_no_comment_marker(expression)
    _check_balanced(expression)

    references = parse_metric_references(expression)
    if not references:
        raise ExpressionError(
            f"{expression!r} references nothing. A metric composes measures or other "
            "metrics, written as ${name}."
        )

    remainder = NUMBER.sub(" ", REFERENCE.sub(" ", expression))
    if IDENTIFIER.search(remainder):
        stray = IDENTIFIER.search(remainder).group()
        raise ExpressionError(
            f"{expression!r} contains the bare name {stray!r}. Metrics may only refer to "
            "measures and metrics, written as ${" + stray + "}."
        )
    _check_leftovers(remainder, expression)
    return references
