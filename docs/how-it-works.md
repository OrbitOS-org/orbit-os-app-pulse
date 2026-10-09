# How Pulse works

Technical notes for developers: where the values come from, how they are recorded and cleaned up, what Pulse keeps on the device, and where things are in the code. For what the app does and how to install it, see the [README](../README.md).

## Where the values come from

| Source | What |
|---|---|
| Orbit OS API: `SystemService` | Memory, load, uptime, CPU frequency, SoC temperature, disk `/`, disk and network counters, device info |
| Orbit OS API: `PackageManagerService` | Installed apps and their processes |
| `/proc` and `/sys` (on the device) | CPU over the exact interval, every core, thermal zone, network interface and mounted disk, swap, CPU and memory of each app |

When Pulse runs on your computer during development, the CPU and memory of each app are not measured: they need `/proc` on the device.

## Recording

- **Sampling:** every 30 seconds by default (10 to 300).
- **Storage:** samples are written to `pulse.db` (SQLite) once a minute, to spare the SD card. If the app is stopped abruptly, at most the last minute is lost.
- **Averages:** the 5-minute and hourly averages are computed as each period closes. On long ranges the charts show the minimum and the maximum around the average.
- **Retention:** every sample is kept for 48 hours, 5-minute averages for 30 days and hourly averages for 2 years.
- **Clean-up:** every hour Pulse deletes what is older than the retention times. If the database is still over the size limit (200 MB by default), it removes the oldest data first.
- **One recorder:** a lock file in the data folder lets only one Pulse process record at a time.

Sampling, retention and the size limit can be changed in the Settings tab of the page.

## What Pulse keeps on the device

Three files, in the app's data folder:

| File | What |
|---|---|
| `pulse.db` | the history |
| `config.json` | the recording settings |
| `pulse.lock` | makes sure only one Pulse records at a time |

They are kept across app and device restarts, app updates and Orbit OS updates, and removed when the app is uninstalled.

## The page

The page asks the app for its data through a small JSON API on the same address (`api/now`, `api/series`, `api/apps`, `api/stats`, `api/config`, `api/export.csv`). It uses no external libraries. Every chart has a table view and can be read with the keyboard; the theme follows the browser.

## Code map

```
src/Pulse/
  main.py            start-up, sampling loop, Launcher registration
  metadata.json      name, version, permissions
  lib/collector.py   reads the API, /proc and /sys
  lib/store.py       SQLite: samples, averages, retention, queries
  lib/server.py      page and JSON API (127.0.0.1 only)
  lib/config.py      recording settings (config.json)
  lib/instances.py   one recorder at a time (pulse.lock)
  web/               the page: HTML, CSS and JavaScript
  orb/icon.svg       Launcher and Store icon
tests/               unit tests
```

The page's icon, `web/favicon.svg`, is a copy of `orb/icon.svg`. Keep the two the same.
