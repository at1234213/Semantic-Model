"""Understanding what a question is asking for.

The first step in the pipeline that involves a language model, and the shape of
its output is the whole point: the model returns **phrases as the user said
them**, never identifiers it chose. "sales in Texas" comes back as
`metric_phrases=["sales"]`, not as the UUID of a metric. Resolving a phrase to a
semantic object is retrieval's job (Step 23) and validation's job (Step 26),
both deterministic.

That keeps the failure mode benign. A model that misreads the question produces
a phrase that resolves to nothing, which is a clear error. A model allowed to
pick identifiers could produce one that resolves to the *wrong* thing, silently.

Two providers behind one protocol, mirroring the embedding layer:

    scripted   deterministic, registered question -> intent; no network
    claude     Claude via the Anthropic SDK, imported lazily
"""

import enum
from functools import lru_cache
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.core.config import get_settings


class IntentType(enum.StrEnum):
    AGGREGATE = "aggregate"        # "what was revenue last quarter"
    BREAKDOWN = "breakdown"        # "revenue by country"
    TREND = "trend"                # "revenue over the last year"
    COMPARISON = "comparison"      # "this quarter versus last"
    DEFINITION = "definition"      # "what do we mean by active customer"
    UNSUPPORTED = "unsupported"    # not answerable from a semantic model


class FilterOperatorPhrase(enum.StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    NOT_IN = "not_in"


class FilterPhrase(BaseModel):
    """A restriction, still in the user's words."""

    field_phrase: str = Field(description="What is being filtered, as the user said it")
    operator: FilterOperatorPhrase
    value_phrases: list[str] = Field(
        default_factory=list, description="The value(s), as the user said them"
    )


class TimeRangePhrase(BaseModel):
    phrase: str = Field(description="The time expression verbatim, e.g. 'last quarter'")
    grain: str | None = Field(
        default=None, description="Bucket size if implied: day, week, month, quarter, year"
    )


class Intent(BaseModel):
    """A question, decomposed. Contains no identifiers and no SQL."""

    intent_type: IntentType
    metric_phrases: list[str] = Field(default_factory=list)
    dimension_phrases: list[str] = Field(default_factory=list)
    filters: list[FilterPhrase] = Field(default_factory=list)
    time_range: TimeRangePhrase | None = None
    note: str | None = Field(
        default=None, description="Why the question is unsupported, if it is"
    )

    @property
    def is_answerable(self) -> bool:
        if self.intent_type is IntentType.UNSUPPORTED:
            return False
        if self.intent_type is IntentType.DEFINITION:
            return True
        return bool(self.metric_phrases)


@runtime_checkable
class IntentParser(Protocol):
    name: str

    def parse(self, question: str, *, vocabulary: list[str] | None = None) -> Intent: ...


class ScriptedIntentParser:
    """Returns registered intents. Deterministic, offline, and used by the tests.

    Anything unregistered comes back UNSUPPORTED rather than guessed, so a test
    that forgets to register a question fails loudly instead of drifting.
    """

    name = "scripted"

    def __init__(self) -> None:
        self._responses: dict[str, Intent] = {}

    def register(self, question: str, intent: Intent) -> None:
        self._responses[question.strip().lower()] = intent

    def parse(self, question: str, *, vocabulary: list[str] | None = None) -> Intent:
        return self._responses.get(
            question.strip().lower(),
            Intent(
                intent_type=IntentType.UNSUPPORTED,
                note="No scripted response registered for this question.",
            ),
        )


SYSTEM_PROMPT = """\
You decompose analytics questions into a structured intent for a semantic layer.

Rules:
- Return the user's own words. Do not translate a phrase into a canonical name, \
even if you believe you know the right one. Resolving phrases to defined metrics \
and dimensions happens later, deterministically.
- Never invent a metric or dimension that the question does not mention.
- Do not write SQL, table names, or column names.
- A question you cannot express as metrics, dimensions, filters and a time range \
is `unsupported`. Say why in `note`. Guessing is worse than declining.
- `definition` is for questions asking what a term means, not for questions \
asking for a number.

If a vocabulary of known names is supplied, it is a hint about what this \
organisation measures. It may help you tell a metric phrase from a dimension \
phrase. It does not license you to substitute a name the user did not say.\
"""


class ClaudeIntentParser:
    """Claude via the Anthropic SDK. Imported lazily so the package is optional."""

    def __init__(self, model: str, max_tokens: int = 4000) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "INTENT_PROVIDER is 'claude' but the anthropic package is not installed. "
                "Add anthropic to requirements.txt and rebuild, or set "
                "INTENT_PROVIDER=scripted."
            ) from exc

        self.name = model
        self._model = model
        self._max_tokens = max_tokens
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()

    def parse(  # pragma: no cover - requires network and credentials
        self, question: str, *, vocabulary: list[str] | None = None
    ) -> Intent:
        content = question if not vocabulary else (
            f"Known names in this organisation's semantic model: "
            f"{', '.join(sorted(vocabulary))}\n\nQuestion: {question}"
        )

        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=self._max_tokens,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_format=Intent,
            )
        except self._anthropic.RateLimitError as exc:
            raise IntentUnavailable("Rate limited by the Anthropic API") from exc
        except self._anthropic.APIConnectionError as exc:
            raise IntentUnavailable("Could not reach the Anthropic API") from exc
        except self._anthropic.APIStatusError as exc:
            raise IntentUnavailable(f"Anthropic API error: {exc.message}") from exc

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            return Intent(
                intent_type=IntentType.UNSUPPORTED,
                note=f"The model declined to answer ({category}).",
            )
        return response.parsed_output


class IntentUnavailable(RuntimeError):
    """The parser could not be reached. Distinct from a question it declined."""


@lru_cache
def get_intent_parser() -> IntentParser:
    settings = get_settings()
    provider = settings.intent_provider

    if provider == "scripted":
        return ScriptedIntentParser()
    if provider == "claude":
        return ClaudeIntentParser(settings.intent_model)
    raise ValueError(
        f"Unknown INTENT_PROVIDER {provider!r}. Expected 'scripted' or 'claude'."
    )
