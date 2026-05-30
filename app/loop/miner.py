"""Failure miner: turn a RED eval into a corrected objection-handling exemplar.

This is the "no human touches it" step — the agent's own failure transcript is fed
back to an LLM that writes the ideal handling, which then conditions future prompts.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from ..config import Settings
from .types import EvalResult, Exemplar

_MINER_SYSTEM = """A cart-recovery phone agent FAILED to handle a "{objection}" objection.

Pass criterion it missed:
{expected}

Below is the failed call transcript. Write the CORRECTED handling: a short, concrete,
phone-friendly way the agent SHOULD respond to this objection next time. Be specific
(name a lever or a concrete value point), not generic. 2-4 sentences, in the agent's
voice, ready to drop into its instructions. Output ONLY that guidance."""


class FailureMiner:
    def __init__(self, settings: Settings):
        if settings.use_nemotron:
            self._client = AsyncOpenAI(
                base_url=settings.nemotron_base_url, api_key=settings.nemotron_api_key
            )
            self._model = settings.nemotron_model
        else:
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
            self._model = "gpt-4.1"

    async def mine(self, result: EvalResult, expected_outcome: str) -> Exemplar:
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "system",
                    "content": _MINER_SYSTEM.format(
                        objection=result.objection, expected=expected_outcome
                    ),
                },
                {"role": "user", "content": f"Failed transcript:\n{result.transcript}"},
            ],
            temperature=0.4,
        )
        corrected = (resp.choices[0].message.content or "").strip()
        return Exemplar(
            objection=result.objection,
            corrected_handling=corrected,
            source_reasoning=result.reasoning,
        )
