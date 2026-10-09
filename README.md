<p align="center">
  <img src="https://www.orbit-os.org/images/vscode/orbit-os-logo.png" width="300" alt="Orbit OS">
</p>

<h1 align="center">Pulse for Orbit OS</h1>

<p align="center"><b>See how your device is doing over time — CPU, memory, temperature, disk and network, as charts in your browser.</b></p>

An [Orbit OS](https://www.orbit-os.org/?ref=github-pulse) app that records how a device is doing and shows it as charts in the device's Launcher. It keeps months of history on the device itself, in a small database that cleans up after itself — no cloud service and no account.

It is also an example of a complete Orbit OS app in Python: it reads the device through the Orbit OS API with the [Orbit OS Python SDK](https://github.com/OrbitOS-org/orbit-os-sdk-python), keeps its data in the app's data folder, and serves its page through the Launcher.

Runs on Raspberry Pi, Arduino UNO Q and other ARM64 devices with Orbit OS (free Community Edition).

## Features

- **Live tiles** — CPU, memory, temperature, disk, network and uptime, as they are now
- **History from one hour to one year** — charts for CPU (all cores or one line per core), memory, temperature, network, disk and load
- **Every app on the device** — its state, and its CPU and memory use over time
- **Months of history in little space** — recent data in full detail, older data as averages, inside a size limit you choose
- **CSV export** of every series in the selected range
- **Settings on the page** — how often to sample, how long to keep the data and how much space to use
- Charts with a table view and keyboard access; light and dark themes

## Install

**From the Orbit OS Store (recommended):** install [Pulse](https://store.orbit-os.org/app/pulse?ref=github-pulse) on your device in one click.

<a href="https://store.orbit-os.org/app/pulse?ref=github-pulse"><img src="https://www.orbit-os.org/images/badges/get-it-on-orbit-os-store@3x.png" width="200" alt="Get it on Orbit OS Store"></a>

**From source — recommended: [Orbit Studio](https://marketplace.visualstudio.com/items?itemName=orbit-os.orbit-studio) (VS Code):**

You need [VS Code](https://code.visualstudio.com/) with the Orbit Studio extension and **[Python](https://www.python.org/downloads/) 3.13** installed.

1. Clone the repository and open the folder in VS Code with the Orbit Studio extension:
   ```bash
   git clone https://github.com/OrbitOS-org/orbit-os-app-pulse
   code orbit-os-app-pulse
   ```
2. In the Orbit sidebar, run **Add / Update SDK** and set your device's IP.
3. Use **Run** to try it live against a device in Developer Mode, then **Build + Deploy** to install the signed `.orb`.

**Without Orbit Studio:** with the SDK in `orbit-os-sdk-python/` and its packages installed in a virtual environment, `python src/Pulse/main.py --host <DEVICE_IP>` runs Pulse on your computer — use Orbit Studio to package and sign the `.orb`.

## Getting started

1. Open the device portal at `http://<DEVICE_IP>`, sign in, and open **Pulse** from the Launcher.
2. The tiles at the top show the device as it is now; the charts fill in as Pulse records.
3. Choose a range, from **1 hour** to **1 year**, to look back. **Download CSV** exports what you see.
4. In **Settings**, choose how often Pulse samples, how long it keeps the data and how much space it may use.

## How it works

Pulse reads the device every 30 seconds through the Orbit OS API and writes the values to a small database in its data folder. It keeps every sample for 48 hours, 5-minute averages for 30 days and hourly averages for 2 years, and it stays under a size limit (200 MB by default). All of these can be changed in **Settings**.

Where each value comes from, how the data is stored and cleaned up, and the map of the code are in [docs/how-it-works.md](docs/how-it-works.md).

## Development (Orbit Studio)

This project follows the [Orbit Studio](https://marketplace.visualstudio.com/items?itemName=orbit-os.orbit-studio) layout for Python apps:

| Path | What |
|---|---|
| `src/Pulse/` | app source — `main.py` (start-up, sampling loop, Launcher registration), `metadata.json` (manifest & permissions) |
| `src/Pulse/lib/` | reading the device, the database, the settings and the page's server |
| `src/Pulse/web/` | the page: HTML, CSS and JavaScript, with no external libraries |
| `src/Pulse/orb/icon.svg` | launcher / Store icon |
| `tests/` | unit tests (standard library only) |
| `orbit.project.json` | Orbit Studio project settings |

- **Recommended workflow:** open the folder in VS Code with Orbit Studio, **Add / Update SDK** (downloads the SDK into `orbit-os-sdk-python/` and creates `.venv`; neither is in the repository), then **Run** to develop against a device in Developer Mode, or **Build + Deploy** to install the `.orb`.
- With **Run**, the page is served on your computer at `http://127.0.0.1:<port>`, with the port shown in the log, and the data goes to `.pulse-data/`. The CPU and memory use of each app is measured only when Pulse runs on the device.
- Development TLS certificates live in `certs/grpc/` and are never committed.
- Tests: `python -m unittest discover -s tests`, with the Python of `.venv`.

## Security

- The page listens on `127.0.0.1` only and is reached through the Orbit OS Launcher, at `http://<DEVICE_IP>/pulse`, behind the device login. It is not reachable directly from the network.
- The app takes a port in the reserved range 50000–60000 (starting at 50480) and moves to the next one when a port is taken.
- Everything stays on the device: Pulse needs no internet access and no account. Its history and its settings are in the app's data folder; they are kept across restarts and updates and removed when the app is uninstalled.
- Pulse does not start any other program: it only calls the API and reads system files.
- Permissions used: `SystemService`, `PackageManagerService`, `AppHubService`.

## Links

[App in the Store](https://store.orbit-os.org/app/pulse?ref=github-pulse) · [Orbit OS](https://www.orbit-os.org/?ref=github-pulse) · [Getting started](https://www.orbit-os.org/getting_started.html?ref=github-pulse) · [SDK reference](https://www.orbit-os.org/api-reference.html?ref=github-pulse) · [Forum](https://forum.orbit-os.org/?ref=github-pulse) · info@orbit-os.org

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
