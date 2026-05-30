"""The loop orchestrator.

improve(merchant, objection):
  1. run eval against the CURRENT prompt              -> `before` (red at 9am)
  2. if it passed, we're done (nothing to learn)
  3. mine the failure into a corrected exemplar       -> store, bump strategy version
  4. re-run against the NOW-improved prompt           -> `after` (green at 2pm)

The prompt is reassembled each run from the store's exemplars, so step 4 genuinely
uses what step 3 learned. No human in the loop.
"""

from __future__ import annotations

from loguru import logger

from ..config import Settings
from ..prompts import build_system_prompt
from . import scenarios
from .evaluator import make_evaluator
from .miner import FailureMiner
from .store import LoopStore
from .types import LoopReport


class ImprovementLoop:
    def __init__(self, settings: Settings, store: LoopStore | None = None):
        self.settings = settings
        self.store = store or LoopStore(settings)
        self.evaluator = make_evaluator(settings)
        self.miner = FailureMiner(settings)

    def _prompt_for(self, merchant_id: str, store_name: str) -> str:
        return build_system_prompt(
            store_name=store_name,
            objection_exemplars=self.store.objection_guidance(merchant_id),
        )

    async def evaluate_once(self, merchant_id: str, objection: str, store_name: str = "the store"):
        scenario = scenarios.get(objection)
        version = self.store.strategy_version(merchant_id)
        prompt = self._prompt_for(merchant_id, store_name)
        result = await self.evaluator.run(scenario, prompt, strategy_version=version)
        self.store.record_run(merchant_id, result)
        logger.info(
            f"eval merchant={merchant_id} obj={objection} v{version} "
            f"-> {'GREEN' if result.passed else 'RED'} score={result.score:.2f}"
        )
        return result

    async def improve(
        self, merchant_id: str, objection: str = "price", store_name: str = "the store"
    ) -> LoopReport:
        scenario = scenarios.get(objection)
        before = await self.evaluate_once(merchant_id, objection, store_name)
        report = LoopReport(objection=objection, before=before, rounds=[before])

        if before.passed:
            logger.info(f"{objection} already GREEN — nothing to mine")
            report.after = before
            return report

        # RED -> mine the failure into a corrected exemplar, store it (bumps version).
        exemplar = await self.miner.mine(before, scenario.expected_outcome)
        version = self.store.add_exemplar(merchant_id, exemplar)
        report.mined = exemplar
        logger.info(f"mined {objection} exemplar -> strategy v{version}")

        # Re-run against the now-improved prompt.
        after = await self.evaluate_once(merchant_id, objection, store_name)
        report.after = after
        report.rounds.append(after)
        return report
