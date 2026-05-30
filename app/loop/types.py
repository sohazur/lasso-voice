"""Shared types for the self-improvement loop.

Kept backend-agnostic on purpose: the LocalJudge and Cekura evaluators both
produce the same EvalResult, so the miner/store/loop never know which ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scenario:
    """A simulated abandoning-shopper scenario.

    Maps to a Cekura `evaluator` (persona + expected outcome). For the hackathon
    the locked demo scenario is `price`.
    """

    objection: str  # "price" | "shipping" | "browsing"
    persona: str  # how the simulated shopper behaves
    expected_outcome: str  # what "handled well" means — the pass criterion

    @property
    def key(self) -> str:
        return self.objection


@dataclass(frozen=True)
class EvalResult:
    """Outcome of one evaluation run against the agent's current prompt."""

    objection: str
    passed: bool  # red/green
    score: float  # 0..1
    transcript: str  # the simulated agent<->shopper exchange
    reasoning: str = ""  # judge's rationale (why it passed/failed)
    backend: str = "local-judge"  # "local-judge" | "cekura"
    strategy_version: int = 0  # which exemplar-set version was live


@dataclass
class Exemplar:
    """A corrected objection-handling response, mined from a failed run.

    Appended to the merchant's strategy slot to condition the agent's prompt.
    """

    objection: str
    corrected_handling: str
    source_reasoning: str = ""


@dataclass
class LoopReport:
    """The red->green story for one /improve run — drives the demo scoreboard."""

    objection: str
    before: EvalResult
    after: EvalResult | None = None
    mined: Exemplar | None = None
    rounds: list[EvalResult] = field(default_factory=list)

    @property
    def flipped_green(self) -> bool:
        return self.before.passed is False and bool(self.after and self.after.passed)
