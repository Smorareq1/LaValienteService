"""Outbound notification delivery for identity flows.

The active backend is selected with the ``NOTIFICATIONS_BACKEND`` setting.
``console`` logs the message instead of sending it, which keeps the password
recovery flow fully functional in development. When AWS is contracted, add an
``aws`` backend (SES for email, SNS for SMS) and register it in
``get_notification_service``.
"""

import logging
from typing import Protocol

from src.core.config import get_settings

logger = logging.getLogger("notifications")


class NotificationService(Protocol):
    def send_password_reset_code(self, channel: str, destination: str, code: str) -> None: ...


class ConsoleNotificationService:
    """Development backend: prints the code to the application log."""

    def send_password_reset_code(self, channel: str, destination: str, code: str) -> None:
        logger.warning(
            "[DEV ONLY] Password reset code via %s to %s: %s", channel, destination, code
        )


def get_notification_service() -> NotificationService:
    settings = get_settings()
    if settings.notifications_backend == "console":
        return ConsoleNotificationService()
    raise ValueError(f"Unknown notifications backend: {settings.notifications_backend!r}")
