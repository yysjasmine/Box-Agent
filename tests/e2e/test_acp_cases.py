"""Protocol-level ACP end-to-end acceptance cases."""

from __future__ import annotations

from .run_acp_cases import CASE_IDS, run_cases


def test_all_acp_cases_pass_without_network_or_credentials() -> None:
    report = run_cases()
    assert [case["id"] for case in report["cases"]] == list(CASE_IDS)
    assert all(case["status"] == "passed" for case in report["cases"]), report


def test_acp_cases_emit_renderable_updates_and_durable_facts() -> None:
    report = run_cases()
    for case in report["cases"]:
        assert case["acp_updates"] >= 1
        assert case["events"]
        assert case["events"][-1]["type"] == "run.completed"
        assert case["result"]["status"] == "completed"
