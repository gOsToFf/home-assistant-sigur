"""The schema library, whichever one this Home Assistant ships.

Home Assistant 2026.10 replaced voluptuous with probatio, a reimplementation
with the same public API, and stopped depending on voluptuous itself. It is
still installed there, but only because two of Home Assistant's own
dependencies pull it in, which nothing promises to keep; and Home Assistant's
type hints now accept only probatio schemas. Releases before 2026.10 have only
voluptuous.

Schemas are built from whichever library Home Assistant itself uses, so that
they mix with its own validators (`config_validation`, selectors, the device
trigger base schema) instead of being a foreign type to them.
"""

from __future__ import annotations

try:
    import probatio as vol
except ImportError:  # Home Assistant before 2026.10
    import voluptuous as vol  # type: ignore[no-redef]

__all__ = ["vol"]
