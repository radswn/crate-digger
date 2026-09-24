# Repository guidance

Crate Digger is a Python 3.14 application for music discovery, a local collection dashboard, and DJ library workflows. Use the relevant README section and nearby code and tests for feature details; avoid loading the whole README for small changes.

## Working in this repo

- Use `uv` for Python commands. The package code is in `src/crate_digger/`, and tests are in `tests/`.
- Keep changes focused on the requested behavior. Preserve unrelated work in the working tree.
- For behavior changes, run the relevant tests. For a broader offline check, use `uv run pytest -m "not spotify_live"`; use `make lint` and `make typecheck` when the change warrants them.
- `make test` and `make check` include tests marked `spotify_live`. One live test adds a track to a configured Spotify playlist before removing it. Run live tests only when the user explicitly requests live verification.

## Real data and external effects

- Use fixtures, temporary files or database copies, and mocked Spotify or Telegram clients for automated verification.
- Do not use a real Spotify playlist, send a Telegram message, write to a live Rekordbox or Traktor collection, edit audio files, or mutate the user's local SQLite state merely to test a change. Do so when the user's task explicitly calls for that real-world action.
- Treat `.env`, `config.toml`, `.spotipy_cache/`, `.crate_digger_state/`, and `exports/` as personal configuration or data. Do not print credentials or include them in test fixtures, logs, or reports.
- Prefer available preview or dry-run paths before library or playlist writes. Check the target and expected effect before applying a requested real-data change.

At the end of a task, state what changed, what was verified, and any manual or live check still needed. `docs/` is ignored by Git, so do not assume a file placed there will be committed.
