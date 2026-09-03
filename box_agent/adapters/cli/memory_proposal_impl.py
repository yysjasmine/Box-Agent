"""CLI handler for MemoryProposalEvent.

    Renders proposed v2 experience → MEMORY.md (core) promotions in the
terminal.  When the event carries an LLM-drafted plan, the user gets one
diff to apply/reject in a single keystroke; otherwise we fall back to
the legacy per-candidate pin/skip/reject flow.

Mirrors the termios pattern from ``permission_broker_impl.py`` to avoid
prompt_toolkit interference.
"""

from __future__ import annotations

import asyncio
import difflib
import sys
from typing import Any

from box_agent.compat.events import MemoryProposalEvent


class Colors:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    CYAN = "\033[36m"


_VALID_CHOICES = {"p", "pin", "1", "s", "skip", "2", "", "r", "reject", "3"}
_PIN_KEYS = {"p", "pin", "1"}
_REJECT_KEYS = {"r", "reject", "3"}
# Plan-mode keys
_APPLY_KEYS = {"a", "apply", "1", "y", "yes"}
_PLAN_REJECT_KEYS = {"r", "reject", "2", "n", "no"}


def _read_with_echo(prompt_text: str) -> str:
    try:
        import termios

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        try:
            new_attrs = termios.tcgetattr(fd)
            new_attrs[3] |= termios.ECHO | termios.ICANON
            termios.tcsetattr(fd, termios.TCSADRAIN, new_attrs)
            return input(prompt_text).strip().lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
    except (ImportError, OSError):
        return input(prompt_text).strip().lower()


async def _prompt(prompt_text: str) -> str:
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _read_with_echo, prompt_text)
    except (EOFError, KeyboardInterrupt):
        return ""


def _render_diff(old: str, new: str, max_lines: int = 40) -> str:
    """Colored unified diff for terminal display."""
    diff_lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="MEMORY.md (current)",
            tofile="MEMORY.md (proposed)",
            lineterm="",
            n=2,
        )
    )
    if not diff_lines:
        return f"{Colors.DIM}(no changes){Colors.RESET}"

    truncated = False
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines]
        truncated = True

    out: list[str] = []
    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            out.append(f"{Colors.BOLD}{line}{Colors.RESET}")
        elif line.startswith("+"):
            out.append(f"{Colors.GREEN}{line}{Colors.RESET}")
        elif line.startswith("-"):
            out.append(f"{Colors.RED}{line}{Colors.RESET}")
        elif line.startswith("@@"):
            out.append(f"{Colors.CYAN}{line}{Colors.RESET}")
        else:
            out.append(line)
    if truncated:
        out.append(f"{Colors.DIM}… (diff truncated){Colors.RESET}")
    return "\n".join(out)


class CLIMemoryProposalNegotiator:
    """Dispatch to plan-mode or legacy per-candidate prompt and persist decisions."""

    def __init__(self, memory_manager: Any) -> None:
        self._mgr = memory_manager

    async def negotiate(self, event: MemoryProposalEvent) -> None:
        if event.plan is not None:
            await self._negotiate_plan(event)
        else:
            await self._negotiate_legacy(event)

    async def _negotiate_plan(self, event: MemoryProposalEvent) -> None:
        plan = event.plan
        assert plan is not None

        print()
        print(f"{Colors.BOLD}🧠 记忆升级建议 (LLM 已起草){Colors.RESET}")
        if plan.rationale:
            print(f"{Colors.DIM}{plan.rationale}{Colors.RESET}")
        print(
            f"{Colors.DIM}  将合并 {len(plan.consumed_entry_ids)} 条 context "
            f"到 MEMORY.md, 应用后这些条目从 v2 experience 删除。{Colors.RESET}"
        )
        print()
        print(_render_diff(plan.current_core, plan.new_core))
        print()
        print(
            f"{Colors.DIM}  [1/a/y] apply — 应用此方案   "
            f"[2/r/n] reject — 拒绝（标记永不再提议）   "
            f"[回车/s] skip — 暂不{Colors.RESET}"
        )
        choice = await _prompt("  选择 [1/2/回车]: ")

        try:
            if choice in _APPLY_KEYS:
                counts = await asyncio.to_thread(
                    self._mgr.apply_promotion_plan,
                    plan,
                )
                print(
                    f"{Colors.GREEN}✓ 已应用 — consumed {counts.get('consumed', 0)} "
                    f"条 context{Colors.RESET}"
                )
            elif choice in _PLAN_REJECT_KEYS:
                counts = await asyncio.to_thread(
                    self._mgr.reject_promotion_plan,
                    plan,
                )
                print(
                    f"{Colors.RED}✗ 已拒绝 — {counts.get('rejected', 0)} "
                    f"条永不再提议{Colors.RESET}"
                )
            else:
                print(f"{Colors.YELLOW}↷ 暂不（下次冷却到期后再提议）{Colors.RESET}")
        except Exception as exc:
            print(f"{Colors.RED}记忆升级写入失败: {exc}{Colors.RESET}")

    async def _negotiate_legacy(self, event: MemoryProposalEvent) -> None:
        candidates = event.candidates
        if not candidates:
            return

        print()
        print(f"{Colors.BOLD}🧠 记忆升级提议 ({len(candidates)} 条){Colors.RESET}")
        print(f"{Colors.DIM}以下条目命中频次较高，是否要加入永久记忆 (MEMORY.md)?{Colors.RESET}")
        print(f"{Colors.DIM}  [1/p] pin — 加入核心 (永久)   [2/s/回车] skip — 暂不   [3/r] reject — 永不再提议{Colors.RESET}")
        print()

        decisions: dict[str, str] = {}
        for i, cand in enumerate(candidates, 1):
            preview = cand.content.strip().splitlines()[0]
            if len(preview) > 120:
                preview = preview[:117] + "…"
            print(f"{Colors.CYAN}[{i}/{len(candidates)}]{Colors.RESET} {preview}")
            print(f"  {Colors.DIM}hits={cand.hits}  confidence={cand.confidence:.2f}{Colors.RESET}")
            choice = await _prompt("  选择 [1/2/3]: ")
            if choice in _PIN_KEYS:
                decisions[cand.entry_id] = "pin"
                print(f"  {Colors.GREEN}✓ 已加入核心记忆{Colors.RESET}")
            elif choice in _REJECT_KEYS:
                decisions[cand.entry_id] = "reject"
                print(f"  {Colors.RED}✗ 已拒绝（永不再提议）{Colors.RESET}")
            else:
                decisions[cand.entry_id] = "skip"
                print(f"  {Colors.YELLOW}↷ 暂不{Colors.RESET}")
            print()

        try:
            counts = await asyncio.to_thread(
                self._mgr.consume_core_proposal,
                decisions,
            )
        except Exception as exc:
            print(f"{Colors.RED}记忆升级写入失败: {exc}{Colors.RESET}")
            return

        summary = (
            f"{Colors.DIM}小结: pinned={counts.get('pinned', 0)} "
            f"rejected={counts.get('rejected', 0)} skipped={counts.get('skipped', 0)}{Colors.RESET}"
        )
        print(summary)
