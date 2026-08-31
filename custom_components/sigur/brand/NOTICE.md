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

The canonical home for these files is the
[home-assistant/brands](https://github.com/home-assistant/brands) repository,
and it is the only home that has any effect. Both Home Assistant and HACS
resolve an integration's images from `brands.home-assistant.io/_/<domain>/`,
which serves a generic placeholder for every domain that is not registered
there; `sigur` is not, so that placeholder is what a user sees. Nothing reads
the copies in this directory - they are kept here so that the registration can
be raised from the repository itself, not because they are served from it.
