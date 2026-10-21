# HelixHarness public repository

This repository contains the `helixharness/` plugin and the optional `helixharness-platform/` service.
Read the component's `AGENTS.md` before changing it. Keep their runtime data separate.

- The root marketplace installs `./helixharness`; the nested marketplace also supports standalone plugin copies.
- Use synthetic examples. Never read, copy or publish credentials, `.env` files, private conversations or runtime output.
- Preserve unrelated changes and local evidence in ignored `output/` directories.
- Keep English and Chinese entry documentation consistent. Public project code uses Apache-2.0; retain third-party notices.
- Validate behavior changes with the relevant component tests and validate public file links after moving documentation.
- Do not commit, push, create a remote repository, publish, or deploy unless the user explicitly requests it.
