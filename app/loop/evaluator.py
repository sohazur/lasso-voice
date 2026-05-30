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

    async def _judge(self, scenario: Scenario, transcript: str) -> tuple[bool, float, str]:
        """Score a transcript against the scenario's pass criterion (LLM judge)."""
        verdict_raw = await self._chat(
            _JUDGE_SYSTEM.format(objection=scenario.objection, expected=scenario.expected_outcome),
            f"Transcript:\n{transcript}",
            json_mode=True,
        )
        try:
            v = json.loads(verdict_raw)
            return bool(v.get("passed", False)), float(v.get("score", 0.0)), str(v.get("reasoning", ""))
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"judge returned non-JSON: {verdict_raw[:120]!r}")
            return False, 0.0, "judge parse error"

    async def run(
        self, scenario: Scenario, agent_system_prompt: str, *, strategy_version: int
    ) -> EvalResult:
        transcript = await self._chat(
            _SIM_SYSTEM.format(agent_prompt=agent_system_prompt, persona=scenario.persona),
            "Begin the call now.",
        )
        passed, score, reasoning = await self._judge(scenario, transcript)
        return EvalResult(
            objection=scenario.objection,
            passed=passed,
            score=score,
            transcript=transcript,
            reasoning=reasoning,
            backend=self.backend,
            strategy_version=strategy_version,
        )

    async def score_transcript(
        self, scenario: Scenario, transcript: str, *, strategy_version: int
    ) -> EvalResult:
        """Score a REAL (already-happened) call transcript — the live-call learning path.

        Unlike run(), this does NOT simulate a shopper: it judges an actual transcript
        captured from a live outbound call, so the agent learns from real interactions.
        """
        passed, score, reasoning = await self._judge(scenario, transcript)
        return EvalResult(
            objection=scenario.objection,
            passed=passed,
            score=score,
            transcript=transcript,
            reasoning=reasoning,
            backend="live-call",
            strategy_version=strategy_version,
        )


# ── Cekura backend (drops in when a key is present) ───────────────────────────


class CekuraEvaluator:
    """Real Cekura scoring via its MCP server (api.cekura.ai/mcp — the prize axis).

    Uses `scenarios_run_text`: Cekura runs YOUR configured evaluators (scenarios) as a
    cheap text/websocket simulation against the agent, and returns pass/score. The
    verdict comes from Cekura's evaluators, not ours.

    Requires (set in .env once the agent + evalset exist in the Cekura dashboard):
      - CEKURA_API_KEY        (valid; you have this)
      - CEKURA_AGENT_ID       (numeric agent id) OR CEKURA_ASSISTANT_ID (external id)
      - CEKURA_SCENARIO_IDS   (comma-separated evaluator ids to run)

    If those aren't set, this raises in __init__ so make_evaluator falls back to the
    local judge — we NEVER label a local verdict as Cekura.
    """

    backend = "cekura"

    def __init__(self, settings: Settings):
        if not settings.cekura_api_key:
            raise RuntimeError("CekuraEvaluator requires CEKURA_API_KEY")
        if not (settings.cekura_agent_id or settings.cekura_assistant_id):
            raise RuntimeError("CekuraEvaluator requires CEKURA_AGENT_ID or CEKURA_ASSISTANT_ID")
        if not settings.cekura_scenario_ids.strip():
            raise RuntimeError("CekuraEvaluator requires CEKURA_SCENARIO_IDS (evaluator ids)")
        self._settings = settings
        self._key = settings.cekura_api_key
        self._scenario_ids = [
            int(x) for x in settings.cekura_scenario_ids.split(",") if x.strip().isdigit()
        ]
        # Reuse the local judge to generate a transcript for our own records/dashboard.
        self._sim = LocalJudgeEvaluator(settings)

    async def run(
        self, scenario: Scenario, agent_system_prompt: str, *, strategy_version: int
    ) -> EvalResult:
        from .cekura_mcp import CekuraMCP

        args: dict = {
            "name": f"lasso-{scenario.objection}-v{strategy_version}",
            "scenarios": self._scenario_ids,
            "frequency": 1,
        }
        if self._settings.cekura_agent_id.isdigit():
            args["agent_id"] = int(self._settings.cekura_agent_id)
        elif self._settings.cekura_assistant_id:
            args["assistant_id"] = self._settings.cekura_assistant_id

        async with CekuraMCP(self._key) as mcp:
            result = await mcp.call_tool("scenarios_run_text", args)
        score, passed, reasoning = _score_from_run(result)
        # Keep a readable transcript for the dashboard (generated locally; the VERDICT is Cekura's).
        local = await self._sim.run(scenario, agent_system_prompt, strategy_version=strategy_version)
        return EvalResult(
            objection=scenario.objection,
            passed=passed,
            score=score,
            transcript=local.transcript,
            reasoning=reasoning or "scored by Cekura evaluators",
            backend=self.backend,
            strategy_version=strategy_version,
        )


def _score_from_run(result) -> tuple[float, bool, str]:
    """Extract a 0..1 score + pass/fail from a Cekura scenarios_run response.

    Cekura run payloads vary; be defensive across the common shapes (results[],
    success_rate, score). Raise on anything unrecognized so we fall back honestly.
    """
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected Cekura result: {str(result)[:160]}")
    runs = result.get("results") or result.get("runs") or []
    if runs:
        scores = [float(r.get("score", 0)) for r in runs if r.get("score") is not None]
        score = sum(scores) / len(scores) if scores else 0.0
        passed = all(bool(r.get("passed", r.get("success", False))) for r in runs)
    else:
        score = float(result.get("success_rate", result.get("score", 0.0)) or 0.0)
        passed = bool(result.get("success", score >= 0.6))
    reasoning = str(result.get("summary") or result.get("status") or "scored by Cekura")[:200]
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
