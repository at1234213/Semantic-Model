"""Compiling a Semantic Query into SQL.

Deterministic: the same IR against the same model version produces byte-identical
SQL. Nothing here consults a model, and every fragment it emits came from a row
a person wrote or from this file.

Values never appear in the SQL text. Filters, business rules and the time range
all bind parameters, so a value that somehow reached this point still cannot
change the shape of the statement.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.business_rule import BusinessRule, FilterOperator
from app.models.dimension import Dimension, TimeGranularity
from app.models.entity import Entity
from app.models.measure import Measure
from app.models.metric import Metric
from app.services.intent import FilterOperatorPhrase
from app.services.joins import JoinPathError, JoinPlan, resolve_join_path
from app.services.query_ir import SemanticQuery
from app.services.sql_expressions import emit, parse_measure_expression, parse_metric_expression

# Aggregations are emitted from this table, never from stored text.
AGGREGATION_SQL = {
    "sum": "SUM({expr})",
    "count": "COUNT({expr})",
    "count_distinct": "COUNT(DISTINCT {expr})",
    "avg": "AVG({expr})",
    "min": "MIN({expr})",
    "max": "MAX({expr})",
}

COMPARISON_SQL = {
    FilterOperatorPhrase.EQ: "=",
    FilterOperatorPhrase.NE: "<>",
    FilterOperatorPhrase.LT: "<",
    FilterOperatorPhrase.LTE: "<=",
    FilterOperatorPhrase.GT: ">",
    FilterOperatorPhrase.GTE: ">=",
}

RULE_SQL = {
    FilterOperator.EQ: "=",
    FilterOperator.NE: "<>",
    FilterOperator.LT: "<",
    FilterOperator.LTE: "<=",
    FilterOperator.GT: ">",
    FilterOperator.GTE: ">=",
}

DATE_TRUNC_GRAIN = {
    TimeGranularity.DAY: "day",
    TimeGranularity.WEEK: "week",
    TimeGranularity.MONTH: "month",
    TimeGranularity.QUARTER: "quarter",
    TimeGranularity.YEAR: "year",
}


class CompilationError(ValueError):
    """The query cannot be compiled from what the model defines."""


@dataclass
class CompiledQuery:
    sql: str
    parameters: dict[str, object] = field(default_factory=dict)
    entities: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)


class _Compiler:
    def __init__(self, db: Session, query: SemanticQuery) -> None:
        self.db = db
        self.query = query
        self.parameters: dict[str, object] = {}
        self._alias_by_entity: dict[uuid.UUID, str] = {}
        self._counter = 0

    # ---- parameters -------------------------------------------------------

    def _bind(self, value: object) -> str:
        name = f"p{len(self.parameters)}"
        self.parameters[name] = value
        return f":{name}"

    # ---- loading ----------------------------------------------------------

    def _load(self) -> None:
        version_id = self.query.version_id

        self.metrics = list(
            self.db.scalars(
                select(Metric)
                .where(Metric.id.in_(self.query.metric_ids))
                .options(selectinload(Metric.references))
            )
        )
        if len(self.metrics) != len(self.query.metric_ids):
            raise CompilationError("One or more metrics no longer exist in this version")

        self.measures = {
            measure.id: measure
            for measure in self.db.scalars(
                select(Measure).where(Measure.semantic_model_version_id == version_id)
            )
        }
        self.measures_by_name = {m.name: m for m in self.measures.values()}
        self.metrics_by_name = {
            metric.name: metric
            for metric in self.db.scalars(
                select(Metric)
                .where(Metric.semantic_model_version_id == version_id)
                .options(selectinload(Metric.references))
            )
        }

        dimension_ids = {
            *self.query.dimension_ids,
            *(f.dimension_id for f in self.query.filters),
        }
        if self.query.time_dimension_id:
            dimension_ids.add(self.query.time_dimension_id)
        self.dimensions = {
            dimension.id: dimension
            for dimension in self.db.scalars(
                select(Dimension)
                .where(Dimension.id.in_(dimension_ids))
                .options(selectinload(Dimension.entity))
            )
        }
        if len(self.dimensions) != len(dimension_ids):
            raise CompilationError("One or more dimensions no longer exist in this version")

        self.entities = {
            entity.id: entity
            for entity in self.db.scalars(
                select(Entity).where(Entity.semantic_model_version_id == version_id)
            )
        }

    # ---- entity discovery -------------------------------------------------

    def _measures_for_metric(self, metric: Metric, seen: set[uuid.UUID]) -> list[Measure]:
        """Every measure a metric ultimately depends on, following metric edges."""
        if metric.id in seen:
            raise CompilationError(f"Metric {metric.name!r} depends on itself")
        seen.add(metric.id)

        found: list[Measure] = []
        for reference in metric.references:
            if reference.ref_measure_id:
                measure = self.measures.get(reference.ref_measure_id)
                if measure is None:
                    raise CompilationError(
                        f"Metric {metric.name!r} references a measure that no longer exists"
                    )
                found.append(measure)
            else:
                nested = next(
                    (m for m in self.metrics_by_name.values()
                     if m.id == reference.ref_metric_id),
                    None,
                )
                if nested is None:
                    raise CompilationError(
                        f"Metric {metric.name!r} references a metric that no longer exists"
                    )
                found.extend(self._measures_for_metric(nested, seen))
        return found

    def _required_entities(self) -> tuple[list[uuid.UUID], uuid.UUID]:
        measure_entities: list[uuid.UUID] = []
        for metric in self.metrics:
            for measure in self._measures_for_metric(metric, set()):
                measure_entities.append(measure.entity_id)
        if not measure_entities:
            raise CompilationError(
                "No metric in this query resolves to a measure, so there is nothing to compute"
            )

        required = list(measure_entities)
        required.extend(
            self.dimensions[dimension_id].entity_id for dimension_id in self.dimensions
        )
        # Entities carrying an active business rule have to be joined even when
        # the question never mentions them. Otherwise "exclude internal
        # customers" applies to `revenue by country` and silently does not apply
        # to `revenue` — the same question answered two different ways.
        required.extend(self._rule_entity_ids())
        # Root at the first measure's entity: the fact table the numbers live on.
        return required, measure_entities[0]

    def _rule_entity_ids(self) -> list[uuid.UUID]:
        return list(
            self.db.scalars(
                select(BusinessRule.entity_id).where(
                    BusinessRule.semantic_model_version_id == self.query.version_id,
                    BusinessRule.is_active.is_(True),
                )
            )
        )

    # ---- SQL fragments ----------------------------------------------------

    def _alias(self, entity_id: uuid.UUID) -> str:
        return self._alias_by_entity[entity_id]

    def _measure_sql(self, measure: Measure) -> str:
        alias = self._alias(measure.entity_id)
        inner = emit(parse_measure_expression(measure.expression), lambda c: f"{alias}.{c}")
        template = AGGREGATION_SQL.get(str(measure.aggregation))
        if template is None:
            raise CompilationError(f"Unknown aggregation {measure.aggregation!r}")
        return template.format(expr=inner)

    def _metric_sql(self, metric: Metric, seen: set[uuid.UUID]) -> str:
        if metric.id in seen:
            raise CompilationError(f"Metric {metric.name!r} depends on itself")
        seen = seen | {metric.id}

        def resolve(name: str) -> str:
            measure = self.measures_by_name.get(name)
            if measure is not None:
                return self._measure_sql(measure)
            nested = self.metrics_by_name.get(name)
            if nested is not None:
                return self._metric_sql(nested, seen)
            raise CompilationError(
                f"Metric {metric.name!r} refers to {name!r}, which this version does not define"
            )

        return emit(parse_metric_expression(metric.expression), resolve)

    def _dimension_sql(self, dimension: Dimension) -> str:
        return f"{self._alias(dimension.entity_id)}.{dimension.expression}"

    def _time_bucket_sql(self, dimension: Dimension, grain: TimeGranularity) -> str:
        return f"date_trunc('{DATE_TRUNC_GRAIN[grain]}', {self._dimension_sql(dimension)})"

    def _from_clause(self, plan: JoinPlan) -> list[str]:
        root = plan.root
        lines = [f"FROM   {root.source_schema}.{root.source_table} AS {self._alias(root.id)}"]
        for step in plan.steps:
            relationship = step.relationship
            if not relationship.join_keys:
                raise CompilationError(
                    f"Relationship {relationship.name!r} declares no join keys, so the "
                    "join condition is unknown"
                )
            conditions = []
            for key in relationship.join_keys:
                from_alias = self._alias(relationship.from_entity_id)
                to_alias = self._alias(relationship.to_entity_id)
                conditions.append(
                    f"{from_alias}.{key.from_column} = {to_alias}.{key.to_column}"
                )
            entity = step.entity
            join_type = "LEFT JOIN" if str(relationship.join_type) == "left" else "JOIN"
            lines.append(
                f"{join_type:<6} {entity.source_schema}.{entity.source_table} "
                f"AS {self._alias(entity.id)} ON {' AND '.join(conditions)}"
            )
        return lines

    def _where_clause(self, entity_ids: set[uuid.UUID]) -> list[str]:
        conditions: list[str] = []

        for query_filter in self.query.filters:
            dimension = self.dimensions[query_filter.dimension_id]
            column = self._dimension_sql(dimension)
            operator = query_filter.operator

            if operator is FilterOperatorPhrase.IN:
                binds = ", ".join(self._bind(v) for v in query_filter.values)
                conditions.append(f"{column} IN ({binds})")
            elif operator is FilterOperatorPhrase.NOT_IN:
                binds = ", ".join(self._bind(v) for v in query_filter.values)
                conditions.append(f"{column} NOT IN ({binds})")
            else:
                conditions.append(
                    f"{column} {COMPARISON_SQL[operator]} {self._bind(query_filter.values[0])}"
                )

        if self.query.time_range and self.query.time_dimension_id:
            dimension = self.dimensions[self.query.time_dimension_id]
            column = self._dimension_sql(dimension)
            conditions.append(f"{column} >= {self._bind(self.query.time_range.start)}")
            conditions.append(f"{column} < {self._bind(self.query.time_range.end)}")

        conditions.extend(self._business_rules(entity_ids))
        return conditions

    def _business_rules(self, entity_ids: set[uuid.UUID]) -> list[str]:
        """Injected for every entity the query touches, whether or not it was asked for."""
        rules = self.db.scalars(
            select(BusinessRule).where(
                BusinessRule.semantic_model_version_id == self.query.version_id,
                BusinessRule.entity_id.in_(entity_ids),
                BusinessRule.is_active.is_(True),
            ).order_by(BusinessRule.name)
        )
        conditions = []
        for rule in rules:
            column = f"{self._alias(rule.entity_id)}.{rule.column_name}"
            if rule.operator is FilterOperator.IS_NULL:
                conditions.append(f"{column} IS NULL")
            elif rule.operator is FilterOperator.IS_NOT_NULL:
                conditions.append(f"{column} IS NOT NULL")
            elif rule.operator in (FilterOperator.IN, FilterOperator.NOT_IN):
                values = rule.value if isinstance(rule.value, list) else [rule.value]
                binds = ", ".join(self._bind(v) for v in values)
                keyword = "IN" if rule.operator is FilterOperator.IN else "NOT IN"
                conditions.append(f"{column} {keyword} ({binds})")
            else:
                conditions.append(
                    f"{column} {RULE_SQL[rule.operator]} {self._bind(rule.value)}"
                )
        return conditions

    # ---- assembly ---------------------------------------------------------

    def compile(self) -> CompiledQuery:
        self._load()
        required, root_id = self._required_entities()

        try:
            plan = resolve_join_path(
                self.db, version_id=self.query.version_id,
                required_entity_ids=required, root_entity_id=root_id,
            )
        except JoinPathError as exc:
            raise CompilationError(str(exc)) from exc

        fanning = [step for step in plan.steps if step.fans_out]
        if fanning:
            names = ", ".join(sorted(step.entity.name for step in fanning))
            raise CompilationError(
                f"Answering this would join to {names} across a one-to-many "
                "relationship, which repeats every row on the other side and "
                "double-counts the totals. Model the measure on that entity "
                "instead, or ask for it separately."
            )

        for index, entity in enumerate(plan.entities):
            self._alias_by_entity[entity.id] = f"e{index}"

        select_parts: list[str] = []
        group_parts: list[str] = []
        columns: list[str] = []

        if self.query.time_dimension_id and self.query.grain:
            dimension = self.dimensions[self.query.time_dimension_id]
            bucket = self._time_bucket_sql(dimension, self.query.grain)
            select_parts.append(f"{bucket} AS {dimension.name}")
            group_parts.append(bucket)
            columns.append(dimension.name)

        for dimension_id in self.query.dimension_ids:
            dimension = self.dimensions[dimension_id]
            column = self._dimension_sql(dimension)
            select_parts.append(f"{column} AS {dimension.name}")
            group_parts.append(column)
            columns.append(dimension.name)

        for metric in self.metrics:
            select_parts.append(f"{self._metric_sql(metric, set())} AS {metric.name}")
            columns.append(metric.name)

        lines = ["SELECT " + ",\n       ".join(select_parts)]
        lines.extend(self._from_clause(plan))

        conditions = self._where_clause({entity.id for entity in plan.entities})
        if conditions:
            lines.append("WHERE  " + "\n  AND  ".join(conditions))
        if group_parts:
            lines.append("GROUP BY " + ", ".join(group_parts))
            lines.append("ORDER BY " + ", ".join(group_parts))
        lines.append(f"LIMIT  {int(self.query.limit)}")

        sql = "\n".join(lines)
        _assert_read_only(sql)
        return CompiledQuery(
            sql=sql,
            parameters=self.parameters,
            entities=[e.name for e in plan.entities],
            columns=columns,
        )


FORBIDDEN = (";", "--", "/*", "*/")


def _assert_read_only(sql: str) -> None:
    """A last look at what we assembled. Should never fire; cheap if it does."""
    if not sql.lstrip().upper().startswith("SELECT"):
        raise CompilationError("Compiled statement is not a SELECT")
    for token in FORBIDDEN:
        if token in sql:
            raise CompilationError(f"Compiled statement contains {token!r}")


def compile_query(db: Session, query: SemanticQuery) -> CompiledQuery:
    return _Compiler(db, query).compile()
