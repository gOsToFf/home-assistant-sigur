"""Optional signed outbound webhook for Sigur events.

This is off by default and is *not* the recommended integration path: the Home
Assistant event bus, device triggers and actions already let a user build a
standard automation and forward events with ``rest_command`` or MQTT. The
webhook exists for deployments that need a direct, signed feed.

Delivery never blocks the OIF reader: events go into a bounded queue that a
worker drains, and a slow or dead endpoint drops the oldest events instead of
growing without limit.
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import MAX_WEBHOOK_QUEUE
from .models import SigurEvent

if TYPE_CHECKING:
    from .runtime import SigurHub

_LOGGER = logging.getLogger(__name__)

#: How many times one event is retried before it is given up on.
_MAX_ATTEMPTS = 3
#: Base delay of the per-event retry backoff, in seconds.
_RETRY_BASE_DELAY = 2.0

SIGNATURE_HEADER = "X-Sigur-Signature"
TIMESTAMP_HEADER = "X-Sigur-Timestamp"
NONCE_HEADER = "X-Sigur-Nonce"


def is_private_url(url: str) -> bool:
    """Whether ``url`` points at a private or loopback address."""
    host = urlparse(url).hostname
    if host is None:
        return False
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def sign_payload(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Compute the HMAC-SHA256 signature of one delivery.

    The timestamp and nonce are part of the signed material, so a captured
    request cannot be replayed against a receiver that tracks either of them.
    """
    message = b".".join((timestamp.encode(), nonce.encode(), body))
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class WebhookForwarder:
    """Delivers Sigur events to an external HTTPS endpoint, signed."""

    def __init__(self, hass: HomeAssistant, hub: SigurHub) -> None:
        """Bind the forwarder to a hub whose options enable it."""
        self.hass = hass
        self.hub = hub
        self._queue: deque[SigurEvent] = deque(maxlen=MAX_WEBHOOK_QUEUE)
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self.failure_count = 0
        self.delivered_count = 0
        self.dropped_count = 0

    async def async_setup(self) -> None:
        """Validate the configuration and start the delivery worker."""
        options = self.hub.options
        if not options.webhook_url or not options.webhook_secret:
            _LOGGER.error(
                "Sigur (%s): the outbound webhook needs both a URL and a secret; "
                "it stays disabled",
                self.hub.server_name,
            )
            return
        if not options.webhook_url.startswith("https://") and not (
            options.webhook_allow_insecure and is_private_url(options.webhook_url)
        ):
            _LOGGER.error(
                "Sigur (%s): the outbound webhook URL must use HTTPS unless it "
                "points at a private address and insecure delivery was confirmed; "
                "it stays disabled",
                self.hub.server_name,
            )
            return
        self._task = self.hass.async_create_background_task(
            self._worker(), f"sigur-webhook-{self.hub.entry.entry_id}"
        )

    async def async_shutdown(self) -> None:
        """Stop the worker and drop anything still queued."""
        self._closing = True
        self._wakeup.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._queue.clear()

    @property
    def queue_size(self) -> int:
        """How many events are waiting to be delivered."""
        return len(self._queue)

    @callback
    def enqueue(self, event: SigurEvent) -> None:
        """Queue one event, dropping the oldest when the queue is full."""
        if self._task is None or self._closing:
            return
        categories = self.hub.options.webhook_categories
        if categories and event.category.value not in categories:
            return
        if len(self._queue) == self._queue.maxlen:
            self.dropped_count += 1
        self._queue.append(event)
        self._wakeup.set()

    def _payload(self, event: SigurEvent) -> dict[str, Any]:
        """Build the delivered payload, stripping personal data by default."""
        payload = event.as_bus_payload(
            include_raw=False,
            include_personal=self.hub.options.enable_personal_data,
        )
        payload.pop("raw_message", None)
        # The name needs a second, webhook-specific opt-in: sending it off the
        # machine is a bigger step than showing it in the local UI.
        if not self.hub.options.webhook_include_names:
            payload.pop("object_name", None)
        return payload

    async def _worker(self) -> None:
        """Drain the queue forever."""
        while not self._closing:
            if not self._queue:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            event = self._queue.popleft()
            await self._async_deliver(event)

    async def _async_deliver(self, event: SigurEvent) -> None:
        """Send one event, retrying a bounded number of times."""
        options = self.hub.options
        if not options.webhook_url or not options.webhook_secret:
            # async_setup refuses to start the worker without both, so this can
            # only happen if the options changed underneath a running worker.
            return
        url, secret = options.webhook_url, options.webhook_secret
        body = json.dumps(
            self._payload(event), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        session = async_get_clientsession(self.hass)

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            timestamp = str(int(time.time()))
            nonce = secrets.token_hex(16)
            headers = {
                "Content-Type": "application/json",
                TIMESTAMP_HEADER: timestamp,
                NONCE_HEADER: nonce,
                SIGNATURE_HEADER: sign_payload(secret, timestamp, nonce, body),
            }
            try:
                async with session.post(
                    url,
                    data=body,
                    headers=headers,
                    timeout=ClientTimeout(total=options.webhook_timeout),
                ) as response:
                    if response.status < 400:
                        self.delivered_count += 1
                        return
                    _LOGGER.debug(
                        "Sigur (%s): the webhook endpoint answered %s",
                        self.hub.server_name,
                        response.status,
                    )
            except (ClientError, TimeoutError) as err:
                _LOGGER.debug(
                    "Sigur (%s): webhook delivery attempt %d failed: %s",
                    self.hub.server_name,
                    attempt,
                    err,
                )
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))

        self.failure_count += 1
        _LOGGER.warning(
            "Sigur (%s): gave up delivering a %s event to the webhook after "
            "%d attempts",
            self.hub.server_name,
            event.category.value,
            _MAX_ATTEMPTS,
        )
