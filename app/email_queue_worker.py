"""Async lifecycle wrapper for durable transactional-email delivery."""
from __future__ import annotations

import asyncio
import logging

from . import config
from .email_delivery import create_email_sender
from .email_queue import TransactionalEmailQueue
from .identity import IdentityStore

logger = logging.getLogger(__name__)


async def email_delivery_loop() -> None:
    """Poll a shared SQLite queue without blocking the ASGI event loop."""
    store = IdentityStore()
    queue = TransactionalEmailQueue(store)
    while True:
        try:
            sender = create_email_sender()
            if sender is None:
                await asyncio.sleep(30)
                continue
            processed = await asyncio.to_thread(queue.process_one, sender)
            await asyncio.sleep(1 if processed else 5)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not log exception text: provider and storage errors can contain
            # operational details. Queue records retain only reviewed codes.
            logger.warning("Transactional email worker encountered an internal error.")
            await asyncio.sleep(15)
