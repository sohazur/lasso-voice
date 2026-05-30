"""Persistence for eval runs + mined exemplars.

Supabase when configured; in-memory otherwise so the whole loop runs offline today.
The store also assembles the `objection_exemplars` block that conditions the agent's
system prompt — closing the loop: each run uses the latest mined corrections.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from loguru import logger

from ..config import Settings
from .types import EvalResult, Exemplar


class LoopStore:
    def __init__(self, settings: Settings):
        self._client = None
        if settings.supabase_url and settings.supabase_key:
            try:
                from supabase import create_client

                self._client = create_client(settings.supabase_url, settings.supabase_key)
                logger.info("LoopStore: Supabase")
            except Exception:
                logger.exception("LoopStore: Supabase init failed; using in-memory")
        if self._client is None:
            logger.info("LoopStore: in-memory (no Supabase configured)")
        # In-memory mirrors (also used as the source for prompt assembly).
        self._exemplars: dict[str, list[Exemplar]] = defaultdict(list)  # merchant -> exemplars
        self._runs: list[dict[str, Any]] = []
        self._versions: dict[str, int] = defaultdict(int)  # merchant -> strategy version

    # ── demo control ───────────────────────────────────────────────────────────
    def reset(self, merchant_id: str) -> None:
        """Clear a merchant's learned state (for re-running the demo from v0)."""
        self._exemplars.pop(merchant_id, None)
        self._versions.pop(merchant_id, None)
        self._runs = [r for r in self._runs if r["merchant_id"] != merchant_id]

    # ── versions ─────────────────────────────────────────────────────────────
    def strategy_version(self, merchant_id: str) -> int:
        return self._versions[merchant_id]

    def _bump_version(self, merchant_id: str) -> int:
        self._versions[merchant_id] += 1
        return self._versions[merchant_id]

    # ── runs ─────────────────────────────────────────────────────────────────
    def record_run(self, merchant_id: str, result: EvalResult) -> None:
        row = {
            "merchant_id": merchant_id,
            "objection": result.objection,
            "score": result.score,
            "passed": result.passed,
            "strategy_version": result.strategy_version,
            "backend": result.backend,
            "transcript": result.transcript,
            "reasoning": result.reasoning,
        }
        self._runs.append(row)
        if self._client:
            try:
                self._client.table("cekura_runs").insert(row).execute()
            except Exception:
                logger.exception("record_run: Supabase insert failed (kept in-memory)")

    def runs_for(self, merchant_id: str) -> list[dict[str, Any]]:
        return [r for r in self._runs if r["merchant_id"] == merchant_id]

    # ── exemplars ──────────────────────────────────────────────────────────────
    def add_exemplar(self, merchant_id: str, exemplar: Exemplar) -> int:
        """Store a mined exemplar and bump the merchant's strategy version."""
        self._exemplars[merchant_id].append(exemplar)
        version = self._bump_version(merchant_id)
        if self._client:
            try:
                self._client.table("exemplars").insert(
                    {
                        "merchant_id": merchant_id,
                        "objection": exemplar.objection,
                        "corrected_handling": exemplar.corrected_handling,
                    }
                ).execute()
            except Exception:
                logger.exception("add_exemplar: Supabase insert failed (kept in-memory)")
        return version

    def exemplars_for(self, merchant_id: str) -> list[Exemplar]:
        return list(self._exemplars[merchant_id])

    def objection_guidance(self, merchant_id: str) -> str | None:
        """Assemble the prompt block from mined exemplars (None if none yet → v0)."""
        ex = self._exemplars[merchant_id]
        if not ex:
            return None
        lines = [
            f"- When the shopper raises a '{e.objection}' objection: {e.corrected_handling}"
            for e in ex
        ]
        return "Use these learned, proven responses:\n" + "\n".join(lines)
