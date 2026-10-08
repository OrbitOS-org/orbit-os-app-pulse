# Pulse

Orbit Python app.

- **Settings** — `orbit.project.json` at the repo root (device address, build paths).
- **SDK** — `./orbit-os-sdk-python` at **v26.1.0**. Refresh with **Orbit: Add SDK** if you replace it.

- **Launcher icon (fixed path)** — `src/Pulse/orb/icon.svg`.
- **TLS** — dev files in `certs/grpc/` (not committed). Replace with real certs for production.
- **Python** — the extension creates a `.venv` with Python 3.13 via `uv` and installs the SDK in editable mode. If `uv` is not on your PATH, run `uv venv --python 3.13 && uv pip install -e ./orbit-os-sdk-python` manually.
- **Build & deploy** — Orbit side bar, or **Orbit: Build ORB** and **Orbit: Deploy to device**.

**From the project name:** on-device id `sdk.dev.pulse` · app folder `src/Pulse`
