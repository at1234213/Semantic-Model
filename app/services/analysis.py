"""Describing a result set, without a model.

Everything here is arithmetic. A natural-language answer comes later and is
written *from* this summary, so the numbers a person reads were computed rather
than generated.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.services.execution import QueryResult

NUMERIC = (int, float, Decimal)
TEMPORAL = (date, datetime)
# Beyond this a "top values" list stops being a summary.
MAX_TOP_VALUES = 5


@dataclass
class ColumnSummary:
    name: str
    kind: str  # numeric | temporal | categorical
    null_count: int = 0
    minimum: Any = None
    maximum: Any = None
    total: float | None = None
    mean: float | None = None
    distinct_count: int | None = None
    top_values: list[tuple[Any, int]] = field(default_factory=list)


@dataclass
class ResultAnalysis:
    row_count: int
    truncated: bool
    duration_ms: float
    columns: list[ColumnSummary] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.row_count == 0

    def headline(self) -> str:
        """One sentence stating what came back, for logs and error messages."""
        if self.is_empty:
            return "No rows matched."
        rows = f"{self.row_count} row{'s' if self.row_count != 1 else ''}"
        if self.truncated:
            rows += " (truncated)"
        numeric = [c for c in self.columns if c.kind == "numeric" and c.total is not None]
        if numeric:
            totals = ", ".join(f"{c.name}={c.total:g}" for c in numeric)
            return f"{rows}; totals {totals}."
        return f"{rows}."


def _classify(values: list[Any]) -> str:
    present = [v for v in values if v is not None]
    if present and all(isinstance(v, NUMERIC) and not isinstance(v, bool) for v in present):
        return "numeric"
    if present and all(isinstance(v, TEMPORAL) for v in present):
        return "temporal"
    return "categorical"


def analyse(result: QueryResult) -> ResultAnalysis:
    analysis = ResultAnalysis(
        row_count=result.row_count,
        truncated=result.truncated,
        duration_ms=result.duration_ms,
    )

    for index, name in enumerate(result.columns):
        values = [row[index] for row in result.rows]
        present = [v for v in values if v is not None]
        kind = _classify(values)
        summary = ColumnSummary(
            name=name, kind=kind, null_count=len(values) - len(present)
        )

        if present and kind == "numeric":
            numbers = [float(v) for v in present]
            summary.minimum = min(numbers)
            summary.maximum = max(numbers)
            summary.total = sum(numbers)
            summary.mean = summary.total / len(numbers)
        elif present and kind == "temporal":
            summary.minimum = min(present)
            summary.maximum = max(present)
        elif present:
            counts: dict[Any, int] = {}
            for value in present:
                counts[value] = counts.get(value, 0) + 1
            summary.distinct_count = len(counts)
            summary.top_values = sorted(
                counts.items(), key=lambda item: (-item[1], str(item[0]))
            )[:MAX_TOP_VALUES]

        analysis.columns.append(summary)
    return analysis
