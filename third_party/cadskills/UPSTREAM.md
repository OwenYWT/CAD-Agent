# CAD Skills vendored runtime

This directory vendors the runtime files for all 11 public skills from
[`earthtojake/text-to-cad`](https://github.com/earthtojake/text-to-cad).

- Upstream release: `0.3.9`
- Upstream commit: `fdbb4b4fb62d95ae298cfe9a46fdc7092bdaf423`
- License: MIT; see `LICENSE` and the per-skill license files.
- Imported paths: `skills/**`

The files are pinned so production builds do not fetch or execute a moving
`main` branch. Application-owned adapters live under `backend/app/capabilities`;
do not add product-specific changes directly to this directory.

To refresh intentionally, download the chosen upstream commit, replace
`skills/**`, update this file, and run both the upstream smoke tests and this
project's backend/frontend test suites.
