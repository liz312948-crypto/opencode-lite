from __future__ import annotations

from urllib.error import URLError

import repopilot_lite.llm_client as llm_client
from repopilot_lite.tools import summarize_repo


def test_llm_failure_falls_back_to_rule_summarizer(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "urlopen", _raise_url_error)

    result = summarize_repo(
        repo_path=".",
        question="How should this repository change?",
        files=["README.md", "repopilot_lite/main.py"],
        readme="# OpenCode-Lite",
        search_matches=[],
    )

    assert result["llm_used"] is False
    assert result["repo_summary"]
    assert len(result["modification_plan"]) >= 3
    assert result["risk_notes"]


def _raise_url_error(*args, **kwargs):
    raise URLError("simulated LLM failure")
