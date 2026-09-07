"""DOCKPIT - a btop/htop-inspired Docker container manager built with Textual."""

import json
import os
import threading
import time
from datetime import datetime, timezone

import docker
import docker.errors
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Matcher, Provider
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, RichLog, Static

GRUVBOX_BG = "#282828"
GRUVBOX_MED = "#3c3836"
GRUVBOX_PANEL = "#1d2021"
GRUVBOX_BORDER = "#504945"
GRUVBOX_FG = "#ebdbb2"
GRUVBOX_GRAY = "#a89984"
GRUVBOX_YELLOW = "#fabd2f"
GRUVBOX_ORANGE = "#fe8019"
GRUVBOX_GREEN = "#b8bb26"
GRUVBOX_RED = "#fb4934"
GRUVBOX_AQUA = "#8ec07c"
MAX_LOG_HISTORY = 5000
MAX_RESOURCE_HISTORY = 30
DEFAULT_STATS_INTERVAL = 2.0
DEFAULT_REGISTRY_INTERVAL = 60.0
DEFAULT_REGISTRY_CACHE_TTL = 900.0
MAX_REGISTRY_BACKOFF = 1800.0

STATUS_DISPLAY = {
    "running": ("\u25cf Running", GRUVBOX_GREEN),
    "exited": ("\u25a0 Stopped", GRUVBOX_RED),
    "dead": ("\u25a0 Dead", GRUVBOX_RED),
    "created": ("\u25a3 Created", GRUVBOX_YELLOW),
    "paused": ("\u23f8 Paused", GRUVBOX_AQUA),
}
STATUS_COMPACT = {
    "running": "\u25cf",
    "exited": "\u25a0",
    "dead": "\u25a0",
    "created": "\u25a3",
    "paused": "\u23f8",
}

UPDATE_DISPLAY = {
    "update": ("\u25b2 Update Available", GRUVBOX_ORANGE),
    "up-to-date": ("\u2713 Current", GRUVBOX_GREEN),
    "local": ("LOCAL ONLY", GRUVBOX_GRAY),
    "registry-error": ("REGISTRY ERROR", GRUVBOX_RED),
    "unknown": ("? Unknown", GRUVBOX_YELLOW),
    "updating": ("\u21bb Updating", GRUVBOX_AQUA),
    "pulling": ("\u21bb Pulling", GRUVBOX_AQUA),
    "stopping": ("\u21bb Stopping", GRUVBOX_YELLOW),
    "removing": ("\u2212 Removing", GRUVBOX_YELLOW),
    "recreating": ("\u21bb Recreating", GRUVBOX_AQUA),
    "restoring": ("\u21bb Restoring Networks", GRUVBOX_AQUA),
    "recovered": ("\u21ba Recovered", GRUVBOX_YELLOW),
    "degraded": ("! Degraded", GRUVBOX_ORANGE),
    "failed": ("\u2716 Failed", GRUVBOX_RED),
}
UPDATE_COMPACT = {
    "update": "\u25b2",
    "up-to-date": "\u2713",
    "updating": "\u21bb",
    "unknown": "?",
    "pulling": "\u21bb",
    "stopping": "\u21bb",
    "removing": "\u2212",
    "recreating": "\u21bb",
    "restoring": "\u21bb",
    "recovered": "\u21ba",
    "degraded": "!",
    "failed": "\u2716",
    "local": "\u00b7",
    "registry-error": "!",
}

DETAIL_IDS = (
    "d-name",
    "d-image",
    "d-id",
    "d-status",
    "d-created",
    "d-ports",
    "d-stats",
    "d-update",
    "d-restart-time",
    "d-network",
    "d-volumes",
    "d-command",
    "d-runtime",
    "d-mounts",
    "d-limits",
)

HEALTH_DISPLAY = {
    "healthy": ("\u2714 healthy", GRUVBOX_GREEN),
    "unhealthy": ("\u2716 unhealthy", GRUVBOX_RED),
    "starting": ("? unknown", GRUVBOX_YELLOW),
}


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("y", "confirm", "Confirm", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("n", "cancel", "Cancel", show=False),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("CONFIRM ACTION", classes="confirm-title"),
            Static(self.message, id="confirm-message"),
            Horizontal(
                Button("Cancel", id="cancel-confirm"),
                Button("Confirm", variant="warning", id="accept-confirm"),
                id="confirm-actions",
            ),
            id="confirm-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#accept-confirm", Button).focus()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed)
    def _on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "accept-confirm")


class DockPitCommandProvider(Provider):
    """Searchable command discovery for actions that are not footer bindings."""

    async def discover(self):
        for _score, label, action, help_text, _aliases in self._commands():
            yield Hit(0.0, label, action, help=help_text)

    async def search(self, query: str):
        query = query.strip().lower()
        matcher = Matcher(query)
        for score, label, action, help_text, aliases in self._commands():
            haystack = f"{label} {help_text} {' '.join(aliases)}".lower()
            if not query:
                yield Hit(0.0, label, action, help=help_text)
            else:
                match_score = matcher.match(haystack)
                if match_score > 0:
                    yield Hit(match_score, label, action, help=help_text)

    def _commands(self):
        app = self.app
        return (
            (0.0, "Start selected container", app.action_start_selected, "start lifecycle", ("run", "up")),
            (0.0, "Stop selected container", app.action_stop_selected, "stop lifecycle", ("halt", "down")),
            (0.0, "Restart selected container", app.action_restart_selected, "restart lifecycle", ("reboot",)),
            (0.0, "Pause selected container", app.action_pause_selected, "pause lifecycle", ("suspend",)),
            (0.0, "Remove selected container", app.action_remove_selected, "remove lifecycle", ("delete",)),
            (0.0, "Update selected container", app.action_update_selected, "update one image", ("upgrade",)),
            (0.0, "Update all available containers", app.action_update_all, "update every candidate", ("upgrade all",)),
            (0.0, "Toggle Attention Mode", app.action_toggle_attention, "attention alerts failures load", ("attention", "alerts")),
            (0.0, "Expand container groups", app.action_expand_groups, "group expand services", ("groups", "services")),
            (0.0, "Collapse container groups", app.action_collapse_groups, "group collapse services", ("groups", "services")),
            (0.0, "Show recent logs", app.action_logs_selected, "logs recent", ("log",)),
            (0.0, "Follow logs", app.action_logs_follow, "logs follow stream", ("log", "tail")),
            (0.0, "Filter containers", app.action_focus_filter, "filter containers", ("search",)),
            (0.0, "Filter logs", app.action_focus_log_filter, "filter log output", ("search",)),
            (0.0, "Inspect selected container", app.action_inspect_selected, "inspect metadata", ("details",)),
            (0.0, "Refresh containers", app.action_rescan, "rescan refresh", ("reload",)),
            (0.0, "Show registry settings", app.action_show_registry_settings, "settings registry cache ttl", ("settings", "configuration")),
        )


class DockerTUI(App):
    """btop/htop-inspired Docker container TUI in a vintage terminal palette."""

    TITLE = "DOCKPIT"
    CSS_PATH = "app.tcss"

    COMMANDS = {DockPitCommandProvider}
    BINDINGS = [
        Binding("s", "start_selected", "Start"),
        Binding("d", "stop_selected", "Stop"),
        Binding("p", "pause_selected", "Pause", show=False),
        Binding("R", "restart_selected", "Restart", show=False),
        Binding("x", "remove_selected", "Remove", show=False),
        Binding("u", "update_selected", "Update"),
        Binding("U", "update_all", "Update all", show=False),
        Binding("l", "logs_selected", "Logs"),
        Binding("L", "logs_follow", "Follow logs", show=False),
        Binding("/", "focus_filter", "Filter containers", show=False),
        Binding("f", "focus_log_filter", "Filter logs", show=False),
        Binding("!", "toggle_attention", "Attention"),
        Binding("i", "inspect_selected", "Inspect", show=False),
        Binding("r", "rescan", "Refresh"),
        Binding("ctrl+p", "command_palette", "Commands"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.client = None
        self.table: DataTable | None = None
        self.output: RichLog | None = None
        self.stats: Static | None = None
        self._container_by_key: dict[str, object] = {}
        self._selected_id: str | None = None
        self._updating: set[str] = set()
        self._stats: dict[str, tuple] = {}
        self._resource_history: dict[str, list[tuple[float | None, float | None]]] = {}
        self._update_status: dict[str, str] = {}
        self._recent_update_status: dict[str, str] = {}
        self._update_outcomes: dict[str, str] = {}
        self._filter_text = ""
        self._attention_mode = False
        self._all_containers: list = []
        self._log_following = False
        self._log_filter_text = ""
        self._log_history: list[tuple[str, str]] = []
        self._recent_actions: list[tuple[str, str, str]] = []
        self._stats_in_flight = False
        self._stats_started_at: float | None = None
        self._check_in_flight = False
        self._scan_generation = 0
        self._docker_state = "starting"
        self._quitting = False
        self._interval_handles: list = []
        self._stats_interval = self._read_interval("DOCKPIT_STATS_INTERVAL", DEFAULT_STATS_INTERVAL)
        self._registry_interval = self._read_interval(
            "DOCKPIT_REGISTRY_INTERVAL", DEFAULT_REGISTRY_INTERVAL
        )
        self._registry_cache_ttl = self._read_interval(
            "DOCKPIT_REGISTRY_CACHE_TTL", DEFAULT_REGISTRY_CACHE_TTL
        )
        self._registry_cache: dict[str, tuple[float, str]] = {}
        self._registry_cooldown_until = 0.0
        self._registry_backoff = 60.0
        self._registry_status = ""
        self._registry_notice_at = 0.0
        self._groups_expanded = True

    @staticmethod
    def _read_interval(name: str, default: float) -> float:
        try:
            value = float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("DOCKPIT", id="app-title"),
            Static("DOCKER OPS / LIVE", id="app-subtitle"),
            Horizontal(
                Input(placeholder="filter containers", id="container-filter"),
                Input(placeholder="filter logs", id="log-filter"),
                Static("starting\u2026", id="container-stats"),
                Static("", id="attention-banner"),
                id="header-controls",
            ),
            id="app-header",
        )
        yield Horizontal(
            DataTable(id="containers"),
            Vertical(
                Static("CONTAINER DETAILS", classes="panel-title"),
                Static("select a container", id="d-name", classes="detail-row"),
                Static("", id="d-image", classes="detail-row"),
                Static("", id="d-id", classes="detail-row"),
                Static("", id="d-status", classes="detail-row"),
                Static("", id="d-health", classes="detail-row"),
                Static("", id="d-restart", classes="detail-row"),
                Static("", id="d-created", classes="detail-row"),
                Static("", id="d-ports", classes="detail-row"),
                Static("", id="d-stats", classes="detail-row"),
                Static("", id="d-update", classes="detail-row"),
                Static("", id="d-restart-time", classes="detail-row"),
                Static("", id="d-network", classes="detail-row"),
                Static("", id="d-volumes", classes="detail-row"),
                Static("", id="d-command", classes="detail-row"),
                Static("", id="d-runtime", classes="detail-row"),
                Static("", id="d-mounts", classes="detail-row"),
                Static("", id="d-limits", classes="detail-row"),
                id="details",
            ),
            id="main",
        )
        yield RichLog(id="output", highlight=True, markup=True, wrap=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.table = self.query_one("#containers", DataTable)
        self.output = self.query_one("#output", RichLog)
        self.stats = self.query_one("#container-stats", Static)
        self.table.border_title = " CONTAINERS "
        self.query_one("#details", Vertical).border_title = " DETAILS "
        self.output.border_title = " LOG "
        self.table.cursor_type = "row"
        self.table.add_column("NAME", key="name", width=24)
        self.table.add_column("STATUS", key="status", width=10)
        self.table.add_column("HEALTH", key="health", width=10)
        self.table.add_column("CPU", key="cpu", width=8)
        self.table.add_column("MEM", key="mem", width=8)
        self.table.add_column("UPD", key="upd", width=5)
        self._configure_semantic_columns()
        self.push_log("Welcome to DOCKPIT.", f"bold {GRUVBOX_YELLOW}")
        try:
            self.client = docker.from_env(timeout=30)
            version = (self.client.version() or {}).get("Version", "?")
            self._docker_state = "connected"
        except Exception as exc:
            self.client = None
            self._docker_state = "unavailable"
            self.push_log(f"Docker connection failed: {exc}", f"bold {GRUVBOX_RED}")
            if "permission denied" in str(exc).lower():
                self.push_log(
                    "Run 'docker ps' in a terminal. If that also fails, Docker isn't "
                    "running or you lack socket permissions - restart Docker Desktop.",
                    GRUVBOX_YELLOW,
                )
            self.push_log("Press r to retry the connection.", GRUVBOX_YELLOW)
            return
        self.push_log(f"Connected to Docker daemon (API {version}).", f"bold {GRUVBOX_GREEN}")
        self._request_scan()
        self.table.focus()
        self._interval_handles.append(self.set_interval(self._stats_interval, self._on_stats_timer))
        self._interval_handles.append(self.set_interval(0.5, self._refresh_stats_wait))
        self._interval_handles.append(self.set_interval(self._registry_interval, self._on_update_check_timer))

    # --- shutdown ----------------------------------------------------------

    def action_help_quit(self) -> None:
        """Ctrl+C: quit gracefully instead of showing Textual's 'press q' reminder."""
        self.exit()

    def on_unmount(self) -> None:
        self._quitting = True
        self._log_following = False
        self._stats_in_flight = False
        self._check_in_flight = False
        for timer in self._interval_handles:
            timer.stop()
        self._interval_handles.clear()
        try:
            if self.client is not None:
                self.client.close()
        except Exception:
            pass

    # --- logging -----------------------------------------------------------

    def push_log(self, message: str, style: str = "") -> None:
        if self._quitting:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._log_history.append((message, style))
        self._recent_actions.append((timestamp, message, style))
        if len(self._recent_actions) > MAX_LOG_HISTORY:
            del self._recent_actions[: len(self._recent_actions) - MAX_LOG_HISTORY]
        if len(self._log_history) > MAX_LOG_HISTORY:
            del self._log_history[: len(self._log_history) - MAX_LOG_HISTORY]
        if self.output is None:
            return
        if self._thread_id == threading.get_ident():
            self._write_log(message, style, timestamp)
        else:
            self.call_from_thread(self._write_log, message, style, timestamp)

    def _write_log(self, message: str, style: str = "", timestamp: str | None = None) -> None:
        if self._log_matches_filter(message):
            stamp = timestamp or datetime.now().strftime("%H:%M:%S")
            self.output.write(Text(f"{stamp}  {message}", style=style))

    @on(Input.Changed, "#log-filter")
    def _on_log_filter_changed(self, event: Input.Changed) -> None:
        self._log_filter_text = event.value.strip().lower()
        self._render_log_history()

    def action_focus_log_filter(self) -> None:
        self.query_one("#log-filter", Input).focus()

    def _log_matches_filter(self, message: str) -> bool:
        return not self._log_filter_text or self._log_filter_text in message.lower()

    def _render_log_history(self) -> None:
        if self.output is None:
            return
        self.output.clear()
        for timestamp, message, style in self._recent_actions:
            self._write_log(message, style, timestamp)

    # --- scanning ----------------------------------------------------------

    def _request_scan(self) -> None:
        self._scan_generation += 1
        self._scan_worker(self._scan_generation)

    @work(thread=True)
    def _scan_worker(self, generation: int) -> None:
        if self._quitting:
            return
        if self.client is None:
            try:
                self.client = docker.from_env(timeout=30)
                version = (self.client.version() or {}).get("Version", "?")
                self._docker_state = "connected"
                self.push_log(f"Connected to Docker daemon (API {version}).", f"bold {GRUVBOX_GREEN}")
            except Exception as exc:
                self._docker_state = "unavailable"
                self.push_log(f"Docker unavailable: {exc}", f"bold {GRUVBOX_RED}")
                if not self._quitting:
                    self.call_from_thread(self._refresh_header)
                return
        try:
            containers = sorted(
                self.client.containers.list(all=True),
                key=lambda c: (c.status != "running", (c.name or c.id).lstrip("/")),
            )
        except Exception as exc:
            self._docker_state = "unavailable"
            self.client = None
            self.push_log(f"Docker unavailable: {exc}", f"bold {GRUVBOX_RED}")
            if not self._quitting:
                self.call_from_thread(self._refresh_header)
            return
        self._docker_state = "connected"
        if not self._quitting:
            self.call_from_thread(self._populate_table, containers, generation)

    def _populate_table(
        self, containers: list, generation: int | None = None, remember: bool = True
    ) -> None:
        if (generation is not None and generation != self._scan_generation) or self._quitting:
            return
        if remember:
            self._all_containers = list(containers)
        containers = [container for container in containers if self._matches_filter(container)]
        prev_selected = self._selected_id if self._selected_id in self._container_by_key else None
        self._container_by_key.clear()
        self._update_status.clear()
        self._stats = {cid: pair for cid, pair in self._stats.items() if cid in {c.id for c in containers}}
        self._resource_history = {
            cid: history for cid, history in self._resource_history.items() if cid in {c.id for c in containers}
        }
        self.table.clear()
        for container in containers:
            self._add_row(container)
        for cid, container in self._container_by_key.items():
            state = self._recent_update_status.get(self._name_of(container))
            if state:
                self._update_status[cid] = state
                self._set_upd_cell(cid, state)
        if not containers:
            self._selected_id = None
            self._clear_details()
        else:
            if prev_selected:
                for idx, container in enumerate(containers):
                    if container.id == prev_selected:
                        self.table.move_cursor(row=idx)
                        break
                else:
                    self.table.move_cursor(row=0)
            else:
                self._selected_id = containers[0].id
                self.table.move_cursor(row=0)
        self._refresh_header()
        self._update_attention_banner()
        self._update_check_tick()
        self._on_stats_timer()

    @on(Input.Changed, "#container-filter")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._filter_text = event.value.strip().lower()
        if self._all_containers:
            self._populate_table(self._all_containers, remember=False)

    def action_toggle_attention(self) -> None:
        self._attention_mode = not self._attention_mode
        self._update_attention_banner()
        self._populate_table(self._all_containers, remember=False)

    def action_expand_groups(self) -> None:
        self._groups_expanded = True
        self.push_log("Container groups expanded.", GRUVBOX_GRAY)

    def action_collapse_groups(self) -> None:
        self._groups_expanded = False
        self.push_log("Container groups collapsed; actionable containers remain visible.", GRUVBOX_GRAY)

    def action_show_registry_settings(self) -> None:
        self.push_log(
            f"Registry cache: {self._registry_cache_ttl / 60:.0f}m TTL; "
            f"poll interval: {self._registry_interval:.0f}s.",
            GRUVBOX_GRAY,
        )

    def _update_attention_banner(self) -> None:
        try:
            banner = self.query_one("#attention-banner", Static)
        except Exception:
            return
        if self._attention_mode:
            banner.update("ATTENTION MODE: stopped / unhealthy / updates / failures / high load")
            banner.styles.display = "block"
        else:
            banner.update("")
            banner.styles.display = "none"

    def action_focus_filter(self) -> None:
        self.query_one("#container-filter", Input).focus()

    def _matches_filter(self, container) -> bool:
        if self._attention_mode and not self._needs_attention(container):
            return False
        if not self._filter_text:
            return True
        attrs = container.attrs
        name = self._name_of(container)
        image = (attrs.get("Config") or {}).get("Image") or ""
        status = container.status or ""
        haystack = f"{name} {image} {status}".lower()
        return self._filter_text in haystack

    def _needs_attention(self, container) -> bool:
        status = container.status or ""
        health = ((container.attrs.get("State") or {}).get("Health") or {}).get("Status")
        cpu, mem = self._stats.get(container.id) or (None, None)
        recent_failure = self._recent_update_status.get(self._name_of(container)) in {
            "failed", "degraded", "recovered"
        }
        return (
            status in {"exited", "dead", "paused"}
            or health == "unhealthy"
            or self._update_status.get(container.id) == "update"
            or recent_failure
            or (cpu is not None and cpu >= 80)
            or (mem is not None and mem >= 80)
        )

    def _add_row(self, container) -> None:
        attrs = container.attrs
        name = (container.name or container.id).lstrip("/")
        image = self._format_image(attrs, container)
        status = container.status or (attrs.get("State") or {}).get("Status") or "unknown"
        status_label = self._status_cell(status)
        health_label, health_color = self._health_display(attrs)
        update_state = self._update_status.get(container.id, self._recent_update_status.get(name, "unknown"))
        update_label = self._update_cell(update_state)
        self.table.add_row(
            Text(name, style=f"bold {GRUVBOX_FG}"),
            status_label,
            Text(health_label, style=f"bold {health_color}"),
            self._pct_cell(None),
            self._pct_cell(None),
            update_label,
            key=container.id,
        )
        self._container_by_key[container.id] = container

    def _configure_semantic_columns(self) -> None:
        if self.table is None:
            return
        width = self.table.size.width
        self.table.columns["status"].width = 10 if width >= 82 else 3
        update_width = 18 if width >= 92 else 5
        self.table.columns["upd"].width = update_width

    def _status_cell(self, status: str) -> Text:
        full_label, color = STATUS_DISPLAY.get(status, (f"? {status}", GRUVBOX_YELLOW))
        compact = self.table is not None and self.table.columns["status"].width <= 3
        label = STATUS_COMPACT.get(status, "?") if compact else full_label
        return Text(label, style=f"bold {color}")

    def _update_cell(self, state: str) -> Text:
        full_label, color = UPDATE_DISPLAY.get(state, UPDATE_DISPLAY["unknown"])
        compact = self.table is not None and self.table.columns["upd"].width <= 5
        label = UPDATE_COMPACT.get(state, UPDATE_COMPACT["unknown"]) if compact else full_label
        return Text(label, style=f"bold {color}")

    def on_resize(self, event: Resize) -> None:
        if self.table is None:
            return
        previous_width = self.table.columns["upd"].width
        self._configure_semantic_columns()
        if previous_width == self.table.columns["upd"].width:
            if previous_width == self.table.columns["upd"].width:
                for cid, container in self._container_by_key.items():
                    status = container.status or (container.attrs.get("State") or {}).get("Status") or "unknown"
                    self.table.update_cell(cid, "status", self._status_cell(status))
                return
        for cid, container in self._container_by_key.items():
            state = self._update_status.get(cid, self._recent_update_status.get(self._name_of(container), "unknown"))
            self.table.update_cell(cid, "upd", self._update_cell(state))
            status = container.status or (container.attrs.get("State") or {}).get("Status") or "unknown"
            self.table.update_cell(cid, "status", self._status_cell(status))

    @staticmethod
    def _health_display(attrs: dict) -> tuple[str, str]:
        state = ((attrs.get("State") or {}).get("Health") or {}).get("Status")
        if state in HEALTH_DISPLAY:
            return HEALTH_DISPLAY[state]
        if (attrs.get("Config") or {}).get("Healthcheck"):
            return "UNKNOWN", GRUVBOX_YELLOW
        return "NO CHECK", GRUVBOX_GRAY

    @staticmethod
    def _restart_policy(attrs: dict) -> str:
        name = ((attrs.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name")
        return name or "none"

    @staticmethod
    def _format_ports(attrs: dict) -> str:
        ports = (attrs.get("NetworkSettings") or {}).get("Ports") or {}
        parts = []
        for container_port, bindings in sorted(ports.items()):
            if bindings:
                host = bindings[0]
                host_ip = host.get("HostIp") or "0.0.0.0"
                parts.append(f"{host_ip}:{host.get('HostPort')}->{container_port}")
            else:
                parts.append(container_port)
        return "  ".join(parts)

    # --- update detection --------------------------------------------------

    def _on_update_check_timer(self) -> None:
        self._update_check_tick()

    def _update_check_tick(self) -> None:
        if self.client is None or self._check_in_flight or not self._container_by_key:
            return
        self._check_in_flight = True
        self._update_check_worker(self._scan_generation)

    @work(thread=True)
    def _update_check_worker(self, generation: int) -> None:
        results = {}
        try:
            by_ref = {}
            for cid, container in list(self._container_by_key.items()):
                if self._quitting:
                    return
                ref = (container.attrs.get("Config") or {}).get("Image") or ""
                by_ref.setdefault(ref, []).append(cid)
            checked = {}
            for ref, ids in by_ref.items():
                if ids:
                    checked[ref] = self._check_update(self._container_by_key[ids[0]])
            for ref, ids in by_ref.items():
                for cid in ids:
                    results[cid] = checked.get(ref, "unknown")
        except Exception as exc:
            self.push_log(f"Update check failed: {exc}", f"bold {GRUVBOX_RED}")
        finally:
            if not self._quitting:
                self.call_from_thread(self._apply_update_status, results, generation)

    def _check_update(self, container) -> str:
        if self.client is None:
            return "unknown"
        try:
            ref = (container.attrs.get("Config") or {}).get("Image") or ""
            if not ref or "@" in ref:
                return "unknown"
            now = time.monotonic()
            cached = self._registry_cache.get(ref)
            if cached and now - cached[0] < self._registry_cache_ttl:
                return cached[1]
            if now < self._registry_cooldown_until:
                self._set_registry_status()
                return cached[1] if cached else "unknown"
            image = None
            try:
                image = container.image
            except Exception:
                image = None
            if image is None:
                try:
                    image = self.client.images.get(ref)
                except Exception:
                    image = None
            if image is None:
                return "unknown"
            repo_digests = (image.attrs or {}).get("RepoDigests") or []
            if not repo_digests:
                return self._cache_registry_result(ref, "local")
            local = repo_digests[0].rsplit("@", 1)[-1]
            if not local.startswith("sha256:"):
                return self._cache_registry_result(ref, "unknown")
            try:
                data = self.client.images.get_registry_data(ref)
            except Exception as exc:
                if self._is_rate_limited(exc):
                    self._enter_registry_cooldown()
                    return cached[1] if cached else "unknown"
                self._registry_status = "Registry unavailable"
                self._log_registry_warning(f"Registry check failed for '{ref}': {exc}")
                return cached[1] if cached else "registry-error"
            attrs = getattr(data, "attrs", None) or {}
            descriptor = attrs.get("Descriptor") or {}
            remote = descriptor.get("digest") or attrs.get("Digest")
            if not remote:
                return self._cache_registry_result(ref, "unknown")
            return self._cache_registry_result(ref, "up-to-date" if local == remote else "update")
        except Exception:
            return "unknown"

    def _cache_registry_result(self, ref: str, state: str) -> str:
        self._registry_cache[ref] = (time.monotonic(), state)
        if time.monotonic() >= self._registry_cooldown_until:
            self._registry_backoff = 60.0
            self._registry_status = ""
        return state

    @staticmethod
    def _is_rate_limited(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
        return status == 429 or "429" in str(exc)

    def _enter_registry_cooldown(self) -> None:
        now = time.monotonic()
        self._registry_cooldown_until = now + self._registry_backoff
        self._registry_status = f"Registry rate limited - retrying in {max(1, round(self._registry_backoff / 60))} minutes"
        self._registry_backoff = min(MAX_REGISTRY_BACKOFF, self._registry_backoff * 2)
        self._log_registry_warning(self._registry_status)
        self._refresh_header()

    def _set_registry_status(self) -> None:
        remaining = max(1, round((self._registry_cooldown_until - time.monotonic()) / 60))
        self._registry_status = f"Registry rate limited - retrying in {remaining} minutes"

    def _log_registry_warning(self, message: str) -> None:
        now = time.monotonic()
        if now - self._registry_notice_at >= 60:
            self._registry_notice_at = now
            self.push_log(message, GRUVBOX_YELLOW)

    def _apply_update_status(self, results: dict, generation: int | None = None) -> None:
        self._check_in_flight = False
        if (generation is not None and generation != self._scan_generation) or self._quitting:
            return
        self._update_status.update(results)
        for cid, state in results.items():
            if cid not in self._updating:
                self._set_upd_cell(cid, state)
        if self._selected_id in self._container_by_key:
            self._update_details(self._selected_id)
        self._refresh_header()

    # --- live stats --------------------------------------------------------

    def _on_stats_timer(self) -> None:
        if self.client is None or self._stats_in_flight or not self._container_by_key:
            return
        self._stats_in_flight = True
        self._stats_started_at = time.monotonic()
        self._refresh_header()
        self._stats_worker(self._scan_generation)

    def _refresh_stats_wait(self) -> None:
        if self._stats_in_flight:
            self._refresh_header()

    @work(thread=True)
    def _stats_worker(self, generation: int) -> None:
        snapshot = {}
        if self._quitting:
            return
        try:
            for cid, container in list(self._container_by_key.items()):
                if self._quitting:
                    return
                if container.status != "running":
                    snapshot[cid] = None
                    continue
                try:
                    sample = self._read_stats_sample(container)
                    snapshot[cid] = (self._compute_cpu(sample), self._compute_mem(sample))
                except Exception:
                    snapshot[cid] = None
        except Exception as exc:
            self.push_log(f"Stats polling failed: {exc}", f"bold {GRUVBOX_YELLOW}")
        finally:
            if not self._quitting:
                self.call_from_thread(self._apply_stats, snapshot, generation)

    @staticmethod
    def _read_stats_sample(container) -> dict:
        stream = container.stats(stream=True, decode=True)
        try:
            sample = next(iter(stream))
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        if not isinstance(sample, dict):
            raise RuntimeError("Docker returned an invalid stats sample")
        return sample

    @staticmethod
    def _compute_cpu(sample: dict):
        cpu_stats = sample.get("cpu_stats") or {}
        precpu_stats = sample.get("precpu_stats") or {}
        cpu_delta = (
            (cpu_stats.get("cpu_usage") or {}).get("total_usage", 0)
            - (precpu_stats.get("cpu_usage") or {}).get("total_usage", 0)
        )
        system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
        online = cpu_stats.get("online_cpus") or len(
            (cpu_stats.get("cpu_usage") or {}).get("percpu_usage", [1])
        )
        if system_delta <= 0 or cpu_delta <= 0:
            return 0.0
        return (cpu_delta / system_delta) * online * 100.0

    @staticmethod
    def _compute_mem(sample: dict):
        memory = sample.get("memory_stats") or {}
        usage = memory.get("usage")
        limit = memory.get("limit")
        if not usage or not limit:
            return None
        return (usage / limit) * 100.0

    def _apply_stats(self, snapshot: dict, generation: int | None = None) -> None:
        self._stats_in_flight = False
        self._stats_started_at = None
        if (generation is not None and generation != self._scan_generation) or self._quitting:
            return
        for cid, pair in snapshot.items():
            self._stats[cid] = pair
            history = self._resource_history.setdefault(cid, [])
            history.append(pair or (None, None))
            if len(history) > MAX_RESOURCE_HISTORY:
                del history[: len(history) - MAX_RESOURCE_HISTORY]
            cpu = mem = None
            if pair is not None:
                cpu, mem = pair
            try:
                self.table.update_cell(cid, "cpu", self._pct_cell(cpu))
                self.table.update_cell(cid, "mem", self._pct_cell(mem))
            except Exception:
                pass
        if self._selected_id in self._container_by_key:
            self._update_details(self._selected_id)
        self._refresh_header()

    def _pct_cell(self, value: float | None) -> Text:
        text = Text()
        color = f"bold {self._pct_color(value)}"
        text.append(self._gauge(value, width=4), style=color)
        text.append(" ", style=GRUVBOX_GRAY)
        text.append("--" if value is None else self._format_pct(value), style=color)
        return text

    @staticmethod
    def _format_pct(value: float) -> str:
        return f"{value:.1f}%" if value < 10 else f"{value:.0f}%"

    @staticmethod
    def _gauge(value: float | None, width: int = 10) -> str:
        if value is None:
            return "\u2591" * width
        ratio = max(0.0, min(100.0, value)) / 100.0
        filled = round(ratio * width)
        return "\u2588" * filled + "\u2591" * (width - filled)

    def _gauge_pct(self, value: float | None, width: int = 10) -> Text:
        text = Text()
        if value is None:
            text.append(f"[{self._gauge(value, width)}]  \u2013", style=GRUVBOX_GRAY)
            return text
        color = f"bold {self._pct_color(value)}"
        text.append(f"[{self._gauge(value, width)}]", style=color)
        text.append(f" {self._format_pct(value)}", style=color)
        return text

    @staticmethod
    def _pct_color(value: float | None) -> str:
        if value is None:
            return GRUVBOX_GRAY
        if value >= 80:
            return GRUVBOX_RED
        if value >= 50:
            return GRUVBOX_YELLOW
        return GRUVBOX_GREEN

    def _stats_wait_label(self, now: float | None = None) -> str | None:
        if not self._stats_in_flight or self._stats_started_at is None:
            return None
        elapsed = max(0, int((time.monotonic() if now is None else now) - self._stats_started_at))
        return f"STATS {'|/-\\'[elapsed % 4]} {elapsed}s"

    @staticmethod
    def _sparkline(values: list[float | None], width: int = 12) -> str:
        points = [value for value in values if value is not None]
        if not points:
            return "\u00b7" * width
        recent = points[-width:]
        levels = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
        result = "\u00b7" * max(0, width - len(recent))
        result += "".join(levels[min(len(levels) - 1, round(max(0.0, min(100.0, value)) / 100 * (len(levels) - 1)))] for value in recent)
        return result

    # --- header ------------------------------------------------------------

    def _refresh_header(self) -> None:
        if self.stats is None:
            return
        items = list(self._container_by_key.values())
        running = sum(1 for c in items if c.status == "running")
        updates = sum(1 for state in self._update_status.values() if state == "update")
        values = [pair for pair in self._stats.values() if pair is not None]
        cpus = [cpu for cpu, _mem in values if cpu is not None]
        mems = [mem for _cpu, mem in values if mem is not None]
        avg_cpu = sum(cpus) / len(cpus) if cpus else None
        avg_mem = sum(mems) / len(mems) if mems else None
        header = Text(f"{len(items)} containers | {running} running | ", style="bold")
        header.append(
            f"{updates} update available" if updates else "0 updates",
            style=f"bold {GRUVBOX_ORANGE if updates else GRUVBOX_GREEN}",
        )
        header.append(" | CPU ", style="bold")
        header.append(self._gauge_pct(avg_cpu, width=6))
        header.append("  MEM ", style="bold")
        header.append(self._gauge_pct(avg_mem, width=6))
        stats_wait = self._stats_wait_label()
        if stats_wait:
            header.append(f"  {stats_wait}", style=f"bold {GRUVBOX_YELLOW}")
        if self._docker_state == "unavailable":
            header.append("  DOCKER UNAVAILABLE - press r", style=f"bold {GRUVBOX_RED}")
        if self._registry_status:
            header.append(f"  {self._registry_status}", style=f"bold {GRUVBOX_YELLOW}")
        self.stats.update(header)

    # --- details panel -----------------------------------------------------

    def _clear_details(self) -> None:
        for widget_id in DETAIL_IDS:
            self.query_one(f"#{widget_id}", Static).update("")
        self._set_detail("d-name", ("\u2014 no container selected \u2014", GRUVBOX_GRAY))

    def _update_details(self, cid: str) -> None:
        if cid not in self._container_by_key:
            self._clear_details()
            return
        container = self._container_by_key[cid]
        attrs = container.attrs
        name = (container.name or container.id).lstrip("/")
        status = container.status or (attrs.get("State") or {}).get("Status") or "unknown"
        image = (attrs.get("Config") or {}).get("Image") or "?"
        short_id = container.id[:12] if container.id else "?"
        created = (attrs.get("Created") or "").strip()
        ports = self._format_ports(attrs) or "none published"
        cpu, mem = self._stats.get(cid) or (None, None)
        history = self._resource_history.get(cid, [])
        cpu_text = "\u2013" if cpu is None else f"{cpu:.1f}%"
        mem_text = "\u2013" if mem is None else self._format_pct(mem)
        state = self._update_status.get(cid, "unknown")
        state_text, state_color = UPDATE_DISPLAY.get(state, UPDATE_DISPLAY["unknown"])
        status_label, status_color = STATUS_DISPLAY.get(status, (f"? {status}", GRUVBOX_YELLOW))
        health_text, health_color = self._health_display(attrs)
        restart = self._restart_policy(attrs)

        self._set_detail("d-name", (name, f"bold {GRUVBOX_FG}"))
        self._set_detail(
            "d-image",
            ("IMAGE  ", GRUVBOX_GRAY),
            (image, GRUVBOX_FG),
        )
        self._set_detail("d-id", ("ID     ", GRUVBOX_GRAY), (short_id, GRUVBOX_FG))
        self._set_detail(
            "d-status",
            ("STATUS ", GRUVBOX_GRAY),
            (status_label, f"bold {status_color}"),
        )
        self._set_detail(
            "d-health",
            ("HEALTH ", GRUVBOX_GRAY),
            (self._format_health(attrs, health_text), f"bold {health_color}"),
        )
        self._set_detail("d-restart", ("RESTART", GRUVBOX_GRAY), (restart, GRUVBOX_FG))
        self._set_detail("d-created", ("UP     ", GRUVBOX_GRAY), (self._fmt_created(created), GRUVBOX_FG))
        self._set_detail("d-ports", ("PORTS  ", GRUVBOX_GRAY), (ports, GRUVBOX_FG))
        self._set_detail("d-stats", (self._stats_block(cpu, mem, cpu_text, mem_text, history), ""))
        self._set_detail("d-update", ("UPDATE ", GRUVBOX_GRAY), (state_text, f"bold {state_color}"))
        restart_time = ((attrs.get("State") or {}).get("StartedAt") or "")
        networks = self._format_networks(attrs)
        volume_count = len(attrs.get("Mounts") or [])
        self._set_detail("d-restart-time", ("STARTED", GRUVBOX_GRAY), (self._fmt_created(restart_time), GRUVBOX_FG))
        self._set_detail("d-network", ("NETWORK", GRUVBOX_GRAY), (networks, GRUVBOX_FG))
        self._set_detail("d-volumes", ("VOLUMES", GRUVBOX_GRAY), (str(volume_count), GRUVBOX_FG))
        command = self._format_command(attrs)
        runtime = self._format_runtime(attrs)
        mounts = self._format_mounts(attrs)
        limits = self._format_limits(attrs)
        self._set_detail("d-command", ("COMMAND", GRUVBOX_GRAY), (command, GRUVBOX_FG))
        self._set_detail("d-runtime", ("RUNTIME", GRUVBOX_GRAY), (runtime, GRUVBOX_FG))
        self._set_detail("d-mounts", ("MOUNTS ", GRUVBOX_GRAY), (mounts, GRUVBOX_FG))
        self._set_detail("d-limits", ("LIMITS ", GRUVBOX_GRAY), (limits, GRUVBOX_FG))

    def _stats_block(
        self,
        cpu: float | None,
        mem: float | None,
        cpu_text: str,
        mem_text: str,
        history: list[tuple[float | None, float | None]] | None = None,
    ) -> Text:
        text = Text()
        for label, value, display in (("CPU", cpu, cpu_text), ("MEM", mem, mem_text)):
            color = f"bold {self._pct_color(value)}"
            text.append(f"{label} [", style=GRUVBOX_GRAY)
            text.append(self._gauge(value, width=10), style=color)
            text.append("]  ", style=GRUVBOX_GRAY)
            text.append(display, style=color)
            text.append("\n", style=GRUVBOX_GRAY)
        history = history or []
        text.append("HIST ", style=GRUVBOX_GRAY)
        text.append(self._sparkline([pair[0] for pair in history]), style=GRUVBOX_AQUA)
        text.append(" / ", style=GRUVBOX_GRAY)
        text.append(self._sparkline([pair[1] for pair in history]), style=GRUVBOX_YELLOW)
        if history:
            cpu_samples = [pair[0] for pair in history if pair[0] is not None]
            mem_samples = [pair[1] for pair in history if pair[1] is not None]
            text.append("\nPEAK ", style=GRUVBOX_GRAY)
            text.append(
                f"CPU {self._format_pct(max(cpu_samples)) if cpu_samples else '--'}  "
                f"MEM {self._format_pct(max(mem_samples)) if mem_samples else '--'}  "
                f"({len(history)} samples)",
                style=GRUVBOX_FG,
            )
        return text

    @staticmethod
    def _format_command(attrs: dict) -> str:
        config = attrs.get("Config") or {}
        entrypoint = config.get("Entrypoint") or ""
        command = config.get("Cmd") or []
        if isinstance(entrypoint, list):
            parts = entrypoint + command
        else:
            parts = ([entrypoint] if entrypoint else []) + (command if isinstance(command, list) else [command])
        return " ".join(str(part) for part in parts).strip() or "none"

    @staticmethod
    def _format_image(attrs: dict, container) -> str:
        image_ref = (attrs.get("Config") or {}).get("Image") or "?"
        image = getattr(container, "image", None)
        digests = (getattr(image, "attrs", None) or {}).get("RepoDigests") or []
        digest = digests[0].split("@", 1)[-1] if digests else ""
        return f"{image_ref} @ {digest[:19]}" if digest.startswith("sha256:") else image_ref

    @staticmethod
    def _format_health(attrs: dict, health_text: str) -> str:
        health = ((attrs.get("State") or {}).get("Health") or {})
        failures = health.get("FailingStreak") or 0
        return f"{health_text} | failures {failures}" if failures else health_text

    @staticmethod
    def _format_networks(attrs: dict) -> str:
        networks = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
        if not networks:
            return "none"
        values = []
        for name, data in sorted(networks.items()):
            address = (data or {}).get("IPAddress") or "-"
            values.append(f"{name} ({address})")
        return ", ".join(values)

    @staticmethod
    def _format_runtime(attrs: dict) -> str:
        state = attrs.get("State") or {}
        status = state.get("Status") or "unknown"
        exit_code = state.get("ExitCode")
        error = state.get("Error") or ""
        restart_count = attrs.get("RestartCount", 0)
        result = f"{status} | restarts {restart_count}"
        if exit_code not in (None, 0):
            result += f" | exit {exit_code}"
        if error:
            result += f" | {error}"
        return result

    @staticmethod
    def _format_mounts(attrs: dict) -> str:
        mounts = attrs.get("Mounts") or []
        if not mounts:
            binds = (attrs.get("HostConfig") or {}).get("Binds") or []
            return ", ".join(str(bind).split(":", 1)[0] for bind in binds) or "none"
        values = []
        for mount in mounts:
            source = mount.get("Source") or mount.get("Name") or "?"
            destination = mount.get("Destination") or "?"
            mode = "ro" if mount.get("RW") is False else "rw"
            values.append(f"{source}->{destination} ({mode})")
        return ", ".join(values)

    @staticmethod
    def _format_limits(attrs: dict) -> str:
        host = attrs.get("HostConfig") or {}
        memory = host.get("Memory") or 0
        nano_cpus = host.get("NanoCpus") or 0
        parts = []
        if memory:
            parts.append(f"MEM {memory / (1024 ** 3):.1f}G")
        if nano_cpus:
            parts.append(f"CPU {nano_cpus / 1_000_000_000:.2f}")
        return "  ".join(parts) or "unlimited"

    def _set_detail(self, widget_id: str, *parts) -> None:
        text = Text()
        for content, style in parts:
            if isinstance(content, Text):
                text.append(content)
            else:
                text.append(str(content), style=style)
        self.query_one(f"#{widget_id}", Static).update(text)

    @staticmethod
    def _fmt_created(iso: str) -> str:
        try:
            created = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            delta = int((datetime.now(timezone.utc) - created).total_seconds())
            if delta < 60:
                return "just now"
            if delta < 3600:
                return f"{delta // 60}m ago"
            if delta < 86400:
                return f"{delta // 3600}h {delta % 3600 // 60}m ago"
            return f"{delta // 86400}d {delta % 86400 // 3600}h ago"
        except Exception:
            return "unknown"

    # --- events & actions --------------------------------------------------

    @staticmethod
    def _key_value(row_key) -> str:
        value = getattr(row_key, "value", None)
        return value if isinstance(value, str) else str(row_key)

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._selected_id = self._key_value(event.row_key)
        self._update_details(self._selected_id)

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        self._update_for_key(event.row_key)

    def action_rescan(self) -> None:
        self.push_log("Rescanning host containers\u2026", GRUVBOX_ORANGE)
        self._request_scan()

    def action_update_selected(self) -> None:
        cid = self._selected_id
        if cid is None or cid not in self._container_by_key:
            if not self._container_by_key:
                self.push_log("No containers to update.", GRUVBOX_YELLOW)
                return
            cid = next(iter(self._container_by_key))
            self._selected_id = cid
            self.table.move_cursor(row=0)
        if cid in self._updating:
            self.push_log("Container is already being updated.", GRUVBOX_YELLOW)
            return
        container = self._container_by_key[cid]
        self._confirm_action(
            f"Replace '{self._name_of(container)}' with the latest image?",
            lambda confirmed: self._start_update(container, confirmed),
        )

    def action_update_all(self) -> None:
        targets = [
            container
            for cid, container in self._container_by_key.items()
            if self._update_status.get(cid) == "update"
        ]
        if not targets:
            self.push_log("No containers with available updates.", GRUVBOX_YELLOW)
            return
        names = ", ".join(self._name_of(c) for c in targets)
        self._confirm_action(
            f"Update {len(targets)} container(s): {names}?",
            lambda confirmed: self._start_update_all(targets, confirmed),
        )

    def _confirm_action(self, message: str, callback) -> None:
        self.push_screen(ConfirmScreen(message), callback)

    def _start_update(self, container, confirmed: bool) -> None:
        if not confirmed:
            self.push_log("Update cancelled.", GRUVBOX_GRAY)
            return
        cid = container.id
        if cid in self._updating:
            self.push_log("Container is already being updated.", GRUVBOX_YELLOW)
            return
        self._updating.add(cid)
        self._set_upd_cell(cid, "updating")
        self._update_worker(container)

    def _start_update_all(self, targets: list, confirmed: bool) -> None:
        if not confirmed:
            self.push_log("Update-all cancelled.", GRUVBOX_GRAY)
            return
        targets = [container for container in targets if container.id not in self._updating]
        if not targets:
            self.push_log("All selected containers are already being updated.", GRUVBOX_YELLOW)
            return
        names = ", ".join(self._name_of(c) for c in targets)
        self.push_log(f"Updating {len(targets)} container(s): {names}", f"bold {GRUVBOX_ORANGE}")
        for container in targets:
            self._updating.add(container.id)
            self._set_upd_cell(container.id, "updating")
        self._update_all_worker(targets)

    def action_start_selected(self) -> None:
        self._run_lifecycle_action("start")

    def action_stop_selected(self) -> None:
        self._run_lifecycle_action("stop")

    def action_pause_selected(self) -> None:
        self._run_lifecycle_action("pause")

    def action_remove_selected(self) -> None:
        self._run_lifecycle_action("remove")

    def action_restart_selected(self) -> None:
        self._run_lifecycle_action("restart")

    def _run_lifecycle_action(self, operation: str) -> None:
        container = self._selected_container()
        if container is None:
            self.push_log("No container selected.", GRUVBOX_YELLOW)
            return
        if container.id in self._updating:
            self.push_log("Container is already being updated.", GRUVBOX_YELLOW)
            return
        if operation in {"stop", "pause", "remove"}:
            verb = {"stop": "Stop", "pause": "Pause", "remove": "Remove"}[operation]
            self._confirm_action(
                f"{verb} '{self._name_of(container)}'?",
                lambda confirmed: self._start_lifecycle(container, operation, confirmed),
            )
            return
        self._start_lifecycle(container, operation, True)

    def _start_lifecycle(self, container, operation: str, confirmed: bool) -> None:
        if not confirmed:
            self.push_log(f"{operation.capitalize()} cancelled.", GRUVBOX_GRAY)
            return
        self._lifecycle_worker(container, operation)

    def _selected_container(self):
        if self._selected_id is None:
            return None
        return self._container_by_key.get(self._selected_id)

    @work(thread=True)
    def _lifecycle_worker(self, container, operation: str) -> None:
        name = self._name_of(container)
        present = {
            "start": "Starting", "stop": "Stopping", "pause": "Pausing", "restart": "Restarting", "remove": "Removing"
        }[operation]
        past = {"start": "started", "stop": "stopped", "pause": "paused", "restart": "restarted", "remove": "removed"}[operation]
        try:
            self.push_log(f"{present} '{name}'...", GRUVBOX_ORANGE)
            getattr(container, operation)()
            self.push_log(f"Container '{name}' {past}.", f"bold {GRUVBOX_GREEN}")
        except Exception as exc:
            self.push_log(f"Could not {operation} '{name}': {exc}", f"bold {GRUVBOX_RED}")
        finally:
            if not self._quitting:
                self.call_from_thread(self._request_scan)

    def action_logs_selected(self) -> None:
        container = self._selected_container()
        if container is None:
            self.push_log("No container selected.", GRUVBOX_YELLOW)
            return
        self._logs_worker(container)

    def action_inspect_selected(self) -> None:
        container = self._selected_container()
        if container is None:
            self.push_log("No container selected.", GRUVBOX_YELLOW)
            return
        self._inspect_worker(container)

    @work(thread=True)
    def _inspect_worker(self, container) -> None:
        name = self._name_of(container)
        try:
            self.push_log(f"--- inspect: {name} ---", f"bold {GRUVBOX_YELLOW}")
            for line in self._format_inspect(container.attrs):
                self.push_log(line, GRUVBOX_FG)
        except Exception as exc:
            self.push_log(f"Could not inspect '{name}': {exc}", f"bold {GRUVBOX_RED}")

    @staticmethod
    def _format_inspect(attrs: dict) -> list[str]:
        return json.dumps(attrs, indent=2, sort_keys=True, default=str).splitlines()

    def action_logs_follow(self) -> None:
        if self._log_following:
            self._log_following = False
            self.push_log("Stopping log follow.", GRUVBOX_GRAY)
            return
        container = self._selected_container()
        if container is None:
            self.push_log("No container selected.", GRUVBOX_YELLOW)
            return
        self._log_following = True
        self._logs_follow_worker(container)

    @work(thread=True)
    def _logs_worker(self, container) -> None:
        name = self._name_of(container)
        try:
            self.push_log(f"--- recent logs: {name} ---", f"bold {GRUVBOX_YELLOW}")
            output = container.logs(tail=100, timestamps=True)
            lines = self._log_lines(output)
            if not lines:
                self.push_log("(no log output)", GRUVBOX_GRAY)
            for line in lines:
                self.push_log(line, GRUVBOX_FG)
        except Exception as exc:
            self.push_log(f"Could not read logs for '{name}': {exc}", f"bold {GRUVBOX_RED}")

    @work(thread=True)
    def _logs_follow_worker(self, container) -> None:
        name = self._name_of(container)
        try:
            self.push_log(f"--- following logs: {name} ---", f"bold {GRUVBOX_YELLOW}")
            stream = container.logs(stream=True, follow=True, tail=0, timestamps=True)
            for chunk in stream:
                if not self._log_following or self._quitting:
                    break
                for line in self._log_lines(chunk):
                    self.push_log(line, GRUVBOX_FG)
        except Exception as exc:
            self.push_log(f"Could not follow logs for '{name}': {exc}", f"bold {GRUVBOX_RED}")
        finally:
            self._log_following = False

    @staticmethod
    def _log_lines(output) -> list[str]:
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return str(output).splitlines()

    def _update_for_key(self, row_key) -> None:
        container = self._container_by_key.get(self._key_value(row_key))
        if container is None:
            return
        cid = container.id
        if cid in self._updating:
            self.push_log("Container is already being updated.", GRUVBOX_YELLOW)
            return
        self._updating.add(cid)
        self._set_upd_cell(cid, "updating")
        self._update_worker(container)

    @staticmethod
    def _name_of(container) -> str:
        return (container.name or container.id).lstrip("/")

    def _set_upd_cell(self, cid: str, state: str) -> None:
        try:
            self.table.update_cell(cid, "upd", self._update_cell(state))
        except Exception:
            pass

    # --- update sequence ---------------------------------------------------

    @work(thread=True)
    def _update_worker(self, container) -> None:
        succeeded = self._do_update(container)
        if not self._quitting:
            state = self._update_outcomes.pop(container.id, "up-to-date" if succeeded else "failed")
            self.call_from_thread(self._apply_update_outcome, self._name_of(container), state)
            self.call_from_thread(self._request_scan)

    @work(thread=True)
    def _update_all_worker(self, targets: list) -> None:
        ok = 0
        failed = 0
        for container in targets:
            succeeded = self._do_update(container)
            state = self._update_outcomes.pop(container.id, "up-to-date" if succeeded else "failed")
            if not self._quitting:
                self.push_log(
                    f"Update-all: {self._name_of(container)} -> {state}.",
                    f"bold {GRUVBOX_GREEN if state == 'up-to-date' else GRUVBOX_ORANGE}",
                )
                self.call_from_thread(self._apply_update_outcome, self._name_of(container), state)
            if succeeded:
                ok += 1
            else:
                failed += 1
        if not self._quitting:
            self.push_log(
                f"Update-all finished: {ok} updated, {failed} failed.",
                f"bold {GRUVBOX_GREEN if failed == 0 else GRUVBOX_ORANGE}",
            )
            self.call_from_thread(self._request_scan)

    def _apply_update_outcome(self, name: str, state: str) -> None:
        self._recent_update_status[name] = state
        for cid, container in self._container_by_key.items():
            if self._name_of(container) == name:
                self._update_status[cid] = state
                self._set_upd_cell(cid, state)
        if self._selected_id in self._container_by_key:
            self._update_details(self._selected_id)
        self._refresh_header()

    def _publish_update_stage(self, name: str, state: str) -> None:
        if self.table is not None and not self._quitting:
            self.call_from_thread(self._apply_update_stage, name, state)

    def _apply_update_stage(self, name: str, state: str) -> None:
        for cid, container in self._container_by_key.items():
            if self._name_of(container) == name:
                self._update_status[cid] = state
                self._set_upd_cell(cid, state)
        if self._selected_id in self._container_by_key:
            self._update_details(self._selected_id)

    def _do_update(self, container) -> bool:
        cid = container.id
        name = self._name_of(container)
        removed = False
        degraded = False
        run_kwargs = None
        original_image = None
        self._update_outcomes[cid] = "failed"
        try:
            if self.client is None:
                raise RuntimeError("No Docker connection.")
            self.push_log(f"== Updating '{name}' ==", f"bold {GRUVBOX_YELLOW}")
            container.reload()
            attrs = container.attrs
            image_ref = (attrs.get("Config") or {}).get("Image") or ""
            if not image_ref:
                raise RuntimeError("Container has no image reference; cannot update.")
            try:
                original_image = container.image.id
            except Exception:
                original_image = None
            run_kwargs, secondary_networks = self._build_run_kwargs(attrs)
            unsupported = self._unsupported_settings(attrs)
            if unsupported:
                fields = ", ".join(unsupported)
                raise RuntimeError(f"unsupported configuration: {fields}")

            self._publish_update_stage(name, "pulling")
            self.push_log(f"Pulling latest image: {image_ref}", GRUVBOX_ORANGE)
            try:
                self.client.images.pull(image_ref)
                self.push_log("Image pulled.", GRUVBOX_ORANGE)
            except docker.errors.NotFound as exc:
                self.push_log(f"Pull failed ({exc}); continuing with local image.", GRUVBOX_YELLOW)

            self._publish_update_stage(name, "stopping")
            self.push_log("Stopping container\u2026", GRUVBOX_ORANGE)
            container.stop(timeout=15)
            self._publish_update_stage(name, "removing")
            self.push_log("Removing container\u2026", GRUVBOX_ORANGE)
            container.remove()
            removed = True

            self._publish_update_stage(name, "recreating")
            self.push_log("Recreating container\u2026", GRUVBOX_ORANGE)
            new_container = self.client.containers.run(**run_kwargs)
            self._publish_update_stage(name, "restoring")
            for net_name, aliases in secondary_networks.items():
                try:
                    network = self.client.networks.get(net_name)
                    network.connect(new_container, aliases=aliases or None)
                    self.push_log(f"Attached to network '{net_name}'.", GRUVBOX_ORANGE)
                except Exception as exc:
                    degraded = True
                    self.push_log(f"Warning re-attaching network '{net_name}': {exc}", GRUVBOX_YELLOW)
            if degraded:
                self._update_outcomes[cid] = "degraded"
                self.push_log(
                    f"Container '{name}' updated with incomplete network restoration.",
                    f"bold {GRUVBOX_ORANGE}",
                )
                return False
            self.push_log(
                f"Container recreated successfully! '{self._name_of(new_container)}' "
                f"({new_container.short_id}).",
                f"bold {GRUVBOX_GREEN}",
            )
            return True
        except Exception as exc:
            self.push_log(f"Update failed for '{name}': {exc}", f"bold {GRUVBOX_RED}")
            if removed and run_kwargs is not None:
                rollback_kwargs = dict(run_kwargs)
                rollback_kwargs["image"] = original_image or image_ref
                try:
                    self.push_log(f"Attempting recovery of '{name}'…", GRUVBOX_YELLOW)
                    restored = self.client.containers.run(**rollback_kwargs)
                    self.push_log(
                        f"Recovery succeeded for '{name}' ({restored.short_id}).",
                        f"bold {GRUVBOX_GREEN}",
                    )
                    self._update_outcomes[cid] = "recovered"
                except Exception as recovery_exc:
                    self.push_log(
                        f"Recovery failed for '{name}': {recovery_exc}",
                        f"bold {GRUVBOX_RED}",
                    )
            return False
        finally:
            self._updating.discard(cid)

    def _build_run_kwargs(self, attrs: dict) -> tuple[dict, dict]:
        config = attrs.get("Config") or {}
        host = attrs.get("HostConfig") or {}
        net_settings = attrs.get("NetworkSettings") or {}
        networks = net_settings.get("Networks") or {}
        network_names = list(networks.keys())
        host_mode = host.get("NetworkMode") or ""
        kwargs: dict = {
            "image": config.get("Image"),
            "detach": True,
        }
        name = (attrs.get("Name") or "").lstrip("/")
        if name:
            kwargs["name"] = name
        if config.get("Cmd"):
            kwargs["command"] = config["Cmd"]
        if config.get("Entrypoint"):
            kwargs["entrypoint"] = config["Entrypoint"]
        if config.get("WorkingDir"):
            kwargs["working_dir"] = config["WorkingDir"]
        for config_key, run_key in (
            ("User", "user"),
            ("Hostname", "hostname"),
            ("Healthcheck", "healthcheck"),
        ):
            if config_key in config and config[config_key] is not None:
                kwargs[run_key] = config[config_key]
        for config_key, run_key in (
            ("Tty", "tty"),
            ("OpenStdin", "stdin_open"),
            ("Privileged", "privileged"),
            ("StopSignal", "stop_signal"),
            ("StopTimeout", "stop_timeout"),
        ):
            if config_key in config:
                kwargs[run_key] = config[config_key]

        env = self._parse_env(config.get("Env"))
        if env:
            kwargs["environment"] = env

        labels = config.get("Labels")
        if labels:
            kwargs["labels"] = labels

        ports = self._parse_ports(host.get("PortBindings"))
        if ports:
            kwargs["ports"] = ports

        mounts = attrs.get("Mounts")
        volumes = self._parse_mounts(mounts) if mounts is not None else self._parse_binds(host.get("Binds"))
        if volumes:
            kwargs["volumes"] = volumes

        restart = host.get("RestartPolicy")
        if restart and restart.get("Name"):
            kwargs["restart_policy"] = restart
        for host_key, run_key in (
            ("CapAdd", "cap_add"),
            ("CapDrop", "cap_drop"),
            ("Devices", "devices"),
            ("Dns", "dns"),
            ("ExtraHosts", "extra_hosts"),
            ("Tmpfs", "tmpfs"),
        ):
            if host_key in host and host[host_key] is not None:
                kwargs[run_key] = host[host_key]
        for host_key, run_key in (
            ("Memory", "mem_limit"),
            ("MemorySwap", "memswap_limit"),
            ("CpuShares", "cpu_shares"),
            ("CpuPeriod", "cpu_period"),
            ("CpuQuota", "cpu_quota"),
            ("CpusetCpus", "cpuset_cpus"),
            ("BlkioWeight", "blkio_weight"),
            ("PidsLimit", "pids_limit"),
            ("Ulimits", "ulimits"),
            ("ShmSize", "shm_size"),
            ("SecurityOpt", "security_opt"),
            ("UsernsMode", "userns_mode"),
            ("IpcMode", "ipc_mode"),
            ("LogConfig", "log_config"),
            ("Sysctls", "sysctls"),
            ("Init", "init"),
            ("AutoRemove", "auto_remove"),
            ("CgroupnsMode", "cgroupns"),
            ("Runtime", "runtime"),
            ("GroupAdd", "group_add"),
            ("OomKillDisable", "oom_kill_disable"),
            ("OomScoreAdj", "oom_score_adj"),
            ("ReadonlyRootfs", "read_only"),
            ("StorageOpt", "storage_opt"),
            ("VolumeDriver", "volume_driver"),
        ):
            if host_key in host and host[host_key] is not None:
                kwargs[run_key] = host[host_key]

        # Networking: attach to the container's first network at create time,
        # then re-attach the remaining ones (with their aliases) afterwards so
        # Compose-managed networks and DNS aliases survive the update.
        secondary: dict = {}
        if network_names and not host_mode.startswith("container:"):
            primary = network_names[0]
            kwargs["network_mode"] = primary
            for net_name in network_names[1:]:
                info = networks[net_name] or {}
                secondary[net_name] = list(info.get("Aliases") or [])
        elif host_mode:
            kwargs["network_mode"] = host_mode

        return kwargs, secondary

    @staticmethod
    def _unsupported_settings(attrs: dict) -> list[str]:
        config = attrs.get("Config") or {}
        host = attrs.get("HostConfig") or {}
        unsupported = []
        if config.get("Secrets"):
            unsupported.append("Config.Secrets")
        if config.get("Configs"):
            unsupported.append("Config.Configs")
        if host.get("SecretReferences"):
            unsupported.append("HostConfig.SecretReferences")
        return unsupported

    @staticmethod
    def _parse_env(env_list) -> dict:
        result = {}
        for item in env_list or []:
            if "=" in item:
                key, _, value = item.partition("=")
                result[key] = value
        return result

    @staticmethod
    def _parse_ports(bindings) -> dict:
        result = {}
        for container_port, rules in (bindings or {}).items():
            parsed = []
            for rule in rules or []:
                if not rule or not rule.get("HostPort"):
                    continue
                host_port = rule["HostPort"]
                host_ip = rule.get("HostIp")
                parsed.append((host_ip, host_port) if host_ip else host_port)
            if not parsed:
                continue
            result[container_port] = parsed if len(parsed) > 1 else parsed[0]
        return result

    @staticmethod
    def _parse_binds(binds) -> dict:
        result = {}
        for spec in binds or []:
            parts = spec.split(":")
            if len(parts) == 2:
                source, target = parts
                mode = "rw"
            elif len(parts) > 2:
                source = ":".join(parts[:-2])
                target, mode = parts[-2], parts[-1]
            else:
                continue
            result[source] = {"bind": target, "mode": mode}
        return result

    @staticmethod
    def _parse_mounts(mounts) -> dict:
        result = {}
        for mount in mounts or []:
            source = mount.get("Name") or mount.get("Source")
            target = mount.get("Destination")
            if not source or not target:
                continue
            mode = mount.get("Mode") or ("rw" if mount.get("RW", True) else "ro")
            result[source] = {"bind": target, "mode": mode}
        return result


if __name__ == "__main__":
    DockerTUI().run()