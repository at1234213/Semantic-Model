"""Step 18: the two expression grammars."""

import pytest

from app.services.expressions import (
    ExpressionError,
    validate_measure_expression,
    validate_metric_expression,
)


@pytest.mark.parametrize(
    "expression,columns",
    [
        ("amount", ["amount"]),
        ("quantity * unit_price", ["quantity", "unit_price"]),
        ("(price - discount) * quantity", ["price", "discount", "quantity"]),
        ("amount / 100", ["amount"]),
        ("amount * 1.5", ["amount"]),
    ],
)
def test_valid_measure_expressions(expression: str, columns: list[str]) -> None:
    assert validate_measure_expression(expression) == columns


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(amount)",                    # aggregation is a separate field
        "COALESCE(amount, 0)",
        "amount; DROP TABLE tenants",
        "amount' OR '1'='1",
        "amount, other",                  # commas would widen the select list
        "orders.amount",                  # qualification would escape the entity
        "(amount",
        "amount)",
        "",
        "   ",
        "42",                             # references no columns
        "amount -- comment",
    ],
)
def test_invalid_measure_expressions(expression: str) -> None:
    with pytest.raises(ExpressionError):
        validate_measure_expression(expression)


@pytest.mark.parametrize(
    "expression,references",
    [
        ("${gross_revenue}", ["gross_revenue"]),
        ("${gross_revenue} / ${order_count}", ["gross_revenue", "order_count"]),
        ("(${a} - ${b}) / ${a}", ["a", "b"]),
        ("${revenue} * 100", ["revenue"]),
    ],
)
def test_valid_metric_expressions(expression: str, references: list[str]) -> None:
    assert validate_metric_expression(expression) == references


@pytest.mark.parametrize(
    "expression",
    [
        "gross_revenue / order_count",    # bare names, not references
        "${a} + NOW()",
        "SUM(${a})",
        "${a}; DROP TABLE tenants",
        "${a} + 'literal'",
        "(${a}",
        "",
        "100",                            # references nothing
    ],
)
def test_invalid_metric_expressions(expression: str) -> None:
    with pytest.raises(ExpressionError):
        validate_metric_expression(expression)


def test_error_message_names_the_offending_text() -> None:
    with pytest.raises(ExpressionError) as exc:
        validate_metric_expression("${a} + NOW()")
    assert "NOW" in str(exc.value)
