"""Outbound webhook tests: signing, filtering, redaction and back-pressure."""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import hmac
import json

from homeassistant.core import HomeAssistant
import pytest

from custom_components.sigur.const import (
    MAX_WEBHOOK_QUEUE,
    OPT_ENABLE_PERSONAL_DATA,
    OPT_WEBHOOK_CATEGORIES,
    OPT_WEBHOOK_ENABLED,
    OPT_WEBHOOK_INCLUDE_NAMES,
    OPT_WEBHOOK_SECRET,
    OPT_WEBHOOK_URL,
)
from custom_components.sigur.webhook import (
    NONCE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    is_private_url,
    sign_payload,
)

from .conftest import requires_home_assistant
from .fake_oif_server import FakeEvent, FakeSigurServer
from .helpers import make_entry

pytestmark = requires_home_assistant

WHEN = datetime(2025, 1, 27, 11, 23, 8)
SECRET = "s3cret-signing-key"
ENDPOINT = "https://sigur.example.com/hook"


@pytest.fixture
async def server() -> FakeSigurServer:
    """A running fake OIF server, torn down after the test."""
    fake = FakeSigurServer()
    await fake.start()
    yield fake
    await fake.stop()


def _options(**overrides: object) -> dict:
    """Webhook options with a valid HTTPS endpoint and a secret."""
    return {
        OPT_WEBHOOK_ENABLED: True,
        OPT_WEBHOOK_URL: ENDPOINT,
        OPT_WEBHOOK_SECRET: SECRET,
        **overrides,
    }


async def _setup(hass: HomeAssistant, fake: FakeSigurServer, **kwargs):  # type: ignore[no-untyped-def]
    """Add and set up a config entry pointing at ``fake``."""
    entry = make_entry(fake.port, **kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _push(hass: HomeAssistant, fake: FakeSigurServer, event: FakeEvent) -> None:
    """Push an event and let the pipeline and the webhook worker drain."""
    await fake.push_event(event)
    for _ in range(10):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("url", "private"),
    [
        ("http://192.168.1.10/hook", True),
        ("http://10.0.0.5/hook", True),
        ("http://127.0.0.1:8080/hook", True),
        ("http://localhost/hook", True),
        ("https://sigur.example.com/hook", False),
        ("https://93.184.216.34/hook", False),
    ],
)
def test_private_url_detection(url: str, private: bool) -> None:
    """Only genuinely private destinations may skip HTTPS."""
    assert is_private_url(url) is private


def test_signature_covers_the_timestamp_the_nonce_and_the_body() -> None:
    """The signature is an HMAC over all three, so nothing can be swapped."""
    body = b'{"a":1}'
    signature = sign_payload(SECRET, "1700000000", "abcd", body)
    expected = hmac.new(
        SECRET.encode(), b"1700000000.abcd." + body, hashlib.sha256
    ).hexdigest()
    assert signature == expected
    assert sign_payload(SECRET, "1700000001", "abcd", body) != signature
    assert sign_payload(SECRET, "1700000000", "efgh", body) != signature
    assert sign_payload("other", "1700000000", "abcd", body) != signature


async def test_webhook_is_disabled_by_default(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """No delivery happens unless the user turned the webhook on."""
    entry = await _setup(hass, server)
    assert entry.runtime_data.hub.webhook is None
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))
    assert aioclient_mock.call_count == 0


async def test_webhook_delivers_a_signed_payload(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """An enabled webhook posts a signed JSON body to the endpoint."""
    aioclient_mock.post(ENDPOINT, status=200)
    await _setup(hass, server, options=_options())
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2, object_name="Иванов"))

    assert aioclient_mock.call_count == 1
    _method, _url, data, headers = aioclient_mock.mock_calls[0]
    body = data if isinstance(data, bytes) else str(data).encode()
    assert headers[SIGNATURE_HEADER] == sign_payload(
        SECRET, headers[TIMESTAMP_HEADER], headers[NONCE_HEADER], body
    )
    payload = json.loads(body)
    assert payload["event_code"] == 4
    assert payload["category"] == "pass_registered"
    assert payload["access_point_id"] == 1


async def test_the_nonce_changes_between_deliveries(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """Every delivery carries a fresh nonce, so replays are detectable."""
    aioclient_mock.post(ENDPOINT, status=200)
    await _setup(hass, server, options=_options())
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))
    await _push(hass, server, FakeEvent(WHEN, 1, 1, 0, 0))

    nonces = {call[3][NONCE_HEADER] for call in aioclient_mock.mock_calls}
    assert len(nonces) == aioclient_mock.call_count == 2


async def test_the_payload_withholds_names_by_default(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """The delivered payload never carries a name unless asked to."""
    aioclient_mock.post(ENDPOINT, status=200)
    await _setup(hass, server, options=_options(**{OPT_ENABLE_PERSONAL_DATA: True}))
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2, object_name="Иванов"))

    payload = json.loads(aioclient_mock.mock_calls[0][2])
    assert "object_name" not in payload
    assert "raw_message" not in payload
    assert payload["key_masked"] == "W26 ***23"


async def test_the_payload_withholds_object_ids_without_the_option(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """Without the personal-data option the delivered id is empty."""
    aioclient_mock.post(ENDPOINT, status=200)
    await _setup(hass, server, options=_options())
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2, object_name="Иванов"))
    payload = json.loads(aioclient_mock.mock_calls[0][2])
    assert payload["object_id"] is None


async def test_names_can_be_included_explicitly(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """Both the personal-data and the webhook option must be on."""
    aioclient_mock.post(ENDPOINT, status=200)
    await _setup(
        hass,
        server,
        options=_options(
            **{OPT_ENABLE_PERSONAL_DATA: True, OPT_WEBHOOK_INCLUDE_NAMES: True}
        ),
    )
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2, object_name="Иванов"))
    payload = json.loads(aioclient_mock.mock_calls[0][2])
    assert payload["object_name"] == "Иванов"


async def test_categories_can_be_filtered(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """Only the selected categories are forwarded."""
    aioclient_mock.post(ENDPOINT, status=200)
    await _setup(
        hass, server, options=_options(**{OPT_WEBHOOK_CATEGORIES: ["break_in"]})
    )

    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))
    assert aioclient_mock.call_count == 0

    await _push(hass, server, FakeEvent(WHEN, 1, 1, 0, 0))
    assert aioclient_mock.call_count == 1


async def test_an_http_endpoint_is_refused(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """A public plain-HTTP endpoint never receives anything."""
    entry = await _setup(
        hass, server, options=_options(**{OPT_WEBHOOK_URL: "http://example.com/hook"})
    )
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))
    assert aioclient_mock.call_count == 0
    assert entry.runtime_data.hub.webhook.queue_size == 0


async def test_a_missing_secret_disables_delivery(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """Without a signing secret nothing is sent."""
    await _setup(hass, server, options=_options(**{OPT_WEBHOOK_SECRET: ""}))
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))
    assert aioclient_mock.call_count == 0


async def test_a_failing_endpoint_is_retried_then_given_up_on(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """Delivery is retried a bounded number of times, then counted as failed."""
    aioclient_mock.post(ENDPOINT, status=500)
    entry = await _setup(hass, server, options=_options())
    forwarder = entry.runtime_data.hub.webhook
    assert forwarder is not None

    await server.push_event(FakeEvent(WHEN, 4, 1, 6, 2))
    for _ in range(60):
        await asyncio.sleep(0.15)
        await hass.async_block_till_done()
        if forwarder.failure_count:
            break

    assert forwarder.failure_count == 1
    assert aioclient_mock.call_count == 3


async def test_the_queue_is_bounded(
    hass: HomeAssistant, server: FakeSigurServer, aioclient_mock
) -> None:
    """A stalled endpoint drops the oldest events instead of growing forever."""
    entry = await _setup(hass, server, options=_options())
    forwarder = entry.runtime_data.hub.webhook
    assert forwarder is not None

    from custom_components.sigur.api.event_codes import EventCategory
    from custom_components.sigur.models import SigurEvent

    for index in range(MAX_WEBHOOK_QUEUE + 25):
        forwarder.enqueue(
            SigurEvent(
                server_entry_id=entry.entry_id,
                server_name="Sigur",
                occurred_at=WHEN,
                event_code=4,
                event_type="pass_registered",
                category=EventCategory.PASS_REGISTERED,
                description="Pass registered",
                access_point_id=1,
                access_point_name="Главный вход",
                object_id=index,
                object_name=None,
                direction_code=2,
                direction="in",
                key_masked="UNKNOWN",
            )
        )

    assert forwarder.queue_size <= MAX_WEBHOOK_QUEUE
    assert forwarder.dropped_count >= 25
