"""Evaluator backends.

Two implementations behind one `Evaluator` protocol so the loop is identical
regardless of which runs:

  LocalJudgeEvaluator  — runs TODAY with no Cekura key. An LLM plays the abandoning
                          shopper against the agent's current system prompt, then an
                          LLM-judge scores pass/fail + 0..1. Honest stand-in clearly
                          labelled backend="local-judge".
  CekuraEvaluator      — same interface, hits api.cekura.ai (evaluators -> runs ->
                          results). Drops in when CEKURA_API_KEY is set; no loop changes.

Pick via `make_evaluator(settings)`.
"""

from __future__ import annotations

import json
from typing import Protocol

import httpx
from loguru import logger
from openai import AsyncOpenAI

from ..config import Settings
from .types import EvalResult, Scenario


class Evaluator(Protocol):
    backend: str

    async def run(self, scenario: Scenario, agent_system_prompt: str, *, strategy_version: int) -> EvalResult: ...


# ── Local LLM-judge backend (works offline today) ────────────────────────────

_SIM_SYSTEM = """You simulate a phone call between a CART-RECOVERY AGENT and a SHOPPER.

The AGENT's behaviour is fully defined by this system prompt:
<agent_prompt>
{agent_prompt}
</agent_prompt>

The SHOPPER persona:
<shopper>
{persona}
</shopper>

Produce a realistic 6-10 turn phone conversation. The agent opens. Alternate turns,
labelled "Agent:" and "Shopper:". The shopper behaves EXACTLY as the persona says —
in particular, generic reassurance must NOT satisfy them.

CRITICAL — the AGENT may only use offers, discounts, promo codes, payment options,
shipping terms, or facts that are EXPLICITLY written in <agent_prompt> above. It must
NOT invent any lever that isn't there (no made-up discount %, promo code, payment plan,
bundle, or free shipping). If <agent_prompt> gives the agent no concrete lever for the
objection, the agent can ONLY empathize or reassure — it cannot conjure a deal.

End when it's natural. Output ONLY the transcript."""

_JUDGE_SYSTEM = """You are a strict evaluator of a cart-recovery agent's objection handling.

Objection under test: {objection}
Pass criterion (expected outcome):
{expected}

Given the transcript, decide if the AGENT met the criterion. Be strict: vague or
generic reassurance with no concrete value/lever is a FAIL. Reply with JSON only:
{{"passed": true|false, "score": 0.0-1.0, "reasoning": "one sentence"}}"""


class LocalJudgeEvaluator:
    backend = "local-judge"

    def __init__(self, settings: Settings):
        # Reuse whichever LLM creds exist (Nemotron endpoint or OpenAI).
        if settings.use_nemotron:
            self._client = AsyncOpenAI(
                base_url=settings.nemotron_base_url,
                api_key=settings.nemotron_api_key or "no-key",
            )
            self._model = settings.nemotron_model
        else:
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
            self._model = "gpt-4.1"

    async def _chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.7,
            **kwargs,
        )
        return resp.choices[0].message.content or ""

    async def run(
        self, scenario: Scenario, agent_system_prompt: str, *, strategy_version: int
    ) -> EvalResult:
        transcript = await self._chat(
            _SIM_SYSTEM.format(agent_prompt=agent_system_prompt, persona=scenario.persona),
            "Begin the call now.",
        )
        verdict_raw = await self._chat(
            _JUDGE_SYSTEM.format(objection=scenario.objection, expected=scenario.expected_outcome),
            f"Transcript:\n{transcript}",
            json_mode=True,
        )
        try:
            v = json.loads(verdict_raw)
            passed = bool(v.get("passed", False))
            score = float(v.get("score", 0.0))
            reasoning = str(v.get("reasoning", ""))
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"judge returned non-JSON: {verdict_raw[:120]!r}")
            passed, score, reasoning = False, 0.0, "judge parse error"

        return EvalResult(
            objection=scenario.objection,
            passed=passed,
            score=score,
            transcript=transcript,
            reasoning=reasoning,
            backend=self.backend,
            strategy_version=strategy_version,
        )


# ── Cekura backend (drops in when a key is present) ───────────────────────────


class CekuraEvaluator:
    """Targets api.cekura.ai: ensure evaluator -> trigger run -> poll results.

    The hackathon judging axis. Tool/endpoint shapes follow the documented model
    (evaluators carry persona + expected outcome; results are boolean/numeric).
    Network specifics are deliberately defensive so a schema drift degrades to a
    clear error rather than a crash — the loop can always fall back to local-judge.
    """

    backend = "cekura"
    BASE = "https://api.cekura.ai"

    def __init__(self, settings: Settings):
        if not settings.cekura_api_key:
            raise RuntimeError("CekuraEvaluator requires CEKURA_API_KEY")
        self._key = settings.cekura_api_key
        self._headers = {"X-CEKURA-API-KEY": self._key, "Content-Type": "application/json"}

    async def run(
        self, scenario: Scenario, agent_system_prompt: str, *, strategy_version: int
    ) -> EvalResult:
        async with httpx.AsyncClient(timeout=60, headers=self._headers) as http:
            ev = await http.post(
                f"{self.BASE}/evaluators",
                json={
                    "name": f"lasso-{scenario.objection}",
                    "personality": scenario.persona,
                    "expected_outcome": scenario.expected_outcome,
                },
            )
            ev.raise_for_status()
            evaluator_id = ev.json().get("id")

            run = await http.post(
                f"{self.BASE}/runs",
                json={"evaluator_id": evaluator_id, "agent_prompt": agent_system_prompt},
            )
            run.raise_for_status()
            run_id = run.json().get("id")

            res = await http.get(f"{self.BASE}/results", params={"run_id": run_id})
            res.raise_for_status()
            data = res.json()

        score = float(data.get("score", 0.0))
        passed = bool(data.get("passed", score >= 0.6))
        return EvalResult(
            objection=scenario.objection,
            passed=passed,
            score=score,
            transcript=str(data.get("transcript", "")),
            reasoning=str(data.get("reasoning", "")),
            backend=self.backend,
            strategy_version=strategy_version,
        )


def make_evaluator(settings: Settings) -> Evaluator:
    """Prefer Cekura (prize axis); fall back to the local judge so the loop always runs."""
    if settings.cekura_api_key:
        logger.info("evaluator: Cekura (api.cekura.ai)")
        return CekuraEvaluator(settings)
    logger.info("evaluator: local LLM-judge (no CEKURA_API_KEY — labelled honestly)")
    return LocalJudgeEvaluator(settings)
