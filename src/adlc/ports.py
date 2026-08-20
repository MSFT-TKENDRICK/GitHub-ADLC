"""Frozen contracts for the ADLC framework.

Everything in this module is a **stable interface**. The spine ships a
credential-free default implementation for every Protocol; adapters registered
via ``pyproject.toml`` entry points are pure additions that can never block the
spine.

See ``docs/PLAN.md`` §4 for the authoritative contract description.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from adlc.config import Config

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

AdapterKind = Literal[
    "agents", "taskstore", "evals", "evidence",
    "flags", "telemetry", "gate", "daytwo", "export",
]

StageName = Literal[
    "intake", "qualify", "spec", "enrich", "graph", "build",
    "evidence", "eval", "gate", "report", "review", "export",
]

RunStatus = Literal[
    "draft", "specced", "built", "evaluated",
    "gated", "reported", "decided", "abandoned",
]

GateStatus = Literal["pass", "fail", "not_run"]
Severity = Literal["low", "medium", "high", "critical"]
TaskKind = Literal["implement", "test", "doc", "infra"]
DecisionOutcome = Literal["ship", "do_not_ship", "iterate", "rerun"]

#: Capsule budgets (plan section 4.3). Hard caps -- enforced, not advisory.
CAPSULE_MAX_TOTAL_BYTES = 65_536
CAPSULE_MAX_FILE_BYTES = 8_192
CAPSULE_MAX_FILES = 12

#: Paths an agent-authored patch may never touch (plan section 4.8).
PROTECTED_PATHS: tuple[str, ...] = (
    ".github/**", ".adlc/**", "schemas/**", "docs/decisions/**", "pyproject.toml",
)

#: Gate ids known to the framework. `required` is resolved from config+profile.
GATE_IDS: tuple[str, ...] = (
    "tests", "secrets_local", "deps_local", "evidence_completeness",
    "security", "code_quality", "evals", "governance",
    "adversarial_review", "evidence_review", "pre_registration", "spec_coverage",
)


# ---------------------------------------------------------------------------
# Data shapes  (mirrored 1:1 by JSON Schemas in ``schemas/``)
# ---------------------------------------------------------------------------


class StageResult(TypedDict, total=False):
    """One immutable stage execution.

    Written to ``runs/<run>/stages/<stage>.<attempt>.json``. Never mutated.
    A re-run appends ``attempt: n+1``.
    """

    stage: str
    attempt: int
    status: Literal["ok", "fail", "skipped"]
    startedAt: str
    endedAt: str
    outputs: list[str]
    digest: str
    message: str
    data: dict[str, Any]


class ArtifactRef(TypedDict, total=False):
    path: str
    kind: str
    mimeType: str
    sha256: str
    bytes: int


class GateResult(TypedDict, total=False):
    id: str
    required: bool
    status: GateStatus
    severity: Severity
    observed: dict[str, Any]
    expected: dict[str, Any]
    message: str
    evidence: list[str]


class Variant(TypedDict, total=False):
    key: str
    role: Literal["control", "treatment"]
    commit: str
    flagKeys: list[str]


class Decision(TypedDict, total=False):
    outcome: DecisionOutcome
    rationale: str
    decidedBy: str
    decidedAt: str
    reviewSha: str
    adr: str


class ContextRef(TypedDict, total=False):
    """A bounded reference into the repo. A cache, never the source of truth."""

    path: str
    blobSha: str
    lines: list[list[int]]
    symbols: list[str]
    excerpt: str


class ContextCapsule(TypedDict, total=False):
    refs: list[ContextRef]
    interfaces: str
    conventions: str
    commands: dict[str, str]
    doNotTouch: list[str]
    budget: dict[str, int]


class TaskNode(TypedDict, total=False):
    id: str
    title: str
    kind: TaskKind
    dependsOn: list[str]
    level: int
    writeSet: list[str]
    acceptance: list[str]
    rubricIds: list[str]
    adrRefs: list[str]
    context: ContextCapsule


class TaskGraph(TypedDict, total=False):
    runId: str
    baseSha: str
    specDigest: str
    nodes: list[TaskNode]


class TaskOutcome(TypedDict, total=False):
    status: Literal["ok", "fail", "skipped"]
    patchPath: str
    log: str
    tokensIn: int
    tokensOut: int
    cost: float


class RubricCriterion(TypedDict, total=False):
    id: str
    score: float
    weight: float
    passed: bool
    rationale: str
    evidence: list[str]
    #: True when the backend could not actually judge this criterion -- typically
    #: because it needs an LLM judge that was not configured, or the judge errored.
    #:
    #: This is the **structured** signal. It exists because the first version of
    #: this contract inferred the same thing by grepping ``rationale`` for a
    #: literal phrase, which silently coupled every eval backend, the eval stage
    #: and the autoresearch feedback loop to one another's prose: a backend that
    #: worded its message differently became invisible to the outer loop.
    #:
    #: A criterion that was not evaluated MUST set this to True and MUST score
    #: 0.0, so it can only ever pull a verdict down, never prop one up.
    requiresJudge: bool


class RubricScore(TypedDict, total=False):
    overall: float
    threshold: float
    passed: bool
    criteria: list[RubricCriterion]


class Rubric(TypedDict, total=False):
    id: str
    threshold: float
    criteria: list[dict[str, Any]]


class FlagResult(TypedDict, total=False):
    key: str
    value: Any
    variant: str
    reason: str


class Run(TypedDict, total=False):
    """The canonical ``adlc-run/v1`` document.

    Only :func:`adlc.reduce.reduce_run` may write this. Stages never do -- that
    is what makes parallel GitHub Actions jobs safe.
    """

    schemaVersion: str
    runId: str
    createdAt: str
    referencesRun: str | None
    repo: str
    baseSha: str
    headSha: str
    prNumber: int | None
    status: RunStatus
    profile: Literal["minimal", "full"]
    capabilities: dict[str, str]
    stages: list[StageResult]
    variants: list[Variant]
    gates: list[GateResult]
    artifacts: list[ArtifactRef]
    decision: Decision | None
    experimentRef: str | None


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class Adapter(Protocol):
    """Base contract. Every adapter is discoverable and self-describing."""

    name: str
    kind: AdapterKind

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        """Return ``(available, reason)``.

        ``reason`` is surfaced verbatim in ``capabilities.json`` and in any
        ``not_run`` gate, so it must be human-readable and specific.
        """
        ...


@runtime_checkable
class AgentRunner(Adapter, Protocol):
    async def run_task(
        self, node: TaskNode, worktree: Path, cfg: Config
    ) -> TaskOutcome:
        """Execute one task node inside an isolated worktree.

        MUST produce a patch anchored to the worktree's base SHA and MUST NOT
        write outside ``node['writeSet']``.
        """
        ...


@runtime_checkable
class TaskStore(Adapter, Protocol):
    def sync(self, graph: TaskGraph) -> dict[str, str]:
        """Push the graph to the store. Returns ``{node_id: external_id}``."""
        ...

    def update(self, node_id: str, status: str, note: str = "") -> None: ...


@runtime_checkable
class EvalRunner(Adapter, Protocol):
    def run(self, run: Run, rubric: Rubric) -> RubricScore: ...


@runtime_checkable
class EvidenceCollector(Adapter, Protocol):
    def collect(self, run: Run, variant: str, out: Path) -> list[ArtifactRef]: ...


@runtime_checkable
class FlagProvider(Adapter, Protocol):
    def materialize(self, run: Run) -> Path:
        """Write the flag definition file (e.g. ``flags.flagd.json``)."""
        ...

    def evaluate(self, key: str, ctx: dict[str, Any]) -> FlagResult: ...


@runtime_checkable
class GateRunner(Adapter, Protocol):
    id: str
    required_by_default: bool

    def evaluate(self, run: Run, cfg: Config) -> GateResult: ...


@runtime_checkable
class Telemetry(Adapter, Protocol):
    def emit(self, span: dict[str, Any]) -> None:
        """Emit an OTel-shaped span.

        Feature-flag spans MUST use current semconv names:
        ``feature_flag.key``, ``feature_flag.provider.name``,
        ``feature_flag.result.variant``, ``feature_flag.result.reason``,
        ``feature_flag.context.id``, ``feature_flag.set.id``.
        """
        ...


@runtime_checkable
class Exporter(Adapter, Protocol):
    def export(self, run: Run, out: Path) -> Path: ...
