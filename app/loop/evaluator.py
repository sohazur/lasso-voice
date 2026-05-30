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
in particular, generic reassurance must NOT satisfy them. End when it's natural.
Output ONLY the transcript."""

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
    """Real Cekura scoring via its MCP server (api.cekura.ai/mcp — the prize axis).

    Flow per run:
      1. We generate the simulated price-objection conversation locally (Nemotron
         plays shopper + agent) — same as the local judge.
      2. We send that transcript to Cekura's `observe_create` tool, which runs
         Cekura's REAL metrics on it, and read the metric score back.

    This is genuinely "scored by Cekura": the verdict comes from Cekura's metrics,
    not ours. Requires a project-scoped key + an agent/assistant in the dashboard.
    Any auth/permission/protocol issue raises so `make_evaluator` falls back to the
    local judge — we NEVER label a local verdict as Cekura.
    """

    backend = "cekura"

    def __init__(self, settings: Settings):
        if not settings.cekura_api_key:
            raise RuntimeError("CekuraEvaluator requires CEKURA_API_KEY")
        self._settings = settings
        self._key = settings.cekura_api_key
        # Reuse the local judge purely to GENERATE the transcript Cekura will score.
        self._sim = LocalJudgeEvaluator(settings)

    async def run(
        self, scenario: Scenario, agent_system_prompt: str, *, strategy_version: int
    ) -> EvalResult:
        from .cekura_mcp import CekuraMCP

        # 1. Generate the conversation locally (Nemotron drives both sides).
        local = await self._sim.run(scenario, agent_system_prompt, strategy_version=strategy_version)
        transcript_json = _transcript_to_turns(local.transcript)

        # 2. Score it with Cekura's real metrics via MCP.
        async with CekuraMCP(self._key) as mcp:
            result = await mcp.call_tool(
                "observe_create",
                {
                    "call_id": f"lasso-{scenario.objection}-v{strategy_version}",
                    "assistant_id": self._settings.cekura_assistant_id or "lasso-cart-agent",
                    "transcript_type": "cekura",
                    "transcript_json": transcript_json,
                },
            )
        score, passed, reasoning = _score_from_observe(result)
        return EvalResult(
            objection=scenario.objection,
            passed=passed,
            score=score,
            transcript=local.transcript,
            reasoning=reasoning or "scored by Cekura metrics",
            backend=self.backend,
            strategy_version=strategy_version,
        )


def _transcript_to_turns(transcript: str) -> list[dict[str, str]]:
    """Turn an 'Agent:/Shopper:' transcript into Cekura's transcript_json shape."""
    turns: list[dict[str, str]] = []
    for line in transcript.splitlines():
        line = line.strip()
        if line.lower().startswith("agent:"):
            turns.append({"role": "agent", "content": line.split(":", 1)[1].strip()})
        elif line.lower().startswith("shopper:") or line.lower().startswith("customer:"):
            turns.append({"role": "user", "content": line.split(":", 1)[1].strip()})
    return turns or [{"role": "agent", "content": transcript[:500]}]


def _score_from_observe(result) -> tuple[float, bool, str]:
    """Extract a 0..1 score + pass/fail from Cekura's observe/metrics response."""
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected Cekura result: {str(result)[:160]}")
    metrics = result.get("metrics") or []
    if metrics:
        scores = [float(m.get("score", 0)) for m in metrics if m.get("score") is not None]
        score = sum(scores) / len(scores) if scores else 0.0
    else:
        score = float(result.get("success_rate", result.get("score", 0.0)) or 0.0)
    passed = bool(result.get("success", score >= 0.6))
    reasoning = "; ".join(
        f"{m.get('name')}={m.get('value', m.get('score'))}" for m in metrics[:3]
    )
    return score, passed, reasoning


class ResilientEvaluator:
    """Tries Cekura; on the FIRST runtime failure (auth/permission/protocol) it
    permanently demotes to the local judge for the session and logs why. The loop
    never crashes on a key-scope issue, and each result is labelled by the backend
    that actually produced it — so a fallback verdict is never mislabelled Cekura."""

    def __init__(self, primary: Evaluator, fallback: Evaluator):
        self._primary = primary
        self._fallback = fallback
        self._demoted = False

    @property
    def backend(self) -> str:
        return self._fallback.backend if self._demoted else self._primary.backend

    async def run(self, scenario, agent_system_prompt, *, strategy_version):
        if not self._demoted:
            try:
                return await self._primary.run(
                    scenario, agent_system_prompt, strategy_version=strategy_version
                )
            except Exception as e:
                logger.warning(
                    f"Cekura unavailable ({type(e).__name__}: {str(e)[:120]}); "
                    "demoting to local judge for this session"
                )
                self._demoted = True
        return await self._fallback.run(
            scenario, agent_system_prompt, strategy_version=strategy_version
        )


def make_evaluator(settings: Settings) -> Evaluator:
    """Prefer real Cekura (prize axis); fall back to the local judge so the loop
    always runs — and is always labelled honestly by its `backend`."""
    local = LocalJudgeEvaluator(settings)
    if settings.cekura_api_key:
        try:
            cekura = CekuraEvaluator(settings)
            logger.info("evaluator: Cekura MCP (api.cekura.ai/mcp) with local-judge fallback")
            return ResilientEvaluator(cekura, local)
        except Exception:
            logger.exception("Cekura init failed; using local judge")
    logger.info("evaluator: local LLM-judge (Nemotron) — labelled honestly")
    return local
