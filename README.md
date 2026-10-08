<p align="center">
  <img src="src/Pulse/orb/icon.svg" alt="Pulse" width="96" height="96">
</p>

# Pulse

Pulse records how an Orbit OS device is doing over time — CPU, memory, temperature, disk and network — and shows it as charts in the device's Launcher. It keeps months of history on the device itself, in a small database that cleans up after itself.

It is also an example of a complete Orbit OS app in Python: it reads the device through the Orbit OS API, keeps its data in the app's data folder, and serves its page through the Launcher.

## Features

- **Live tiles:** CPU, memory, temperature, disk, network and uptime, as they are now.
- **History:** 1 hour, 6 hours, 24 hours, 7 days, 30 days or 1 year, with the minimum and maximum shown around the average on long ranges.
- **Charts:**
  - CPU (all cores, or one line per core) and memory, with swap when the device has it.
  - Temperature, for every sensor.
  - Network (all interfaces, or one line per interface).
  - Disk activity and disk space for every mounted disk.
  - Load average.
- **Apps:** every installed app with its state, and its CPU and memory use over time.
- **Long-term recording:**
  - Every sample is kept for 48 hours.
  - 5-minute averages are kept for 30 days, and hourly averages for 2 years.
  - The database has a size limit (200 MB by default).
  - All of these can be changed on the page.
- **CSV export** of the selected range.
- Every chart has a table view and can be read with the keyboard.
- Light and dark themes, following the browser.

## How it works

| Where the values come from | What |
|---|---|
| Orbit OS API: `SystemService` | Memory, load, uptime, CPU frequency, SoC temperature, disk `/`, disk and network counters, device info |
| Orbit OS API: `PackageManagerService` | Installed apps and their processes |
| `/proc` and `/sys` (on the device) | CPU over the exact interval, every core, thermal zone, network interface and mounted disk, swap, CPU and memory per app |

- **Sampling:** every 30 seconds by default (10–300).
- **Storage:** samples are written to `pulse.db` (SQLite) once a minute, to spare the SD card. If the app is stopped abruptly, at most the last minute is lost.
- **Averages:** the 5-minute and hourly averages are computed as each period closes.
- **Clean-up:** every hour Pulse deletes what is older than the retention times. If the database is still over the size limit, it removes the oldest data first.
- **No other programs:** Pulse does not start any other program. It only reads files and calls the API.

## Network and security

- **No network ports.** The page listens on `127.0.0.1` only, on a free port between 50000 and 60000.
- **Behind the device login.** Pulse registers the page with the Orbit OS Launcher, which serves it at `http://<device>/pulse`.
- **Data stays on the device.** Pulse needs no internet access and no account.
- **Permissions:** `SystemService`, `PackageManagerService` (apps and their processes) and `AppHubService` (to register the page with the Launcher).

## Data on the device

Pulse keeps two files in its data folder on the device:

- `pulse.db`: the history.
- `config.json`: the recording settings.

| Event | Data |
|---|---|
| App or device restart, app update, Orbit OS update | kept |
| App uninstalled | deleted |

## Development

You need Python 3.13 and [Orbit Studio](https://www.orbit-os.org/) for VS Code.

1. Clone this repository and open the folder in VS Code.
2. In Orbit Studio, run **Add / Update SDK**. It downloads the Python SDK (version in `orbit.project.json`) and creates `.venv`. The SDK is not part of this repository.
3. **Run** starts Pulse on your computer against a device in Developer Mode. The page opens at `http://127.0.0.1:50480/` on your computer, and the data goes to `.pulse-data/`. You can also start it yourself:

   ```bash
   .venv/Scripts/python src/Pulse/main.py --host <device address>   # Windows
   .venv/bin/python src/Pulse/main.py --host <device address>       # Linux / macOS
   ```

   In this mode the per-app CPU and memory are not measured; they need `/proc` on the device.

4. **Build ORB**, then **Deploy**, installs it on the device. It shows up in the Launcher as Pulse.

**Tests** (standard library only):

```bash
.venv/Scripts/python -m unittest discover -s tests
```

**Project layout:**

```
src/Pulse/
  main.py            start-up, sampling loop, Launcher registration
  metadata.json      name, version, permissions
  lib/collector.py   reads the API, /proc and /sys
  lib/store.py       SQLite: samples, averages, retention, queries
  lib/server.py      page and JSON API (127.0.0.1 only)
  lib/config.py      recording settings (config.json)
  web/               the page: HTML, CSS and JavaScript, no libraries
  orb/icon.svg       Launcher and Store icon
tests/               unit tests
```

The page's icon, `web/favicon.svg`, is a copy of `orb/icon.svg`. Keep the two the same.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
