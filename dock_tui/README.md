# Dockpit

A terminal user interface for managing Docker containers — vintage retro palette, styled after btop/htop.

**Stack:** Python · Textual · docker SDK · Rich

## Features

- htop-style layout: container list + live detail panel + timestamped Recent Actions feed
- Live utilization polling (`docker stats`): CPU / MEM per container as bar gauges with percentage, color-coded by load, plus aggregate CPU/MEM in the header
- Responsive status/update columns use colored semantic icons when constrained (`✓`, `▲`, `↻`, `?`) and expand to labels such as `✓ Current` and `▲ Update Available` when space allows
- Stats use Docker's decoded streaming API for prompt first-sample delivery, then close each short-lived stream
- Automatic update detection via registry digest comparison (`UPDATE AVAILABLE` = update available), re-checked on scan and every 60s
- Attention Mode (`!`) narrows the table to stopped, unhealthy, update-ready, failed, or high-load containers
- Configurable polling intervals via `DOCKPIT_STATS_INTERVAL` (default: 2s) and `DOCKPIT_REGISTRY_INTERVAL` (default: 60s), plus image-result caching via `DOCKPIT_REGISTRY_CACHE_TTL` (default: 15m)
- Update in place: pull latest image → stop → remove → recreate with saved env, ports, volumes, restart policy, and network mode
- Update-all applies pending updates to every flagged container
- Compose-managed projects are grouped by `com.docker.compose.project`; updating one service updates the project with `docker compose pull`, `docker compose up -d`, then `docker image prune -f`
- ctop-style container details include live CPU/MEM gauges, rolling history and peaks, image digest, lifecycle/exit information, restart count, health failures, networks and IPs, ports, commands, mounts, resource limits, and update state

## Usage

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Make sure the Docker daemon is running, then:

```bash
python app.py
```

## Keys

| Key            | Action                                  |
| -------------- | --------------------------------------- |
| `u`            | Update selected container               |
| `U`            | Update all containers with updates      |
| `s`            | Start selected container                |
| `d`            | Stop selected container                 |
| `p`            | Pause selected container                |
| `R`            | Restart selected container              |
| `x`            | Remove selected container               |
| `l`            | Show recent selected-container logs    |
| `L`            | Follow selected-container logs        |
| `/`            | Focus container filter                 |
| `f`            | Focus log filter                       |
| `!`            | Toggle Attention Mode                  |
| `i`            | Inspect selected container              |
| `r`            | Re-scan containers                      |
| `q`            | Quit                                    |

The table shows color-coded CPU and memory micro-bars, explicit health state, and update state. The selected row uses a high-contrast inverse treatment. The details panel also shows image, restart policy, age, start time, networks, volume count, and resource history.

The footer keeps only the high-frequency actions visible. Press `ctrl+p` to open the Command Palette for the complete action set, including lifecycle actions, updates, Attention Mode, group commands, log views, filters, inspection, refresh, and registry settings. Palette search accepts descriptive keywords and aliases such as `start`, `upgrade`, `attention`, `tail`, and `settings`.

Compose grouping uses Docker's project labels. Collapse groups to see one row per Compose project, with the service count in brackets; expand groups to see each container. Project-level updates require Docker Compose labels for the project name, working directory, and config files. Containers without that metadata continue to use the individual inspect-and-recreate update path.

Update, update-all, and stop actions show a confirmation dialog. Press `Enter` or `y` to confirm, or `Esc` or `n` to cancel.

If the Docker daemon disconnects, DOCKPIT keeps the last successful container view,
shows `DOCKER UNAVAILABLE`, and lets you retry with `r`.

## Docker Hub Rate Limits

Registry update checks use the image reference as their cache key, so containers sharing an image/tag produce one registry lookup. Results are cached for 15 minutes by default and remain visible during that period instead of being re-requested on every UI refresh. Adjust the cache with `DOCKPIT_REGISTRY_CACHE_TTL`; during testing, a 15-30 minute value is recommended, while `DOCKPIT_REGISTRY_INTERVAL` controls how often DockPit considers a new check.

Docker Hub and other registries may respond with HTTP 429 when too many anonymous or authenticated requests arrive in a short period. DockPit treats 429 as a rate-limit condition, pauses registry lookups, and applies exponential backoff up to 30 minutes. It shows a single status warning such as `Registry rate limited - retrying in 2 minutes` and suppresses repeated warning spam. Cached update results continue to appear in the table and details pane while the registry is cooling down. If no cached result exists, the state is shown as unknown rather than incorrectly reporting the image as current.

For rapid update-check testing, use a longer registry interval or cached results between manual refreshes, then wait for the cache TTL or change `DOCKPIT_REGISTRY_CACHE_TTL` before expecting another registry request. Temporary registry failures remain visible as unavailable/unknown state and do not block container lifecycle operations.

Resource history keeps the latest 30 samples per container. The stats poll interval
defaults to 2 seconds, so the visible history covers roughly the latest minute.
While Docker is collecting a sample, the header shows an elapsed `STATS` timer so a
slow first sample is visible rather than appearing as an unresponsive UI.

## Tests

Headless end-to-end tests run against a fake Docker client:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. python tests/test_app.py
```