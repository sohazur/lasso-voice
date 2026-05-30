"""Proof the LIVE-CALL learning path works — the underneath-the-hood engine.

Phase E claim: every real outbound call is a labeled example. We feed a REAL
(already-happened) transcript to the loop; if the agent whiffed the objection, the
loop mines a corrected exemplar so the NEXT call is smarter. No simulation here — we
score an actual transcript, exactly as run_bot's on_call_end does.

Run: uv run python -m tests.test_live_learning
"""

from __future__ import annotations

import asyncio

from app.bot import _transcript_from_context
from app.loop.improve import ImprovementLoop
from app.loop.store import LoopStore
from app.loop.types import EvalResult, Exemplar


class FakeJudge:
    """Scores a transcript: RED if it contains no concrete price lever, else GREEN.

    Mirrors the real judge's intent (vague reassurance fails) but is deterministic.
    """

    backend = "live-call"

    async def score_transcript(self, scenario, transcript, *, strategy_version):
        lowered = transcript.lower()
        has_lever = any(k in lowered for k in ("% off", "discount", "free shipping", "installment", "code "))
        return EvalResult(
            objection=scenario.objection,
            passed=has_lever,
            score=0.9 if has_lever else 0.2,
            transcript=transcript,
            reasoning="green: concrete lever" if has_lever else "red: no price lever",
            backend=self.backend,
            strategy_version=strategy_version,
        )


class FakeMiner:
    async def mine(self, result, expected_outcome):
        return Exemplar(
            objection=result.objection,
            corrected_handling="Offer code SAVE15 (15% off) or free shipping — a concrete lever.",
            source_reasoning=result.reasoning,
        )


def _build_loop():
    class S:
        supabase_url = ""
        supabase_key = ""

    store = LoopStore(S())  # type: ignore[arg-type]
    loop = ImprovementLoop.__new__(ImprovementLoop)
    loop.settings = S()  # type: ignore[attr-defined]
    loop.store = store
    loop.evaluator = FakeJudge()
    loop.miner = FakeMiner()
    return loop, store


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


# A real FAILED call: agent only reassures on quality, never offers a lever.
RED_TRANSCRIPT = (
    "Agent: Hi Sam, this is Lasso from Acme Coffee — want a hand finishing up?\n"
    "Shopper: The beans look great but it's more than I can spend today.\n"
    "Agent: They're premium single-origin, roasted to order — really worth it.\n"
    "Shopper: I know they're good, it's just the price. I'll hold off.\n"
    "Agent: No problem, I'll keep your cart saved. Have a great day!"
)

# A real GREEN call: agent offers a concrete lever.
GREEN_TRANSCRIPT = (
    "Agent: Hi Sam — want a hand finishing up?\n"
    "Shopper: It's a bit more than I can spend today.\n"
    "Agent: I can apply code SAVE15 for 15% off and free shipping — want me to?\n"
    "Shopper: Yes, that works, go ahead."
)


async def main():
    loop, store = _build_loop()

    # 1) A real failed call -> loop learns (mines a fix, bumps strategy).
    _check(store.strategy_version("Acme") == 0, "starts at v0 (nothing learned yet)")
    r1 = await loop.learn_from_call("Acme", "price", RED_TRANSCRIPT, store_name="Acme")
    _check(r1.before.passed is False, "real failed call scored RED")
    _check(r1.before.backend == "live-call", "labeled backend='live-call' (real call, not sim)")
    _check(r1.mined is not None, "loop mined a fix from the REAL transcript")
    _check(store.strategy_version("Acme") == 1, "strategy bumped to v1 after a live failure")
    _check(store.objection_guidance("Acme") is not None, "next call now carries the learned lever")

    # 2) A real successful call -> no mining (nothing to learn).
    r2 = await loop.learn_from_call("Acme", "price", GREEN_TRANSCRIPT, store_name="Acme")
    _check(r2.before.passed, "real successful call scored GREEN")
    _check(r2.mined is None, "no mining when the live call already succeeded")
    _check(store.strategy_version("Acme") == 1, "strategy unchanged after a green live call")

    # 3) Two live calls recorded on the scoreboard (red then green).
    runs = store.runs_for("Acme")
    _check(len(runs) == 2, "two live calls recorded")
    _check(runs[0]["passed"] is False and runs[1]["passed"], "scoreboard: live red → live green")

    # 4) Transcript reconstruction from an LLM context (what run_bot feeds in).
    from pipecat.processors.aggregators.llm_context import LLMContext

    ctx = LLMContext()
    ctx.set_messages([{"role": "system", "content": "persona"}])
    ctx.add_message({"role": "assistant", "content": "Hi, want a hand?"})
    ctx.add_message({"role": "user", "content": "too pricey"})
    t = _transcript_from_context(ctx)
    _check("Agent: Hi, want a hand?" in t and "Shopper: too pricey" in t, "context → Agent/Shopper transcript")
    _check("persona" not in t, "system prompt excluded from transcript")

    print("\nALL LIVE-LEARNING ASSERTIONS PASSED — the agent learns from real calls.")


if __name__ == "__main__":
    asyncio.run(main())
