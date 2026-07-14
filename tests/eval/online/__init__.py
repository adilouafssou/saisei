"""Out-of-band LangSmith online eval suite (MR #2).

These tests are SKIPPED by default and in CI. They require:
  - SAISEI_LANGSMITH_TRACING=true
  - SAISEI_LANGSMITH_API_KEY=<non-empty>
  - SAISEI_LLM_API_KEY=<non-empty>
  - SAISEI_LLM_MODEL=<non-empty>

They MUST NOT run in the PR/CI gate (make verify). They are guarded by
pytest.mark.skipif on the settings/env so ``uv run pytest`` offline
collects-and-skips them, never fails.
"""
