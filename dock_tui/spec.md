# DOCKPIT Spec

Status: **In progress**

DOCKPIT is a Textual terminal UI for monitoring and updating Docker containers. This document is the phased implementation roadmap and the handoff point for future sessions.

## Goals

- Make container updates recoverable and predictable.
- Keep live monitoring responsive while Docker work runs in the background.
- Preserve supported container configuration during recreation.
- Make update progress, partial failure, and retry behavior visible.
- Keep the project small, testable, and usable from a clean checkout.
- Make operational priority legible at a glance: attention states first, current resource load second, metadata and history on demand.

## Non-goals for the initial phases

- A complete Docker orchestration replacement.
- Automatic mutation of Compose files.
- Interactive shell support, advanced log exploration, or image/volume cleanup.
- A broad architecture rewrite before the current behavior is reliable.

## Current Architecture

- `app.py` owns the Textual UI, Docker client, background workers, polling, update detection, container recreation, and rendering.
- `app.tcss` owns presentation and layout.
- `tests/test_app.py` provides a fake Docker client and headless Textual tests.
- `README.md` documents usage and the current test command.

Important symbols include `_scan_worker()`, `_populate_table()`, `_stats_worker()`, `_update_check_worker()`, `_do_update()`, `_build_run_kwargs()`, `_parse_env()`, `_parse_ports()`, and `_parse_binds()`.

## Invariants

1. A failed update must not silently leave the user without the original service.
2. An update is not fully successful if required network restoration fails.
3. Background workers must not apply stale results to a newer scan.
4. Widgets must not be mutated after application shutdown.
5. The UI must distinguish successful, degraded, failed, and unavailable states.
6. Supported configuration must be preserved or rejected before destructive changes.

## Primary Risk

The current `_do_update()` sequence pulls the image, stops the original container, removes it, and then recreates it. If recreation fails, the original container is gone. `_build_run_kwargs()` also reconstructs only part of Docker's configuration, so an update can silently change runtime behavior.

This risk takes priority over visual polish and new feature work.

## Phase 1: Worker and State Hardening

**Status:** Complete

### Scope

Make scans, stats polling, registry checks, and updates consistent when operations overlap, fail, or run during shutdown.

### Acceptance criteria

- Scan results cannot overwrite a newer scan with stale container references.
- Stats and registry workers always clear their in-flight flags, including exceptions and shutdown.
- The same logical container cannot receive overlapping updates.
- Rescans preserve selection when the container still exists and clear state for removed containers.
- Worker callbacks do not mutate widgets after unmount.
- Update and polling state is keyed and cleared consistently when a container is recreated.

### Verification

Add focused fake-client or headless tests for overlapping scans, worker exceptions, shutdown, duplicate update requests, and selection after rescan. Run the documented test command.

Current progress:

- Added scan generation tokens so stale scan, stats, and registry callbacks are ignored.
- Added worker completion cleanup for stats and registry in-flight flags.
- Cleared polling flags on unmount and pruned stale metrics after rescans.
- Added a focused stale-result regression test in `tests/test_app.py`.
- Added shutdown regression coverage for polling flags.
- Added duplicate-update guards at both single-update and update-all worker start boundaries.
- Added headless coverage for retaining selection after rescan and clearing it when the selected container is removed.
- Added injected update-check and stats worker exception coverage, including stats-loop cleanup after unexpected metadata failures.
- `python3 -m py_compile app.py tests/test_app.py` passes.
- `PYTHONPATH=. .venv/bin/python tests/test_app.py` passes.
- The README now documents isolated environment setup and the working test command.

Remaining:

- Future hardening can expand coverage as additional worker failure modes are encountered.

### Relevant files

`app.py`, especially `_scan_worker()`, `_populate_table()`, `_stats_worker()`, `_update_check_worker()`, `_updating`, `_stats_in_flight`, and `_check_in_flight`; `tests/test_app.py`.

## Phase 2: Update Transaction Safety

**Status:** In progress

### Scope

Replace the destructive update flow with a recoverable strategy and explicit failure handling.

### Acceptance criteria

- All required metadata is captured before mutation.
- Pull failure behavior is explicit, logged, and tested.
- Recreate failure produces a visible failed state and a deterministic recovery path.
- The original container is not removed until the replacement strategy is known to be viable, or a documented rollback path exists.
- Network restoration failures are reported as degraded, not fully successful.
- Temporary containers, partial replacements, and other failure artifacts are cleaned up deterministically.
- Successful updates preserve every configuration field defined as supported in Phase 4.
- Update-all reports individual success, degraded, and failed results.

### Verification

Extend the fake Docker client with failures for pull, stop, remove, recreate, and network attachment. Test that the original container remains recoverable after recreation failure.

Current progress:

- Recreation arguments are prepared before the original container is stopped or removed.
- The original image ID is captured before pulling the update.
- If recreation fails after removal, DOCKPIT attempts recovery under the original container name using the captured image.
- Added deterministic fake recreation failure coverage in `tests/test_app.py`.
- Added pull, stop, remove, and network attachment failure coverage.
- Network restoration failure now returns a non-success result and is logged as incomplete restoration.
- Added preservation for user, hostname, health check, TTY/stdin, privilege, capabilities, devices, DNS, extra hosts, and tmpfs settings.
- Added focused runtime-configuration assertions for `_build_run_kwargs()`.
- Added durable `recovered`, `degraded`, and `failed` statuses that survive replacement rescans and appear in the table/details state.
- Update-all now logs an individual result for every target container.
- `PYTHONPATH=. .venv/bin/python tests/test_app.py` passes.

Remaining:

- Reconcile this phase to complete after a final review of cleanup and rollback behavior.

### Relevant files

`app.py`, especially `_do_update()`, `_update_worker()`, `_update_all_worker()`, and `_build_run_kwargs()`; `tests/test_app.py`.

## Phase 3: User-Facing Update State

**Status:** In progress

### Scope

Make update progress and outcomes visible in the table, details panel, header, and log.

### Acceptance criteria

- Each update exposes stable states: pending, pulling, stopping, recreating, restoring networks, succeeded, degraded, and failed.
- The selected container's details show the current state and actionable error information.
- Update-all reports per-container results instead of only an aggregate count.
- Failed and degraded results remain visible after a rescan.
- Users can retry a failed update without restarting the application.
- Logs identify the container name or ID and operation stage.
- The UI remains readable at supported narrow terminal sizes.

### Verification

Add headless UI tests for state transitions, retry behavior, update-all results, and error rendering. Add only the CSS needed for clear state styling.

### Relevant files

`app.py`, `app.tcss`, and `tests/test_app.py`.

Current progress:

- Added background start, stop, and restart actions for the selected container.
- Added key bindings and headless coverage for all three lifecycle actions.
- Durable recovered, degraded, and failed update outcomes are already displayed in the table/details state.
- Added a background recent-logs action with bounded output and UTF-8-safe decoding.
- Added regression coverage for log byte decoding and empty-line handling.
- Added uppercase `L` streamed log-follow mode with cooperative toggle cancellation.
- Added finite-stream headless coverage for follow mode.
- Added a reversible case-insensitive log filter with `f` focus and history replay.
- Added direct log-filter matching coverage.
- Added a visible container filter with `/` focus, matching name/image/status and reversible filtering.
- Added focused filter matching coverage.
- Added staged update states for pulling, stopping, removing, recreating, and network restoration.
- Added regression coverage for the staged-state display definitions.
- Added confirmation dialogs for update, update-all, and stop actions.
- Added keyboard and headless coverage for confirm and cancel paths.
- Added health indicators to the container table and health/restart-policy fields to container details.
- Added metadata tests for healthy and unhealthy states.
- Added read-only `i` inspect action that formats selected-container metadata in the log pane.
- Added direct JSON-formatting coverage for inspect output.

### UX pass: operational hierarchy

**Status:** In progress

The current UI hierarchy is ordered by impact and implementation cost:

1. Selected-row contrast, explicit health/update labels, CPU/MEM micro-bars, and update highlighting.
2. Timestamped Recent Actions, grouped shortcut labels, and `!` Attention Mode for stopped, unhealthy, update-ready, failed, or high-load containers.
3. Details enrichment with image reference, update state, age, start time, networks, volume count, and rolling resource trends.
4. Derived service-oriented filtering through name/image matching. True collapsible group-header rows remain deferred because the current table's actionable identity is a Docker container ID; introducing non-container rows would require a selection and action-model change.

The implementation keeps Docker container identity as the single source of truth, with visual priority derived from health, lifecycle, update, failure, and resource state. Background workers continue to update the derived view through the existing generation and in-flight guards.
- Added confirmed pause and remove lifecycle actions with headless integration coverage.
- Added bounded CPU and memory resource history with compact detail-panel sparklines.
- Added direct coverage for history pruning and per-container update-all results.

Remaining:

- Richer responsive views remain deferred because the installed Textual version rejects the attempted media-query syntax; narrow-terminal behavior still needs manual verification.

## Phase 4: Configuration Compatibility

**Status:** In progress

### Scope

Define and test the Docker configuration that recreation supports. Unsupported configuration must be preserved, rejected before removal, or clearly reported.

### Minimum supported cases

- Image, name, command, entrypoint, working directory, and environment, including empty values.
- Labels, restart policy, and supported resource settings.
- Named volumes and bind mounts, including paths containing colons and mount options.
- Multiple published ports with host IPs and multiple bindings.
- Multiple networks with aliases.
- Host networking and `container:` networking behavior.
- Health checks, user, hostname, TTY/stdin settings, and other supported runtime fields.
- Missing or malformed Docker metadata without an unhandled crash.
- Compose-managed labels and network configuration.

### Acceptance criteria

- The supported subset is documented in this file and in code tests.
- `_build_run_kwargs()` has focused tests independent of Textual.
- `_parse_env()`, `_parse_ports()`, and `_parse_binds()` have edge-case coverage.
- Unsupported settings are detected before destructive changes.
- Configuration mismatches are shown to the user.

Current progress:

- Common runtime settings are now forwarded by `_build_run_kwargs()` and covered by direct tests.
- Common resource limits and structured named-volume/bind-mount metadata are now forwarded by `_build_run_kwargs()`.
- Read-only mount modes are preserved from Docker inspect data.
- Added direct resource and structured-mount compatibility tests.
- Added preflight detection for unsupported runtime settings before pull, stop, or removal.
- Added regression coverage proving unsupported settings leave the original container intact.
- Corrected false positives for normal Docker inspect metadata such as cgroup namespace, runtime, read-only paths, masked paths, and console size.
- Preserved the recreatable runtime fields shown by Docker inspect instead of rejecting ordinary containers.
- Stopped forwarding inspect-only `ConsoleSize` to Docker SDK `containers.run()`.
- Added regression coverage for the invalid-keyword case.
- Preserved host IPs and multiple bindings when rebuilding published ports.
- Added direct coverage for host-IP and multiple-port binding preservation.
- Added edge-case coverage for colon-containing bind sources and mount options.
- Added direct coverage for host and `container:` network modes.

Remaining:

- Expand the unsupported-setting list as new Docker configurations are encountered.

### Verification

Add fixtures to `tests/test_app.py` for each supported case and failure mode. Run the full documented test command.

## Phase 5: Verification and Operations

**Status:** In progress

### Scope

Harden the workflow for regular use and keep documentation aligned with behavior.

### Acceptance criteria

- The documented test command passes from a clean environment.
- Tests cover success, failure, shutdown, concurrency, and partial restoration.
- Docker daemon disconnects produce recoverable UI states.
- Registry failures are distinct from “up to date” and “local only.”
- No stale success state survives a failed or interrupted operation.
- README usage, key bindings, failure behavior, and prerequisites match the implementation.
- Polling and registry-check intervals are configurable or have documented defaults.

### Verification

Run focused tests for each changed slice, then run the complete documented test command. Check the app manually at a normal and narrow terminal size when UI behavior changes.

### Relevant files

`app.py`, `app.tcss`, `tests/test_app.py`, `README.md`, and `requirements.txt` when dependencies change.

Current progress:

- The documented virtualenv setup and test command pass.
- The README key map matches the implemented controls.
- Log history is bounded and follow mode stops during shutdown.
- Docker daemon list failures preserve the last successful container view and expose a retryable unavailable state.
- Registry lookup failures are reported as `registry-error`, distinct from current, update-available, and local-only states.
- Added fake-client coverage for daemon disconnects and registry outages.
- Stats and registry polling intervals are configurable through `DOCKPIT_STATS_INTERVAL` and `DOCKPIT_REGISTRY_INTERVAL`, defaulting to 2 and 60 seconds.
- Resource history and per-container update-all results are covered by headless tests.
- The header shows a live elapsed timer while Docker stats collection is in flight.
- Stats collection reads the first decoded item from a short-lived Docker stats stream and closes it deterministically.
- `PYTHONPATH=. .venv/bin/python tests/test_app.py` passes.

Remaining:

- Continue broader operational verification as new Docker failure modes are encountered.

## Later Feature Backlog

These are intentionally after the reliability phases:

- Sorting.
- Expanded inspect view for mounts, networks, health, and resources.
- Compose-aware updates using Compose as the source of truth.
- Interactive `docker exec` support.
- Resource history, sparklines, and richer responsive layouts.
- Image, volume, and network cleanup tools.

## Session Status Template

Copy this section into the next session update:

```text
Phase: <number and name>
Status: Not started | In progress | Blocked | Complete

Completed:
- ...

Remaining:
- ...

Files changed:
- ...

Validation:
- Focused command/test: ...
- Full documented test command: ...

Known risks or decisions:
- ...

Next smallest task:
- ...
```

A phase is complete only when its acceptance criteria and focused verification pass.

## Decision Log

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-09-06 | Prioritize update safety and worker consistency before visual polish. | Reliability has the highest operational risk. |
| 2026-09-06 | Keep the existing single-process architecture initially. | Stabilize behavior before introducing a broad refactor. |
| 2026-09-06 | Use the existing fake Docker client and Textual headless tests. | Provides deterministic, low-dependency validation. |
| 2026-09-06 | Treat Compose-managed containers as an important compatibility case. | Recreating them incorrectly can break service topology and ownership. |
