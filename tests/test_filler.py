"""The 'still working' filler: fires when a reply is slow, stays silent when it's fast."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from agentzero import main
from agentzero.prompts import THINKING_FILLERS


@pytest.mark.asyncio
async def test_filler_fires_after_delay():
    with patch.object(main, "THINKING_FILLER_SECONDS", 0.0), \
         patch("agentzero.main.send", new_callable=AsyncMock) as mock_send:
        await main._thinking_filler(123)
    mock_send.assert_called_once()
    assert mock_send.call_args[0][1] in THINKING_FILLERS


@pytest.mark.asyncio
async def test_filler_cancelled_when_reply_is_fast():
    """If the answer comes back before the delay, the filler never sends."""
    with patch.object(main, "THINKING_FILLER_SECONDS", 5.0), \
         patch("agentzero.main.send", new_callable=AsyncMock) as mock_send:
        task = asyncio.create_task(main._thinking_filler(123))
        await asyncio.sleep(0)   # let it start and hit the sleep
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    mock_send.assert_not_called()
