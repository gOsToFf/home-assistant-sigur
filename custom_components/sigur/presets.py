"""Named sets of access points for the panel, and who may see what.

A preset is a saved selection: some access points, and some whole zones so that
a point added to a zone later joins without anyone editing the preset. Presets
span servers, so they live in one store for the whole integration rather than
in any config entry.

A preset may also name Home Assistant users. That is what restricts the panel:
a non-admin user named in at least one preset sees only the access points of
their presets, and the events of those points. A user named in none sees
everything, as before presets existed, and an administrator always sees
everything. A preset naming nobody is just a shared view and restricts no one.

This governs what the panel shows, not what a user may do. Home Assistant has
no per-entity permissions, so the entities themselves stay reachable elsewhere.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import PRESETS_STORAGE_KEY, PRESETS_STORAGE_VERSION

#: Key under ``hass.data`` holding the one :class:`PresetStore`.
_DATA_KEY = f"{PRESETS_STORAGE_KEY}_store"


def ap_key(entry_id: str, ap_id: int) -> str:
    """The key an access point goes by across servers."""
    return f"{entry_id}:{ap_id}"


def zone_key(entry_id: str, zone_id: int) -> str:
    """The key a zone goes by across servers."""
    return f"{entry_id}:{zone_id}"


@dataclass(slots=True)
class Preset:
    """One saved selection of access points."""

    id: str
    name: str
    access_points: list[str] = field(default_factory=list)
    """Access point keys, ``entry_id:ap_id``."""

    zones: list[str] = field(default_factory=list)
    """Zone keys, ``entry_id:zone_id``; every point on the A side belongs."""

    users: list[str] = field(default_factory=list)
    """Home Assistant user ids this preset is assigned to."""

    def covers(self, entry_id: str, ap_id: int, zone_id: int | None) -> bool:
        """Whether the access point belongs to this preset."""
        if ap_key(entry_id, ap_id) in self.access_points:
            return True
        return zone_id is not None and zone_key(entry_id, zone_id) in self.zones

    def as_dict(self) -> dict[str, Any]:
        """Serialise for storage and for administrators."""
        return asdict(self)

    def as_public_dict(self) -> dict[str, Any]:
        """Serialise for a non-admin, who has no business seeing the users."""
        data = self.as_dict()
        del data["users"]
        return data


@dataclass(slots=True)
class Access:
    """What one user may see in the panel."""

    presets: list[Preset]
    """The presets offered to this user."""

    restricting: list[Preset] | None
    """The presets that limit this user, or ``None`` when nothing does."""

    @property
    def restricted(self) -> bool:
        """Whether this user sees only part of the installation."""
        return self.restricting is not None

    def allows(self, entry_id: str, ap_id: int, zone_id: int | None) -> bool:
        """Whether this user may see the access point."""
        if self.restricting is None:
            return True
        return any(p.covers(entry_id, ap_id, zone_id) for p in self.restricting)


class PresetStore:
    """Persists every preset of the integration."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Create the store; nothing is read until loaded."""
        self._store: Store[dict[str, Any]] = Store(
            hass, PRESETS_STORAGE_VERSION, PRESETS_STORAGE_KEY
        )
        self._presets: dict[str, Preset] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def async_ensure_loaded(self) -> None:
        """Read the stored presets once, however many entries ask."""
        async with self._lock:
            if self._loaded:
                return
            stored = await self._store.async_load() or {}
            self._presets = {
                item["id"]: Preset(
                    id=item["id"],
                    name=item.get("name") or "",
                    access_points=[str(k) for k in item.get("access_points", [])],
                    zones=[str(k) for k in item.get("zones", [])],
                    users=[str(u) for u in item.get("users", [])],
                )
                for item in stored.get("presets", [])
                if item.get("id")
            }
            self._loaded = True

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"presets": [preset.as_dict() for preset in self._presets.values()]}
        )

    @callback
    def all(self) -> list[Preset]:
        """Every preset, sorted by name."""
        return sorted(self._presets.values(), key=lambda p: p.name.casefold())

    @callback
    def get(self, preset_id: str) -> Preset | None:
        """Return one preset."""
        return self._presets.get(preset_id)

    async def async_save(
        self,
        *,
        preset_id: str | None,
        name: str,
        access_points: list[str],
        zones: list[str],
        users: list[str],
    ) -> Preset:
        """Create a preset, or replace the one with ``preset_id``."""
        preset = Preset(
            id=preset_id or uuid4().hex,
            name=name.strip(),
            access_points=sorted(set(access_points)),
            zones=sorted(set(zones)),
            users=sorted(set(users)),
        )
        self._presets[preset.id] = preset
        await self._async_save()
        return preset

    async def async_delete(self, preset_id: str) -> bool:
        """Delete a preset; whether there was one."""
        if self._presets.pop(preset_id, None) is None:
            return False
        await self._async_save()
        return True

    @callback
    def access_for(self, user: User) -> Access:
        """Work out what ``user`` may see."""
        presets = self.all()
        if user.is_admin:
            return Access(presets=presets, restricting=None)
        assigned = [preset for preset in presets if user.id in preset.users]
        if assigned:
            # Shared views are not offered here: they may well include points
            # this user is meant not to see.
            return Access(presets=assigned, restricting=assigned)
        return Access(
            presets=[preset for preset in presets if not preset.users],
            restricting=None,
        )


async def async_get_presets(hass: HomeAssistant) -> PresetStore:
    """Return the integration's preset store, loading it on first use."""
    store: PresetStore | None = hass.data.get(_DATA_KEY)
    if store is None:
        store = hass.data[_DATA_KEY] = PresetStore(hass)
    await store.async_ensure_loaded()
    return store
