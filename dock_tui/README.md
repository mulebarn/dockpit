# Dockpit

A terminal user interface for managing Docker containers — vintage retro palette, styled after btop/htop.

**Stack:** Python · Textual · docker SDK · Rich

## Features

- htop-style layout: container list + live detail panel + scrolling log
- Live utilization polling (`docker stats`): CPU / MEM per container as bar gauges with percentage, color-coded by load, plus aggregate CPU/MEM in the header
- Automatic update detection via registry digest comparison (`↑` = update available), re-checked on scan and every 60s
- Configurable polling intervals via `DOCKPIT_STATS_INTERVAL` (default: 2s) and `DOCKPIT_REGISTRY_INTERVAL` (default: 60s)
- Update in place: pull latest image → stop → remove → recreate with saved env, ports, volumes, restart policy, and network mode
- Update-all applies pending updates to every flagged container
- Container details include bounded CPU and memory history sparklines

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
| `i`            | Inspect selected container              |
| `r`            | Re-scan containers                      |
| `q`            | Quit                                    |

The table shows Docker health state when a health check is configured; the details panel also shows the restart policy.

Update, update-all, and stop actions show a confirmation dialog. Press `Enter` or `y` to confirm, or `Esc` or `n` to cancel.

If the Docker daemon disconnects, DOCKPIT keeps the last successful container view,
shows `DOCKER UNAVAILABLE`, and lets you retry with `r`. Registry outages are shown
as `registry error` rather than as an up-to-date or local-only result.

Resource history keeps the latest 30 samples per container. The stats poll interval
defaults to 2 seconds, so the visible history covers roughly the latest minute.

## Tests

Headless end-to-end tests run against a fake Docker client:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. python tests/test_app.py
```