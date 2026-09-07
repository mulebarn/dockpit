"""DOCKPIT - a btop/htop-inspired Docker container manager built with Textual."""

import threading
from datetime import datetime, timezone

import docker
import docker.errors
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, RichLog, Static

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

STATUS_DISPLAY = {
    "running": ("\u25cf running", GRUVBOX_GREEN),
    "exited": ("\u25a0 stopped", GRUVBOX_RED),
    "dead": ("\u25a0 dead", GRUVBOX_RED),
    "created": ("\u25a3 created", GRUVBOX_YELLOW),
    "paused": ("\u23f8 paused", GRUVBOX_AQUA),
}

UPDATE_DISPLAY = {
    "update": ("\u25b2 update avail", GRUVBOX_ORANGE),
    "up-to-date": ("\u2714 current", GRUVBOX_GREEN),
    "local": ("\u00b7 local only", GRUVBOX_GRAY),
    "unknown": ("? unknown", GRUVBOX_GRAY),
    "updating": ("\u21bb updating", GRUVBOX_AQUA),
}
UPDATE_GLYPH = {key: value[0].split()[0] for key, value in UPDATE_DISPLAY.items()}
UPDATE_COLOR = {key: value[1] for key, value in UPDATE_DISPLAY.items()}

DETAIL_IDS = (
    "d-name",
    "d-image",
    "d-id",
    "d-status",
    "d-created",
    "d-ports",
    "d-stats",
    "d-update",
)


class DockerTUI(App):
    """btop/htop-inspired Docker container TUI in a vintage terminal palette."""

    TITLE = "DOCKPIT"
    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("u", "update_selected", "Update"),
        Binding("U", "update_all", "Update all"),
        Binding("r", "rescan", "Refresh"),
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
        self._update_status: dict[str, str] = {}
        self._stats_in_flight = False
        self._check_in_flight = False

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Static("D O C K P I T", id="app-title"),
            Static("starting\u2026", id="container-stats"),
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
                Static("", id="d-created", classes="detail-row"),
                Static("", id="d-ports", classes="detail-row"),
                Static("", id="d-stats", classes="detail-row"),
                Static("", id="d-update", classes="detail-row"),
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
        self.table.add_column("CPU", key="cpu", width=8)
        self.table.add_column("MEM", key="mem", width=8)
        self.table.add_column("UPD", key="upd", width=5)
        self.push_log("Welcome to DOCKPIT.", f"bold {GRUVBOX_YELLOW}")
        try:
            self.client = docker.from_env()
            version = (self.client.version() or {}).get("Version", "?")
        except Exception as exc:
            self.client = None
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
        self._scan_worker()
        self.table.focus()
        self.set_interval(2.0, self._on_stats_timer)
        self.set_interval(60.0, self._on_update_check_timer)

    # --- logging -----------------------------------------------------------

    def push_log(self, message: str, style: str = "") -> None:
        if self.output is None:
            return
        if self._thread_id == threading.get_ident():
            self._write_log(message, style)
        else:
            self.call_from_thread(self._write_log, message, style)

    def _write_log(self, message: str, style: str = "") -> None:
        self.output.write(Text(message, style=style))

    # --- scanning ----------------------------------------------------------

    @work(thread=True)
    def _scan_worker(self) -> None:
        if self.client is None:
            self.push_log("No Docker connection - press r to retry.", f"bold {GRUVBOX_RED}")
            return
        try:
            containers = sorted(
                self.client.containers.list(all=True),
                key=lambda c: (c.status != "running", (c.name or c.id).lstrip("/")),
            )
        except Exception as exc:
            self.push_log(f"Failed to list containers: {exc}", f"bold {GRUVBOX_RED}")
            containers = []
        self.call_from_thread(self._populate_table, containers)

    def _populate_table(self, containers: list) -> None:
        prev_selected = self._selected_id if self._selected_id in self._container_by_key else None
        self._container_by_key.clear()
        self._update_status.clear()
        self.table.clear()
        for container in containers:
            self._add_row(container)
        if not containers:
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
                self.table.move_cursor(row=0)
        self._refresh_header()
        self._update_check_tick()
        self._on_stats_timer()

    def _add_row(self, container) -> None:
        attrs = container.attrs
        name = (container.name or container.id).lstrip("/")
        image = (attrs.get("Config") or {}).get("Image") or "?"
        status = container.status or (attrs.get("State") or {}).get("Status") or "unknown"
        status_label, status_color = STATUS_DISPLAY.get(status, (f"? {status}", GRUVBOX_YELLOW))
        self.table.add_row(
            Text(name, style=f"bold {GRUVBOX_FG}"),
            Text(f"{status_label}", style=f"bold {status_color}"),
            Text("\u2013", style=GRUVBOX_GRAY),
            Text("\u2013", style=GRUVBOX_GRAY),
            Text("\u2013", style=GRUVBOX_GRAY),
            key=container.id,
        )
        self._container_by_key[container.id] = container

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
        self._update_check_worker()

    @work(thread=True)
    def _update_check_worker(self) -> None:
        results = {}
        try:
            for cid, container in list(self._container_by_key.items()):
                results[cid] = self._check_update(container)
        finally:
            self.call_from_thread(self._apply_update_status, results)

    def _check_update(self, container) -> str:
        if self.client is None:
            return "unknown"
        try:
            ref = (container.attrs.get("Config") or {}).get("Image") or ""
            if not ref or "@" in ref:
                return "unknown"
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
                return "local"
            local = repo_digests[0].rsplit("@", 1)[-1]
            if not local.startswith("sha256:"):
                return "unknown"
            data = self.client.images.get_registry_data(ref)
            attrs = getattr(data, "attrs", None) or {}
            descriptor = attrs.get("Descriptor") or {}
            remote = descriptor.get("digest") or attrs.get("Digest")
            if not remote:
                return "unknown"
            return "up-to-date" if local == remote else "update"
        except Exception:
            return "unknown"

    def _apply_update_status(self, results: dict) -> None:
        self._check_in_flight = False
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
        self._stats_worker()

    @work(thread=True)
    def _stats_worker(self) -> None:
        snapshot = {}
        for cid, container in list(self._container_by_key.items()):
            if container.status != "running":
                snapshot[cid] = None
                continue
            try:
                sample = container.stats(stream=False)
                snapshot[cid] = (self._compute_cpu(sample), self._compute_mem(sample))
            except Exception:
                snapshot[cid] = None
        self.call_from_thread(self._apply_stats, snapshot)

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

    def _apply_stats(self, snapshot: dict) -> None:
        self._stats_in_flight = False
        for cid, pair in snapshot.items():
            self._stats[cid] = pair
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
        if value is None:
            return Text("\u2013", style=f"bold {GRUVBOX_GRAY}")
        return Text(self._format_pct(value), style=f"bold {self._pct_color(value)}")

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

    # --- header ------------------------------------------------------------

    def _refresh_header(self) -> None:
        items = list(self._container_by_key.values())
        running = sum(1 for c in items if c.status == "running")
        updates = sum(1 for state in self._update_status.values() if state == "update")
        values = [pair for pair in self._stats.values() if pair is not None]
        cpus = [cpu for cpu, _mem in values if cpu is not None]
        mems = [mem for _cpu, mem in values if mem is not None]
        avg_cpu = sum(cpus) / len(cpus) if cpus else None
        avg_mem = sum(mems) / len(mems) if mems else None
        suffix = "" if updates == 1 else "s"

        header = Text(
            f"{len(items)} containers | {running} running | {updates} update{suffix} | "
            "CPU ",
            style="bold",
        )
        header.append(self._gauge_pct(avg_cpu, width=6))
        header.append("  MEM ", style="bold")
        header.append(self._gauge_pct(avg_mem, width=6))
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
        cpu_text = "\u2013" if cpu is None else f"{cpu:.1f}%"
        mem_text = "\u2013" if mem is None else self._format_pct(mem)
        state = self._update_status.get(cid, "unknown")
        state_text, state_color = UPDATE_DISPLAY.get(state, UPDATE_DISPLAY["unknown"])
        status_label, status_color = STATUS_DISPLAY.get(status, (f"? {status}", GRUVBOX_YELLOW))

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
        self._set_detail("d-created", ("UP     ", GRUVBOX_GRAY), (self._fmt_created(created), GRUVBOX_FG))
        self._set_detail("d-ports", ("PORTS  ", GRUVBOX_GRAY), (ports, GRUVBOX_FG))
        self._set_detail("d-stats", (self._stats_block(cpu, mem, cpu_text, mem_text), ""))
        self._set_detail("d-update", ("UPDATE ", GRUVBOX_GRAY), (state_text, f"bold {state_color}"))

    def _stats_block(self, cpu: float | None, mem: float | None, cpu_text: str, mem_text: str) -> Text:
        text = Text()
        for label, value, display in (("CPU", cpu, cpu_text), ("MEM", mem, mem_text)):
            color = f"bold {self._pct_color(value)}"
            text.append(f"{label} [", style=GRUVBOX_GRAY)
            text.append(self._gauge(value, width=10), style=color)
            text.append("]  ", style=GRUVBOX_GRAY)
            text.append(display, style=color)
            text.append("\n", style=GRUVBOX_GRAY)
        return text

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
        self._scan_worker()

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
        self._updating.add(cid)
        self._set_upd_cell(cid, "updating")
        self._update_worker(self._container_by_key[cid])

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
        self.push_log(f"Updating {len(targets)} container(s): {names}", f"bold {GRUVBOX_ORANGE}")
        for container in targets:
            self._updating.add(container.id)
            self._set_upd_cell(container.id, "updating")
        self._update_all_worker(targets)

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
            self.table.update_cell(cid, "upd", Text(UPDATE_GLYPH[state], style=f"bold {UPDATE_COLOR[state]}"))
        except Exception:
            pass

    # --- update sequence ---------------------------------------------------

    @work(thread=True)
    def _update_worker(self, container) -> None:
        self._do_update(container)
        self.call_from_thread(self._scan_worker)

    @work(thread=True)
    def _update_all_worker(self, targets: list) -> None:
        ok = 0
        failed = 0
        for container in targets:
            if self._do_update(container):
                ok += 1
            else:
                failed += 1
        self.push_log(
            f"Update-all finished: {ok} updated, {failed} failed.",
            f"bold {GRUVBOX_GREEN if failed == 0 else GRUVBOX_ORANGE}",
        )
        self.call_from_thread(self._scan_worker)

    def _do_update(self, container) -> bool:
        cid = container.id
        name = self._name_of(container)
        try:
            if self.client is None:
                raise RuntimeError("No Docker connection.")
            self.push_log(f"== Updating '{name}' ==", f"bold {GRUVBOX_YELLOW}")
            container.reload()
            attrs = container.attrs
            image_ref = (attrs.get("Config") or {}).get("Image") or ""
            if not image_ref:
                raise RuntimeError("Container has no image reference; cannot update.")

            self.push_log(f"Pulling latest image: {image_ref}", GRUVBOX_ORANGE)
            try:
                self.client.images.pull(image_ref)
                self.push_log("Image pulled.", GRUVBOX_ORANGE)
            except docker.errors.NotFound as exc:
                self.push_log(f"Pull failed ({exc}); continuing with local image.", GRUVBOX_YELLOW)

            self.push_log("Stopping container\u2026", GRUVBOX_ORANGE)
            container.stop(timeout=15)
            self.push_log("Removing container\u2026", GRUVBOX_ORANGE)
            container.remove()

            kwargs, secondary_networks = self._build_run_kwargs(attrs)
            self.push_log("Recreating container\u2026", GRUVBOX_ORANGE)
            new_container = self.client.containers.run(**kwargs)
            for net_name, aliases in secondary_networks.items():
                try:
                    network = self.client.networks.get(net_name)
                    network.connect(new_container, aliases=aliases or None)
                    self.push_log(f"Attached to network '{net_name}'.", GRUVBOX_ORANGE)
                except Exception as exc:
                    self.push_log(f"Warning re-attaching network '{net_name}': {exc}", GRUVBOX_YELLOW)
            self.push_log(
                f"Container recreated successfully! '{self._name_of(new_container)}' "
                f"({new_container.short_id}).",
                f"bold {GRUVBOX_GREEN}",
            )
            return True
        except Exception as exc:
            self.push_log(f"Update failed for '{name}': {exc}", f"bold {GRUVBOX_RED}")
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

        env = self._parse_env(config.get("Env"))
        if env:
            kwargs["environment"] = env

        labels = config.get("Labels")
        if labels:
            kwargs["labels"] = labels

        ports = self._parse_ports(host.get("PortBindings"))
        if ports:
            kwargs["ports"] = ports

        volumes = self._parse_binds(host.get("Binds"))
        if volumes:
            kwargs["volumes"] = volumes

        restart = host.get("RestartPolicy")
        if restart and restart.get("Name"):
            kwargs["restart_policy"] = restart

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
            host_ports = [r.get("HostPort") for r in rules or [] if r and r.get("HostPort")]
            if not host_ports:
                continue
            result[container_port] = host_ports if len(host_ports) > 1 else host_ports[0]
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


if __name__ == "__main__":
    DockerTUI().run()