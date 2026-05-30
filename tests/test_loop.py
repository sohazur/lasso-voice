"""Proof the self-improvement mechanism is real — no API keys, no network.

We inject a fake evaluator whose verdict DEPENDS on whether an exemplar has been
mined yet: RED with no exemplar, GREEN once one exists. This is exactly the demo's
claim — the agent fails, the loop folds in a fix, the agent passes — verified as a
deterministic property of the orchestrator, not of any model.

Run: uv run python -m tests.test_loop
"""

from __future__ import annotations

import asyncio

from app.loop import scenarios
from app.loop.improve import ImprovementLoop
from app.loop.store import LoopStore
from app.loop.types import EvalResult, Exemplar


class FakeEvaluator:
    """RED until the merchant has a mined exemplar for the objection, then GREEN."""

    backend = "fake"

    def __init__(self, store: LoopStore):
        self._store = store
        self.calls = 0

    async def run(self, scenario, agent_system_prompt, *, strategy_version):
        self.calls += 1
        has_fix = any(
            e.objection == scenario.objection
            # the prompt must actually carry the learned guidance
            and e.corrected_handling in agent_system_prompt
            for e in self._store.exemplars_for("acme")
        )
        return EvalResult(
            objection=scenario.objection,
            passed=has_fix,
            score=0.9 if has_fix else 0.2,
            transcript="Agent: ...\nShopper: too pricey\nAgent: ...",
            reasoning="green: concrete lever offered" if has_fix else "red: only vague reassurance",
            backend=self.backend,
            strategy_version=strategy_version,
        )


class FakeMiner:
    async def mine(self, result, expected_outcome):
        return Exemplar(
            objection=result.objection,
            corrected_handling="Offer LASSO15 (15% off) and name the warranty — concrete lever.",
            source_reasoning=result.reasoning,
        )


def _build_loop() -> tuple[ImprovementLoop, LoopStore]:
    # Settings only needed for store init flags; in-memory path uses none of the keys.
    class S:
        supabase_url = ""
        supabase_key = ""

    store = LoopStore(S())  # type: ignore[arg-type]
    loop = ImprovementLoop.__new__(ImprovementLoop)  # bypass __init__ (no LLM creds)
    loop.settings = S()  # type: ignore[attr-defined]
    loop.store = store
    loop.evaluator = FakeEvaluator(store)
    loop.miner = FakeMiner()
    return loop, store


def _check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


async def main():
    print("scenarios available:", list(scenarios.ALL))
    loop, store = _build_loop()

    # v0: no exemplars → prompt has no learned guidance → RED expected.
    _check(store.strategy_version("acme") == 0, "starts at strategy v0")
    _check(store.objection_guidance("acme") is None, "v0 prompt has no objection guidance")

    report = await loop.improve("acme", "price", store_name="Acme")

    _check(report.before.passed is False, "9am: agent FUMBLES price (red)")
    _check(report.mined is not None, "failure mined into a corrected exemplar")
    _check(store.strategy_version("acme") == 1, "strategy bumped to v1 after mining")
    _check(store.objection_guidance("acme") is not None, "prompt now carries learned guidance")
    _check(report.after is not None and report.after.passed, "2pm: same objection HANDLED (green)")
    _check(report.flipped_green, "report.flipped_green == True (the demo claim)")

    sb = store.runs_for("acme")
    _check(len(sb) == 2, "two runs recorded (before + after)")
    _check(sb[0]["passed"] is False and sb[1]["passed"] is True, "scoreboard shows red→green")

    # Idempotence: improving again should already be green, no new exemplar.
    report2 = await loop.improve("acme", "price", store_name="Acme")
    _check(report2.before.passed, "re-run: already green (learning persisted)")
    _check(store.strategy_version("acme") == 1, "no extra mining when already green")

    print("\nALL LOOP ASSERTIONS PASSED — red→green mechanism verified offline.")


if __name__ == "__main__":
    asyncio.run(main())
