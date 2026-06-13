"""
Digest generation — Phase 3 stub.

Will implement:
  - Rule layer: collect stalled/urgent tasks per scope
  - LLM narration via get_provider().chat()
  - Exclusions: snoozed tasks, recently-nudged tasks (last_nudged_at)
  - No send if nothing to report
"""
from __future__ import annotations


async def send_work_digest(chat_id: int) -> None:
    pass  # Phase 3


async def send_personal_digest(chat_id: int) -> None:
    pass  # Phase 3
