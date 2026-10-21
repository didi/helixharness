# Third-party notices

The platform's own code is licensed under Apache-2.0. Bundled third-party assets retain their original licenses.

## HTMX 1.9.12

- Project: https://github.com/bigskysoftware/htmx
- Distributed file: `web_ui/static/vendor/htmx.min.js`
- Upstream reference: https://github.com/bigskysoftware/htmx/tree/v1.9.12
- License: Zero-Clause BSD, reproduced in [licenses/htmx-1.9.12.txt](licenses/htmx-1.9.12.txt).

## Tailwind CSS

- Project: https://github.com/tailwindlabs/tailwindcss
- Distributed generated stylesheet: `web_ui/static/css/tailwind.css`
- Source: `web_ui/assets/tailwind.css`, `tailwind.config.js`, and Web templates.
- License: MIT, reproduced in [licenses/tailwindcss-3.4.17.txt](licenses/tailwindcss-3.4.17.txt).
- Rebuild with the documented Tailwind CSS 3.4.17 command in the root contribution guide. The existing stylesheet's generating version was not recorded; the notice does not assert a byte-identical rebuild.

Dependency packages installed from `pyproject.toml` are not vendored in this repository and retain their respective licenses.
