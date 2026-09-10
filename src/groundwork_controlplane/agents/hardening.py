"""Shared system-prompt security preamble (OWASP LLM01/LLM02/LLM06/LLM07).

Every LLM-facing system prompt in this service prepends :data:`PROMPT_HARDENING_PREAMBLE`
so that prompt-level defenses live in exactly one place and cannot drift between agents.
The wording deliberately states each defense explicitly — role boundary, instruction
override, disclosure, output format, language, unicode, length, indirect injection,
social engineering, harmful content, abuse, and input validation — because these are
the defense classes verified by ``agt red-team scan`` (Agent Governance Toolkit) against
this repository's prompts.

The text itself lives in ``prompts/hardening_preamble.md``, not here. A change to prompt
wording should be a text-file diff a security reviewer can read without also reading Python.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"

PROMPT_HARDENING_PREAMBLE = (_PROMPTS_DIR / "hardening_preamble.md").read_text(encoding="utf-8")
