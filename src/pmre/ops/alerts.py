"""Telegram alerting (plain ``sendMessage`` via httpx)."""

from __future__ import annotations

import enum

import httpx

from ..logging_setup import get_logger

log = get_logger("ops.alerts")


class AlertLevel(enum.StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


_EMOJI = {
    AlertLevel.INFO: "ℹ️",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.CRITICAL: "🚨",
}


def format_alert(level: AlertLevel, service: str, message: str) -> str:
    """Deterministic, human-readable alert text (unit-tested)."""
    emoji = _EMOJI[level]
    return f"{emoji} [{level.value}] {service}: {message}"


class TelegramAlerter:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        base_url: str = "https://api.telegram.org",
        timeout: float = 10.0,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.enabled = bool(bot_token and chat_id)

    @property
    def _url(self) -> str:
        return f"{self.base_url}/bot{self.bot_token}/sendMessage"

    async def send(
        self, level: AlertLevel, service: str, message: str
    ) -> bool:
        text = format_alert(level, service, message)
        if not self.enabled:
            log.warning("alert_disabled", text=text)
            return False
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self._url,
                    json={"chat_id": self.chat_id, "text": text},
                )
                resp.raise_for_status()
                return True
        except Exception as exc:  # pragma: no cover - network failure path
            log.error("alert_send_failed", error=str(exc), text=text)
            return False

    def send_sync(self, level: AlertLevel, service: str, message: str) -> bool:
        text = format_alert(level, service, message)
        if not self.enabled:
            log.warning("alert_disabled", text=text)
            return False
        try:
            resp = httpx.post(
                self._url,
                json={"chat_id": self.chat_id, "text": text},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:  # pragma: no cover
            log.error("alert_send_failed", error=str(exc), text=text)
            return False
