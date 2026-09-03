"""Memory promotion proposal service independent of ACP and CLI hosts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from box_agent.compat.events import MemoryPromotionPlan


logger = logging.getLogger(__name__)


class MemoryProposalService:
    """List, plan, and apply durable memory promotion decisions."""

    def __init__(
        self,
        memory_manager: Any | None,
        *,
        hit_threshold: int,
        cooldown_days: int,
        planning_llm_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self._memory = memory_manager
        self._hit_threshold = hit_threshold
        self._cooldown_days = cooldown_days
        self._planning_llm_resolver = planning_llm_resolver

    async def list(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._memory is None:
            return {"candidates": []}
        session_id = str(params.get("sessionId", "") or "")
        cooldown = 0 if bool(params.get("includeCooldown")) else self._cooldown_days
        entries = await asyncio.to_thread(
            self._memory.list_promotion_candidates,
            hit_threshold=self._hit_threshold,
            cooldown_days=cooldown,
        )
        candidates = [
            {
                "id": entry.id,
                "content": entry.content,
                "hits": entry.hits,
                "confidence": entry.confidence,
                "created": entry.created,
                "last_used": entry.last_used,
                "last_proposed": entry.last_proposed,
            }
            for entry in entries
        ]
        plan_payload: dict[str, Any] | None = None
        if bool(params.get("includePlan")) and entries:
            wanted = {entry.id for entry in entries}
            try:
                context_entries = await asyncio.to_thread(
                    self._memory.read_all_context_entries
                )
                full_entries = [entry for entry in context_entries if entry.id in wanted]
            except Exception as exc:
                logger.warning("memory proposal context read failed: %s", exc)
                full_entries = []
            if full_entries and self._planning_llm_resolver is not None:
                try:
                    plan = await self._memory.plan_promotion(
                        full_entries,
                        self._planning_llm_resolver(session_id),
                    )
                except Exception as exc:
                    logger.warning("memory proposal planning failed: %s", exc)
                    plan = None
                if plan is not None:
                    plan_payload = {
                        "currentCore": plan.current_core,
                        "newCore": plan.new_core,
                        "consumedEntryIds": list(plan.consumed_entry_ids),
                        "rationale": plan.rationale,
                    }
        response: dict[str, Any] = {"candidates": candidates}
        if plan_payload is not None:
            response["plan"] = plan_payload
        return response

    async def apply(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._memory is None:
            return {"error": "memory_unavailable"}
        raw_plan = params.get("plan")
        if isinstance(raw_plan, Mapping):
            return await self._apply_plan(params, raw_plan)

        raw_decisions = params.get("decisions") or {}
        if not isinstance(raw_decisions, Mapping):
            return {"error": "invalid_decisions"}
        decisions = {
            str(entry_id): value
            for entry_id, value in raw_decisions.items()
            if isinstance(value, str) and value in ("pin", "skip", "reject")
        }

        def consume() -> tuple[dict[str, int], str]:
            with self._memory.context_transaction():
                counts = self._memory.consume_core_proposal(decisions)
                return counts, self._memory.read_core()

        counts, core = await asyncio.to_thread(consume)
        return {
            "pinned": counts["pinned"],
            "rejected": counts["rejected"],
            "skipped": counts["skipped"],
            "core": core,
        }

    async def _apply_plan(
        self,
        params: Mapping[str, Any],
        raw_plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        decision = str(params.get("decision", "")).lower()
        if decision not in ("apply", "reject", "skip"):
            return {"error": "invalid_decision"}
        raw_ids = raw_plan.get("consumedEntryIds") or []
        if not isinstance(raw_ids, list):
            return {"error": "invalid_plan"}
        plan = MemoryPromotionPlan(
            current_core=str(raw_plan.get("currentCore", "")),
            new_core=str(raw_plan.get("newCore", "")),
            consumed_entry_ids=tuple(str(item) for item in raw_ids),
            rationale=str(raw_plan.get("rationale", "")),
        )
        if decision in ("apply", "reject") and not plan.consumed_entry_ids:
            return {"error": "invalid_plan"}
        if decision == "apply" and not plan.new_core.strip():
            return {"error": "invalid_plan"}
        if decision == "apply":
            def apply_plan() -> tuple[dict[str, int], str]:
                with self._memory.context_transaction():
                    counts = self._memory.apply_promotion_plan(plan)
                    return counts, self._memory.read_core()

            counts, core = await asyncio.to_thread(apply_plan)
            return {
                "applied": counts.get("applied", 1),
                "consumed": counts.get("consumed", 0),
                "core": core,
            }
        if decision == "reject":
            def reject_plan() -> tuple[dict[str, int], str]:
                with self._memory.context_transaction():
                    counts = self._memory.reject_promotion_plan(plan)
                    return counts, self._memory.read_core()

            counts, core = await asyncio.to_thread(reject_plan)
            return {"rejected": counts.get("rejected", 0), "core": core}
        core = await asyncio.to_thread(self._memory.read_core)
        return {"skipped": 1, "core": core}


__all__ = ["MemoryProposalService"]
