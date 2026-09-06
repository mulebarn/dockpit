# Dock TUI

A terminal user interface for managing Docker containers — Gruvbox Dark retro palette, styled after htop.

**Stack:** Python · Textual · docker SDK · Rich

## Features

- htop-style layout: container list + live detail panel + scrolling log
- Live utilization polling (`docker stats`): CPU / MEM per container, color-coded by load, plus aggregate CPU/MEM in the header
- Automatic update detection via registry digest comparison (`↑` = update available), re-checked on scan and every 60s
- Update in place: pull latest image → stop → remove → recreate with saved env, ports, volumes, restart policy, and network mode
- Update-all applies pending updates to every flagged container

## Usage

```bash
pip install textual docker rich
```

Make sure the Docker daemon is running, then:

```bash
python app.py
```

## Keys

| Key            | Action                                  |
| -------------- | --------------------------------------- |
| `u` or `ENTER` | Update selected container               |
| `U`            | Update all containers with updates      |
| `r`            | Re-scan containers                      |
| `q`            | Quit                                    |

## Tests

Headless end-to-end tests run against a fake Docker client:

```bash
pip install textual docker rich
python tests/test_app.py
```