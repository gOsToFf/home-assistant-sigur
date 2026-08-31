# Brand assets

`icon.png` and `logo.png` reproduce the Sigur brand mark and wordmark, taken
from the vendor's own public website assets (`favicon.svg` and the site header
logo) and rendered to PNG at the sizes Home Assistant expects. The wordmark is
recoloured from white to the brand colour `#FF5A00` so that it stays legible on
a light background; nothing else about either image is altered.

They are used here to identify the system this integration talks to. **Sigur is
a trademark of «Промавтоматика».** This integration is unofficial, is not
affiliated with or endorsed by them, and the README says so. If the trademark
holder asks for these assets to be removed, replace them with a neutral mark —
nothing in the integration depends on their content.

This directory is where Home Assistant itself looks. Since 2026.3 a custom
integration ships its brand images with its own code, in exactly this layout,
and they take priority over the brands CDN with no further configuration - see
the [Brands Proxy API announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api).
The `custom_integrations` folder of the
[home-assistant/brands](https://github.com/home-assistant/brands) repository is
marked legacy for that reason, so registering `sigur` there is no longer the
way to be seen inside Home Assistant.

Two things this does not cover:

- Home Assistant older than 2026.3 has no local lookup and falls back to the
  CDN, which serves a placeholder for an unregistered domain. `hacs.json`
  declares 2026.2.0 as the minimum, so that window exists.
- HACS builds the CDN URL itself (`hacs/update.py`), so its own screens show
  the placeholder whatever this directory holds. Only a brands registration
  changes what HACS displays.
