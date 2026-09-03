"""The question-answering pipeline, as a typed graph.

    understand_intent → resolve_phrases → build_plan ─┬─ ok ─→ compile_sql ─┬→ END
                              ▲                       │                     │
                              └──── repair_intent ←───┘        execute_sql ─┘
                                                                     ↓
                                                              analyze_results → END

Execution is opt-in. Compiling without running is genuinely useful — previewing
the SQL a question would produce, or checking that a semantic model can answer
it at all — and it needs no warehouse connection. Natural-language answering is
Step 29 and is not wired yet; a placeholder node returning nothing would be a
worse lie than an honest end state.

The repair loop is the reason this is a graph rather than a function. When the
plan fails to build — an unknown metric, a filter value of the wrong type, an
ambiguous date column — the problems are handed back to the intent parser as
feedback and it gets another attempt. Bounded, because a model that cannot fix
its answer in two tries will not fix it in ten.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.search_index import SemanticSearchIndex
from app.services.analysis import ResultAnalysis, analyse
from app.services.compiler import CompilationError, CompiledQuery, compile_query
from app.services.embeddings import Embedder
from app.services.execution import ExecutionError, QueryResult, data_source_for, execute
from app.services.intent import Intent, IntentParser, IntentType, get_intent_parser
from app.services.query_ir import SemanticQuery, build
from app.services.resolution import ResolvedIntent, resolve

MAX_REPAIR_ATTEMPTS = 2

Status = Literal["pending", "ok", "failed"]


class PipelineState(TypedDict, total=False):
    question: str
    version_id: uuid.UUID
    today: date | None
    attempts: int
    intent: Intent | None
    resolved: ResolvedIntent | None
    query: SemanticQuery | None
    compiled: CompiledQuery | None
    result: QueryResult | None
    analysis: ResultAnalysis | None
    should_execute: bool
    problems: list[str]
    status: Status


@dataclass
class PipelineResult:
    question: str
    status: Status
    intent: Intent | None = None
    query: SemanticQuery | None = None
    compiled: CompiledQuery | None = None
    result: QueryResult | None = None
    analysis: ResultAnalysis | None = None
    problems: list[str] = field(default_factory=list)
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _vocabulary(db: Session, version_id: uuid.UUID) -> list[str]:
    """Names the model defines, offered to the parser as a hint about this
    organisation. Not a licence to substitute a name the user did not say."""
    return list(
        db.scalars(
            select(SemanticSearchIndex.name).where(
                SemanticSearchIndex.semantic_model_version_id == version_id
            )
        )
    )


def build_pipeline(
    db: Session,
    *,
    parser: IntentParser | None = None,
    embedder: Embedder | None = None,
    max_attempts: int = MAX_REPAIR_ATTEMPTS,
):
    """Compile the graph. `db` is captured, so one graph serves one session."""
    parser = parser or get_intent_parser()

    def understand_intent(state: PipelineState) -> PipelineState:
        intent = parser.parse(
            state["question"], vocabulary=_vocabulary(db, state["version_id"])
        )
        return {"intent": intent, "attempts": 1}

    def repair_intent(state: PipelineState) -> PipelineState:
        intent = parser.parse(
            state["question"],
            vocabulary=_vocabulary(db, state["version_id"]),
            feedback=state["problems"],
        )
        return {"intent": intent, "attempts": state["attempts"] + 1, "problems": []}

    def resolve_phrases(state: PipelineState) -> PipelineState:
        resolved = resolve(
            db, state["intent"], version_id=state["version_id"], embedder=embedder
        )
        return {"resolved": resolved}

    def build_plan(state: PipelineState) -> PipelineState:
        result = build(
            db,
            state["resolved"],
            version_id=state["version_id"],
            today=state.get("today"),
        )
        return {"query": result.query, "problems": result.problems}

    def compile_sql(state: PipelineState) -> PipelineState:
        try:
            compiled = compile_query(db, state["query"])
        except CompilationError as exc:
            # Not repairable by rephrasing: the model itself is incomplete.
            return {"status": "failed", "problems": [str(exc)]}
        return {"compiled": compiled, "status": "ok"}

    def execute_sql(state: PipelineState) -> PipelineState:
        try:
            source = data_source_for(db, state["version_id"])
            result = execute(source, state["compiled"])
        except ExecutionError as exc:
            return {"status": "failed", "problems": [str(exc)]}
        return {"result": result}

    def analyze_results(state: PipelineState) -> PipelineState:
        return {"analysis": analyse(state["result"])}

    def after_compile(state: PipelineState) -> str:
        if state["status"] != "ok" or not state.get("should_execute"):
            return "finish"
        return "execute_sql"

    def after_execute(state: PipelineState) -> str:
        return "finish" if state["status"] == "failed" else "analyze_results"

    def after_plan(state: PipelineState) -> str:
        if not state["problems"]:
            return "compile_sql"
        if state["intent"].intent_type is IntentType.UNSUPPORTED:
            return "give_up"  # rephrasing cannot help a question we cannot answer
        if state["attempts"] >= max_attempts:
            return "give_up"
        return "repair_intent"

    def give_up(state: PipelineState) -> PipelineState:
        return {"status": "failed"}

    graph = StateGraph(PipelineState)
    graph.add_node("understand_intent", understand_intent)
    graph.add_node("repair_intent", repair_intent)
    graph.add_node("resolve_phrases", resolve_phrases)
    graph.add_node("build_plan", build_plan)
    graph.add_node("compile_sql", compile_sql)
    graph.add_node("execute_sql", execute_sql)
    graph.add_node("analyze_results", analyze_results)
    graph.add_node("give_up", give_up)

    graph.add_edge(START, "understand_intent")
    graph.add_edge("understand_intent", "resolve_phrases")
    graph.add_edge("repair_intent", "resolve_phrases")
    graph.add_edge("resolve_phrases", "build_plan")
    graph.add_conditional_edges(
        "build_plan",
        after_plan,
        {"compile_sql": "compile_sql", "repair_intent": "repair_intent", "give_up": "give_up"},
    )
    graph.add_conditional_edges(
        "compile_sql", after_compile, {"execute_sql": "execute_sql", "finish": END}
    )
    graph.add_conditional_edges(
        "execute_sql", after_execute,
        {"analyze_results": "analyze_results", "finish": END},
    )
    graph.add_edge("analyze_results", END)
    graph.add_edge("give_up", END)
    return graph.compile()


def answer(
    db: Session,
    question: str,
    *,
    version_id: uuid.UUID,
    today: date | None = None,
    parser: IntentParser | None = None,
    embedder: Embedder | None = None,
    max_attempts: int = MAX_REPAIR_ATTEMPTS,
    execute_query: bool = False,
) -> PipelineResult:
    """Run a question through the pipeline.

    `execute_query=False` stops at compiled SQL and needs no warehouse.
    """
    pipeline = build_pipeline(
        db, parser=parser, embedder=embedder, max_attempts=max_attempts
    )
    final: PipelineState = pipeline.invoke(
        {
            "question": question,
            "version_id": version_id,
            "today": today,
            "attempts": 0,
            "problems": [],
            "should_execute": execute_query,
            "status": "pending",
        }
    )
    return PipelineResult(
        question=question,
        status=final.get("status", "failed"),
        intent=final.get("intent"),
        query=final.get("query"),
        compiled=final.get("compiled"),
        result=final.get("result"),
        analysis=final.get("analysis"),
        problems=final.get("problems", []),
        attempts=final.get("attempts", 0),
    )
