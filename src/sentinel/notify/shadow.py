from __future__ import annotations

import logging

from sentinel.notify.message import Notification

logger = logging.getLogger("sentinel.notify.shadow")


class ShadowChannel:
    """影子运行只记录投递意图，不产生任何外部通知副作用。"""

    name = "shadow"

    async def send(self, notification: Notification) -> None:
        logger.info(
            "shadow notification: kind=%s severity=%s subject=%s title=%s",
            notification.kind,
            notification.severity,
            notification.subject,
            notification.title,
        )
