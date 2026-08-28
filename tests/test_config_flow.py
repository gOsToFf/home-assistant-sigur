"""Config, reauth, reconfigure and options flow tests."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.sigur.const import (
    CONF_CA_BUNDLE,
    CONF_CLIENT_CERTIFICATE,
    CONF_CLIENT_KEY,
    CONF_TLS,
    CONF_VERIFY_SSL,
    DOMAIN,
    OPT_BACKFILL_HOURS,
    OPT_ENABLE_BACKFILL,
    OPT_ENABLE_CONTROL,
    OPT_ENABLE_PERSONAL_DATA,
    OPT_EVENT_CATEGORIES,
    OPT_SCAN_INTERVAL,
    OPT_WEBHOOK_ALLOW_INSECURE,
    OPT_WEBHOOK_ENABLED,
    OPT_WEBHOOK_SECRET,
    OPT_WEBHOOK_TIMEOUT,
    OPT_WEBHOOK_URL,
)

from .conftest import requires_home_assistant
from .fake_oif_server import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    FakeBehaviour,
    FakeSigurServer,
    self_signed_context,
)
from .helpers import entry_data, make_entry

pytestmark = requires_home_assistant


@pytest.fixture
async def server() -> FakeSigurServer:
    """A running fake OIF server, torn down after the test."""
    fake = FakeSigurServer()
    await fake.start()
    yield fake
    await fake.stop()


async def _submit(hass: HomeAssistant, user_input: dict) -> dict:
    """Start the user flow and submit ``user_input``."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_user_flow_creates_an_entry(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A valid form creates a config entry titled after the system name."""
    result = await _submit(hass, entry_data(server.port))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Sigur - Офис"
    assert result["data"][CONF_PORT] == server.port
    assert result["result"].unique_id == f"127.0.0.1:{server.port}"


async def test_user_flow_probes_read_access(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Validation logs in and runs a read-only ``GETZONEINFO`` probe."""
    await _submit(hass, entry_data(server.port))
    assert server.login_count >= 1
    assert "GETZONEINFO" in server.received


async def test_user_flow_rejects_bad_credentials(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Wrong credentials show ``invalid_auth`` and create nothing."""
    result = await _submit(hass, entry_data(server.port, password="wrong"))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_user_flow_reports_an_unreachable_server(hass: HomeAssistant) -> None:
    """An unreachable server shows ``cannot_connect``."""
    result = await _submit(hass, entry_data(1))
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_reports_oif_disabled_for_the_operator(
    hass: HomeAssistant,
) -> None:
    """Error 21 is distinguished from a plain authentication failure."""
    fake = FakeSigurServer(behaviour=FakeBehaviour(oif_disabled=True))
    await fake.start()
    try:
        result = await _submit(hass, entry_data(fake.port))
        assert result["errors"] == {"base": "oif_disabled"}
    finally:
        await fake.stop()


async def test_user_flow_reports_an_unsupported_version(hass: HomeAssistant) -> None:
    """Error 3 maps to ``unsupported_version``."""
    fake = FakeSigurServer(behaviour=FakeBehaviour(unsupported_version=True))
    await fake.start()
    try:
        result = await _submit(hass, entry_data(fake.port))
        assert result["errors"] == {"base": "unsupported_version"}
    finally:
        await fake.stop()


async def test_user_flow_reports_permission_denied_on_the_probe(
    hass: HomeAssistant,
) -> None:
    """An operator who can log in but not read zones is rejected."""
    fake = FakeSigurServer(
        behaviour=FakeBehaviour(
            error_on_commands={"GETZONEINFO": (12, "DELEGATION IS DISABLED")}
        )
    )
    await fake.start()
    try:
        result = await _submit(hass, entry_data(fake.port))
        assert result["errors"] == {"base": "permission_denied"}
    finally:
        await fake.stop()


async def test_user_flow_reports_an_invalid_certificate(hass: HomeAssistant) -> None:
    """A TLS server with an untrusted certificate maps to its own error."""
    server_context, _, _ = self_signed_context()
    fake = FakeSigurServer(ssl_context=server_context)
    await fake.start()
    try:
        result = await _submit(
            hass, entry_data(fake.port, **{CONF_TLS: True, CONF_VERIFY_SSL: True})
        )
        assert result["errors"] == {"base": "invalid_certificate"}
    finally:
        await fake.stop()


async def test_user_flow_accepts_a_custom_ca(hass: HomeAssistant) -> None:
    """Pointing at the right CA bundle makes the TLS probe succeed."""
    server_context, _, paths = self_signed_context()
    fake = FakeSigurServer(ssl_context=server_context)
    await fake.start()
    try:
        result = await _submit(
            hass,
            entry_data(
                fake.port,
                **{
                    CONF_TLS: True,
                    CONF_VERIFY_SSL: True,
                    CONF_CA_BUNDLE: paths["ca_bundle"],
                },
            ),
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
    finally:
        await fake.stop()


async def test_user_flow_accepts_mutual_tls(hass: HomeAssistant) -> None:
    """A client certificate is loaded and presented when mTLS is required."""
    server_context, _, paths = self_signed_context(require_client_cert=True)
    fake = FakeSigurServer(ssl_context=server_context)
    await fake.start()
    try:
        result = await _submit(
            hass,
            entry_data(
                fake.port,
                **{
                    CONF_TLS: True,
                    CONF_VERIFY_SSL: True,
                    CONF_CA_BUNDLE: paths["ca_bundle"],
                    CONF_CLIENT_CERTIFICATE: paths["client_cert"],
                    CONF_CLIENT_KEY: paths["client_key"],
                },
            ),
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
    finally:
        await fake.stop()


async def test_user_flow_reports_an_unreadable_ca_bundle(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A missing CA file is an ``invalid_certificate`` error, not a crash."""
    result = await _submit(
        hass,
        entry_data(
            server.port,
            **{CONF_TLS: True, CONF_CA_BUNDLE: "/nonexistent/ca.pem"},
        ),
    )
    assert result["errors"] == {"base": "invalid_certificate"}


async def test_the_same_host_and_port_cannot_be_added_twice(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A duplicate host/port aborts instead of creating a second entry."""
    entry = make_entry(server.port)
    entry.add_to_hass(hass)
    result = await _submit(hass, entry_data(server.port))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_two_different_servers_can_both_be_added(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Two Sigur systems on different ports are both accepted."""
    second = FakeSigurServer()
    await second.start()
    try:
        assert (await _submit(hass, entry_data(server.port)))[
            "type"
        ] is FlowResultType.CREATE_ENTRY
        result = await _submit(hass, entry_data(second.port, name="Sigur - Склад"))
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    finally:
        await second.stop()


async def test_reauth_updates_the_credentials(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A successful reauth stores the new password and reloads the entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sigur - Офис",
        data={**entry_data(server.port), CONF_PASSWORD: "stale"},
        unique_id=f"127.0.0.1:{server.port}",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: DEFAULT_USERNAME, CONF_PASSWORD: DEFAULT_PASSWORD},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == DEFAULT_PASSWORD


async def test_reauth_rejects_wrong_credentials_again(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Reauth stays open while the credentials are still wrong."""
    entry = make_entry(server.port)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: DEFAULT_USERNAME, CONF_PASSWORD: "still-wrong"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reconfigure_moves_the_server_and_keeps_the_registries(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Changing host/port updates the entry without recreating entities."""
    moved = FakeSigurServer()
    await moved.start()
    try:
        entry = make_entry(server.port)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        before = {
            item.entity_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }

        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], entry_data(moved.port)
        )
        await hass.async_block_till_done()

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_PORT] == moved.port

        after = {
            item.entity_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        assert before == after
    finally:
        await moved.stop()


async def test_reconfigure_refuses_to_collide_with_another_entry(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Reconfiguring onto an address that is already configured is refused."""
    other = FakeSigurServer()
    await other.start()
    try:
        first = make_entry(server.port)
        first.add_to_hass(hass)
        second = make_entry(other.port, name="Sigur - Склад")
        second.add_to_hass(hass)

        result = await second.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], entry_data(server.port)
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"
    finally:
        await other.stop()


async def _open_options(hass: HomeAssistant, entry, step: str) -> dict:  # type: ignore[no-untyped-def]
    """Open the options flow and pick ``step`` from the menu."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def test_options_general_step(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The general step stores polling, control and privacy options."""
    entry = make_entry(server.port)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await _open_options(hass, entry, "general")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            OPT_SCAN_INTERVAL: 45,
            OPT_ENABLE_CONTROL: True,
            OPT_ENABLE_PERSONAL_DATA: True,
            "resolve_object_names": True,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_SCAN_INTERVAL] == 45
    assert entry.options[OPT_ENABLE_CONTROL] is True


async def test_options_events_step(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The events step stores the category filter and the backfill window."""
    entry = make_entry(server.port)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await _open_options(hass, entry, "events")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            OPT_EVENT_CATEGORIES: ["pass_registered", "access_denied"],
            OPT_ENABLE_BACKFILL: True,
            OPT_BACKFILL_HOURS: 6,
            "backfill_on_first_start": False,
            "debug_raw_events": False,
        },
    )
    await hass.async_block_till_done()
    assert entry.options[OPT_EVENT_CATEGORIES] == ["pass_registered", "access_denied"]
    assert entry.options[OPT_BACKFILL_HOURS] == 6


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({OPT_WEBHOOK_ENABLED: True, OPT_WEBHOOK_URL: ""}, OPT_WEBHOOK_URL),
        (
            {OPT_WEBHOOK_ENABLED: True, OPT_WEBHOOK_URL: "http://example.com/hook"},
            OPT_WEBHOOK_URL,
        ),
        (
            {
                OPT_WEBHOOK_ENABLED: True,
                OPT_WEBHOOK_URL: "https://example.com/hook",
                OPT_WEBHOOK_SECRET: "",
            },
            OPT_WEBHOOK_SECRET,
        ),
    ],
)
async def test_options_webhook_step_validates_its_input(
    hass: HomeAssistant, server: FakeSigurServer, payload: dict, field: str
) -> None:
    """An enabled webhook needs an HTTPS URL and a signing secret."""
    entry = make_entry(server.port)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await _open_options(hass, entry, "webhook")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            OPT_WEBHOOK_SECRET: "s3cret",
            OPT_WEBHOOK_TIMEOUT: 10,
            OPT_WEBHOOK_ALLOW_INSECURE: False,
            **payload,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert field in result["errors"]


async def test_options_webhook_allows_plain_http_to_a_private_address(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Insecure delivery is possible, but only after explicit confirmation."""
    entry = make_entry(server.port)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await _open_options(hass, entry, "webhook")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            OPT_WEBHOOK_ENABLED: True,
            OPT_WEBHOOK_URL: "http://192.168.1.10/hook",
            OPT_WEBHOOK_SECRET: "s3cret",
            OPT_WEBHOOK_TIMEOUT: 10,
            OPT_WEBHOOK_ALLOW_INSECURE: True,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_WEBHOOK_URL] == "http://192.168.1.10/hook"
