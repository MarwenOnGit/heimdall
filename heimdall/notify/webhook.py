"""Notifications to humans: a chat webhook (Discord or Slack format)."""

import logging
from typing import Protocol

import httpx

log = logging.getLogger("heimdall.notify")


class Notifier(Protocol):
    def send(self, title: str, text: str) -> None: ...


class NullNotifier:
    def send(self, title: str, text: str) -> None:
        log.info("%s: %s", title, text)


class WebhookNotifier:
    """Posts to an incoming webhook. The payload carries both `content`
    (Discord) and `text` (Slack), so either service accepts it.

    The message is built by Heimdall from structured data. Text that came
    from workloads has already been redacted for secrets before it got here.
    """

    def __init__(self, client: httpx.Client, url: str) -> None:
        self._client = client
        self._url = url

    def send(self, title: str, text: str) -> None:
        message = f"**{title}**\n{text}"[:1900]
        try:
            self._client.post(self._url, json={"content": message, "text": message})
        except httpx.HTTPError:
            # A chat outage must never stop incident handling.
            log.warning("could not deliver notification %r", title)
