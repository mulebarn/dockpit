import asyncio
from datetime import datetime, timezone

import docker.errors

import app as app_module
from app import DockerTUI

REMOTE_NGINX = "sha256:newnginx"
REMOTE_ALPINE = "sha256:alpinev1"

STATS_SAMPLE = {
    "cpu_stats": {
        "cpu_usage": {"total_usage": 1000, "percpu_usage": [500, 500]},
        "system_cpu_usage": 20000,
        "online_cpus": 2,
    },
    "precpu_stats": {
        "cpu_usage": {"total_usage": 900, "percpu_usage": [450, 450]},
        "system_cpu_usage": 19000,
        "online_cpus": 2,
    },
    "memory_stats": {"usage": 512 * 1024 * 1024, "limit": 1024 * 1024 * 1024},
}


class FakeRegistryData:
    def __init__(self, digest):
        self.attrs = {"Descriptor": {"digest": digest}}


class FakeImage:
    def __init__(self, ref, local_digest, remote_digest):
        self.ref = ref
        self.local_digest = local_digest
        self.remote_digest = remote_digest
        self.id = local_digest or "sha256:localimage"
        self.attrs = {"RepoDigests": [f"{ref}@{local_digest}"]} if local_digest else {"RepoDigests": []}


class FakeContainer:
    _counter = 0

    def __init__(self, name, image_ref, status="running", image=None):
        FakeContainer._counter += 1
        self.name = f"/{name}"
        self.id = f"id_{name}"
        self.status = status
        self.short_id = self.id[:12]
        self._removed = False
        self._image = image
        self.attrs = {
            "Id": self.id,
            "Name": f"/{name}",
            "Created": datetime.now(timezone.utc).isoformat(),
            "Image": image.id if image else "sha256:localbase",
            "Config": {
                "Image": image_ref,
                "Cmd": ["nginx", "-g", "daemon off;"],
                "Entrypoint": "/docker-entrypoint.sh",
                "WorkingDir": "/app",
                "Env": ["FOO=bar", "BAZ=qux"],
                "ExposedPorts": {"80/tcp": {}},
            },
            "HostConfig": {
                "PortBindings": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]},
                "Binds": ["/srv/data:/data:ro"],
                "RestartPolicy": {"Name": "always"},
                "NetworkMode": "bridge",
            },
            "NetworkSettings": {
                "Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}
            },
        }

    def reload(self):
        return None

    @property
    def image(self):
        return self._image

    def stop(self, timeout=10):
        if self._client._fail_stop_count:
            self._client._fail_stop_count -= 1
            raise docker.errors.APIError("fake stop failure", explanation="fake")
        self.status = "exited"
        return True

    def start(self):
        self.status = "running"
        return True

    def restart(self):
        self.status = "running"
        return True

    def pause(self):
        self.status = "paused"
        return True

    def logs(self, tail=100, timestamps=False, stream=False, follow=False):
        output = b"2026-09-06T12:00:00Z server started\n2026-09-06T12:00:01Z ready\n"
        return iter(output.splitlines(keepends=True)) if stream else output

    def remove(self):
        if self._client._fail_remove_count:
            self._client._fail_remove_count -= 1
            raise docker.errors.APIError("fake remove failure", explanation="fake")
        self._removed = True

    def stats(self, stream=False):
        if self.status != "running":
            raise docker.errors.APIError("container is not running", explanation="fake")
        return STATS_SAMPLE


class FakeNetwork:
    def __init__(self, name):
        self.name = name
        self.connections = []

    def connect(self, container, **kwargs):
        if self._client._fail_network_count:
            self._client._fail_network_count -= 1
            raise docker.errors.APIError("fake network failure", explanation="fake")
        self.connections.append((container, kwargs))


class FakeNetworks:
    def __init__(self, client):
        self._client = client

    def get(self, name):
        net = next((n for n in self._client._networks if n.name == name), None)
        if net is None:
            raise docker.errors.NotFound(f"network {name} not found")
        net._client = self._client
        return net


class FakeImages:
    def __init__(self, client):
        self._client = client

    def get(self, ref):
        image = next((i for i in self._client._images if i.ref == ref), None)
        if image is None:
            raise docker.errors.ImageNotFound("fake image not found")
        return image

    def get_registry_data(self, name, auth_config=None):
        ref = name.split("@", 1)[0]
        image = next((i for i in self._client._images if i.ref == ref), None)
        if image is None:
            raise docker.errors.ImageNotFound("fake image not found")
        if not image.remote_digest:
            raise docker.errors.APIError("no remote registry entry", explanation="fake")
        return FakeRegistryData(image.remote_digest)

    def pull(self, ref):
        if self._client._fail_pull_count:
            self._client._fail_pull_count -= 1
            raise docker.errors.NotFound("fake pull failure")
        image = next((i for i in self._client._images if i.ref == ref), None)
        if image is None:
            raise docker.errors.NotFound("fake image not found")
        image.local_digest = image.remote_digest
        return image


class FakeContainers:
    def __init__(self, client):
        self._client = client

    def list(self, all=True):
        return [c for c in self._client._containers if not c._removed]

    def run(self, **kwargs):
        ref = kwargs["image"]
        if self._client._fail_run_count:
            self._client._fail_run_count -= 1
            raise docker.errors.APIError("fake recreate failure", explanation="fake")
        image = next((i for i in self._client._images if i.ref == ref or i.id == ref), None)
        container = FakeContainer(kwargs["name"], ref, image=image, status="running")
        if kwargs.get("labels"):
            container.attrs.setdefault("Config", {})["Labels"] = kwargs["labels"]
        if kwargs.get("working_dir"):
            container.attrs.setdefault("Config", {})["WorkingDir"] = kwargs["working_dir"]
        self._client._last_run_kwargs = kwargs
        self._client._containers.append(container)
        container._client = self._client
        return container


class FakeDockerClient:
    def __init__(self, containers, images):
        self._containers = containers
        self._images = images
        self._networks = []
        self._last_run_kwargs = None
        self._fail_run_count = 0
        self._fail_pull_count = 0
        self._fail_stop_count = 0
        self._fail_remove_count = 0
        self._fail_network_count = 0
        self.containers = FakeContainers(self)
        self.images = FakeImages(self)
        self.networks = FakeNetworks(self)
        for container in self._containers:
            container._client = self

    def version(self):
        return {"Version": "29.8.0"}


def make_client():
    images = [
        FakeImage("nginx:latest", "sha256:oldnginx", REMOTE_NGINX),
        FakeImage("alpine:3.19", "sha256:alpinev1", "sha256:alpinev1"),
    ]
    containers = [
        FakeContainer("web", "nginx:latest", image=images[0], status="running"),
        FakeContainer("util", "alpine:3.19", image=images[1], status="exited"),
    ]
    return FakeDockerClient(containers, images)


def test_stale_worker_results_are_ignored():
    app = DockerTUI()
    app._scan_generation = 2
    app._stats_in_flight = True
    app._check_in_flight = True

    app._populate_table([], generation=1)
    app._apply_stats({"stale": (99.0, 99.0)}, generation=1)
    app._apply_update_status({"stale": "update"}, generation=1)

    assert app._container_by_key == {}
    assert app._stats == {}
    assert app._update_status == {}
    assert not app._stats_in_flight
    assert not app._check_in_flight


def test_container_filter_matches_name_image_and_status():
    app = DockerTUI()
    client = make_client()
    web, util = client._containers

    assert app._matches_filter(web)
    app._filter_text = "nginx"
    assert app._matches_filter(web)
    assert not app._matches_filter(util)
    app._filter_text = "exited"
    assert app._matches_filter(util)
    assert not app._matches_filter(web)


def test_health_and_restart_metadata_display():
    app = DockerTUI()
    attrs = make_client()._containers[0].attrs
    attrs["State"] = {"Health": {"Status": "healthy"}}
    assert app._health_display(attrs) == ("\u2714 healthy", app_module.GRUVBOX_GREEN)
    assert app._restart_policy(attrs) == "always"
    attrs["State"]["Health"]["Status"] = "unhealthy"
    assert app._health_display(attrs)[0] == "\u2716 unhealthy"


def test_update_display_includes_staged_states():
    assert {"pulling", "stopping", "removing", "recreating", "restoring"}.issubset(
        app_module.UPDATE_DISPLAY
    )


def test_unmount_clears_worker_flags():
    app = DockerTUI()
    app._stats_in_flight = True
    app._check_in_flight = True

    app.on_unmount()

    assert app._quitting
    assert not app._stats_in_flight
    assert not app._check_in_flight


def test_update_recovers_after_recreate_failure():
    client = make_client()
    client._fail_run_count = 1
    app = DockerTUI()
    app.client = client
    original = next(c for c in client._containers if c.name == "/web")

    result = app._do_update(original)

    assert not result
    assert original._removed
    active_web = [c for c in client.containers.list() if c.name == "/web"]
    assert len(active_web) == 1, "failed update left the service unrecovered"
    assert active_web[0].image.id == "sha256:oldnginx"
    assert app._update_outcomes[original.id] == "recovered"


def test_update_failure_policies():
    client = make_client()
    client._fail_pull_count = 1
    app = DockerTUI()
    app.client = client
    original = next(c for c in client._containers if c.name == "/web")
    assert app._do_update(original), "pull NotFound should fall back to local image"

    client = make_client()
    client._fail_stop_count = 1
    app.client = client
    original = next(c for c in client._containers if c.name == "/web")
    assert not app._do_update(original)
    assert not original._removed
    assert [c.name for c in client.containers.list()].count("/web") == 1
    assert app._update_outcomes[original.id] == "failed"

    client = make_client()
    client._fail_remove_count = 1
    app.client = client
    original = next(c for c in client._containers if c.name == "/web")
    assert not app._do_update(original)
    assert not original._removed
    assert [c.name for c in client.containers.list()].count("/web") == 1
    assert app._update_outcomes[original.id] == "failed"

    client = make_client()
    client._networks = [FakeNetwork("primary"), FakeNetwork("secondary")]
    original = next(c for c in client._containers if c.name == "/web")
    original.attrs["NetworkSettings"]["Networks"] = {
        "primary": {"Aliases": ["web"]},
        "secondary": {"Aliases": ["web-secondary"]},
    }
    client._fail_network_count = 1
    app.client = client
    assert not app._do_update(original)
    assert [c.name for c in client.containers.list()].count("/web") == 1
    assert app._update_outcomes[original.id] == "degraded"


def test_build_run_kwargs_preserves_runtime_settings():
    app = DockerTUI()
    attrs = make_client()._containers[0].attrs
    attrs["Config"].update(
        {
            "User": "1000:1000",
            "Hostname": "dockpit-web",
            "Healthcheck": {"Test": ["CMD-SHELL", "curl localhost"]},
            "Tty": True,
            "OpenStdin": True,
            "Privileged": False,
        }
    )
    attrs["HostConfig"].update(
        {
            "CapAdd": ["NET_ADMIN"],
            "CapDrop": ["MKNOD"],
            "Devices": [{"PathOnHost": "/dev/fuse", "PathInContainer": "/dev/fuse", "CgroupPermissions": "rwm"}],
            "Dns": ["1.1.1.1"],
            "ExtraHosts": {"host.docker.internal": "host-gateway"},
            "Tmpfs": {"/run": "rw,noexec"},
        }
    )

    kwargs, _secondary = app._build_run_kwargs(attrs)

    assert kwargs["user"] == "1000:1000"
    assert kwargs["hostname"] == "dockpit-web"
    assert kwargs["healthcheck"]["Test"][0] == "CMD-SHELL"
    assert kwargs["tty"] is True and kwargs["stdin_open"] is True
    assert kwargs["privileged"] is False
    assert kwargs["cap_add"] == ["NET_ADMIN"]
    assert kwargs["cap_drop"] == ["MKNOD"]
    assert kwargs["devices"][0]["PathInContainer"] == "/dev/fuse"
    assert kwargs["dns"] == ["1.1.1.1"]
    assert kwargs["extra_hosts"]["host.docker.internal"] == "host-gateway"
    assert kwargs["tmpfs"]["/run"] == "rw,noexec"


def test_log_output_decodes_bytes_and_preserves_lines():
    assert DockerTUI._log_lines(b"first\nsecond\n") == ["first", "second"]
    assert DockerTUI._log_lines("single line") == ["single line"]
    assert DockerTUI._log_lines(b"\xff\n") == ["\ufffd"]


def test_log_filter_matches_case_insensitively():
    app = DockerTUI()
    app._log_filter_text = "error"
    assert app._log_matches_filter("Worker ERROR: failed")
    assert not app._log_matches_filter("Worker completed")


def test_log_history_is_bounded():
    app = DockerTUI()
    app._log_history = [(str(index), "") for index in range(app_module.MAX_LOG_HISTORY)]
    app.push_log("latest")
    assert len(app._log_history) == app_module.MAX_LOG_HISTORY
    assert app._log_history[0][0] == "1"
    assert app._log_history[-1][0] == "latest"


def test_inspect_output_is_sorted_json_lines():
    lines = DockerTUI._format_inspect({"z": 1, "a": {"value": True}})
    assert lines[0] == "{"
    assert '"a": {' in lines[1]
    assert '"z": 1' in lines[-2]


def test_build_run_kwargs_preserves_resources_and_structured_mounts():
    app = DockerTUI()
    attrs = make_client()._containers[0].attrs
    attrs["Mounts"] = [
        {"Type": "volume", "Name": "dockpit_data", "Destination": "/var/lib/app", "RW": True},
        {"Type": "bind", "Source": "/srv/cache", "Destination": "/cache", "Mode": "ro", "RW": False},
    ]
    attrs["HostConfig"].update(
        {
            "Memory": 536870912,
            "MemorySwap": 1073741824,
            "CpuShares": 512,
            "CpuPeriod": 100000,
            "CpuQuota": 50000,
            "CpusetCpus": "0-1",
            "BlkioWeight": 500,
            "PidsLimit": 128,
            "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 4096}],
            "ShmSize": 67108864,
            "SecurityOpt": ["no-new-privileges:true"],
            "UsernsMode": "host",
            "IpcMode": "private",
            "ConsoleSize": [0, 0],
        }
    )

    kwargs, _secondary = app._build_run_kwargs(attrs)

    assert kwargs["volumes"]["dockpit_data"] == {"bind": "/var/lib/app", "mode": "rw"}
    assert kwargs["volumes"]["/srv/cache"] == {"bind": "/cache", "mode": "ro"}
    assert kwargs["mem_limit"] == 536870912
    assert kwargs["memswap_limit"] == 1073741824
    assert kwargs["cpu_shares"] == 512
    assert kwargs["cpu_period"] == 100000
    assert kwargs["cpu_quota"] == 50000
    assert kwargs["cpuset_cpus"] == "0-1"
    assert kwargs["blkio_weight"] == 500
    assert kwargs["pids_limit"] == 128
    assert kwargs["ulimits"][0]["Name"] == "nofile"
    assert kwargs["shm_size"] == 67108864
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["userns_mode"] == "host"
    assert kwargs["ipc_mode"] == "private"
    assert "console_size" not in kwargs


def test_update_rejects_unsupported_settings_before_mutation():
    client = make_client()
    app = DockerTUI()
    app.client = client
    original = next(c for c in client._containers if c.name == "/web")
    original.attrs["Config"]["Secrets"] = [{"Target": "/run/secrets/app_key"}]

    assert app._unsupported_settings(original.attrs) == [
        "Config.Secrets",
    ]
    assert not app._do_update(original)
    assert not original._removed
    assert [c.name for c in client.containers.list()].count("/web") == 1
    assert app._update_outcomes[original.id] == "failed"


async def test_stats_none_pair_regressions():
    state = {"client": None}

    def from_env(*args, **kwargs):
        if state["client"] is None:
            state["client"] = make_client()
        return state["client"]

    app_module.docker.from_env = from_env
    app = DockerTUI()
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause(0.4)
        client = state["client"]
        web = next(c for c in client._containers if c.name == "/web")
        util = next(c for c in client._containers if c.name == "/util")

        app._stats[util.id] = (0.0, None)
        app._stats[web.id] = (101.2, None)
        try:
            app._refresh_header()
        except TypeError as exc:
            raise AssertionError(f"header crashed on None mem pair: {exc}")
        header = app.stats.content if isinstance(app.stats.content, str) else str(app.stats.content)
        assert "MEM" in header and "CPU" in header, header

        snapshot = {web.id: (20.0, 0.4), util.id: (0.0, None)}
        try:
            app._apply_stats(snapshot)
        except TypeError as exc:
            raise AssertionError(f"apply_stats crashed on None pair: {exc}")
        header = app.stats.content if isinstance(app.stats.content, str) else str(app.stats.content)
        assert "CPU" in header and "MEM" in header, header
        assert "\u2588" in header and "\u2591" in header, "header should render block gauges"


async def test_update_preserves_compose_config():
    state = {"client": None}

    def from_env(*args, **kwargs):
        if state["client"] is None:
            state["client"] = make_client()
            client = state["client"]
            client._networks = [FakeNetwork("immich_default"), FakeNetwork("db_net")]
            web = next(c for c in client._containers if c.name == "/web")
            web.attrs["Config"]["Labels"] = {
                "com.docker.compose.project": "immich",
                "com.docker.compose.service": "server",
            }
            web.attrs["Config"]["WorkingDir"] = "/usr/src/server"
            web.attrs["NetworkSettings"]["Networks"] = {
                "immich_default": {"Aliases": ["server", "immich-server"]},
                "db_net": {"Aliases": ["db-client"]},
            }
        return state["client"]

    app_module.docker.from_env = from_env
    client = from_env()
    app = DockerTUI()
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause(0.4)
        db_net = client.networks.get("db_net")
        await pilot.press("u")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(1.2)

        web = next(c for c in client.containers.list() if c.name == "/web")
        labels = (web.attrs.get("Config") or {}).get("Labels") or {}
        assert labels.get("com.docker.compose.project") == "immich", labels
        assert labels.get("com.docker.compose.service") == "server", labels
        assert (web.attrs.get("Config") or {}).get("WorkingDir") == "/usr/src/server"
        assert client._last_run_kwargs.get("network_mode") == "immich_default", client._last_run_kwargs
        assert client._last_run_kwargs.get("labels", {}).get("com.docker.compose.project") == "immich"
        assert db_net.connections, "secondary network was not re-attached"
        assert db_net.connections[0][1].get("aliases") == ["db-client"], db_net.connections


async def run_main():
    state = {"client": None}

    def from_env(*args, **kwargs):
        if state["client"] is None:
            state["client"] = make_client()
        return state["client"]

    app_module.docker.from_env = from_env
    client = from_env()
    app = DockerTUI()
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause(0.4)
        assert app.table.row_count == 2, f"expected 2 rows, got {app.table.row_count}"
        assert sorted(app._container_by_key) == ["id_util", "id_web"]

        await pilot.pause(1.0)
        assert app._stats.get("id_web") == (20.0, 50.0), app._stats
        assert app._stats.get("id_util") is None and "id_util" in app._stats

        assert app._update_status.get("id_web") == "update", app._update_status
        assert app._update_status.get("id_util") == "up-to-date", app._update_status
        header = app.stats.content if isinstance(app.stats.content, str) else str(app.stats.content)
        assert "2 containers" in header and "1 update" in header, header
        assert "CPU" in header and "MEM" in header, header
        assert "\u2588" in header and "\u2591" in header, "header should render block gauges"

        selected = app._selected_id
        assert selected == "id_web", selected
        detail_content = app.query_one("#d-name").content
        detail = detail_content.plain if hasattr(detail_content, "plain") else str(detail_content)
        assert detail == "web", detail

        def web_count():
            return sum(1 for c in client._containers if c.name == "/web")

        webs_before = web_count()
        assert webs_before == 1

        await pilot.press("U")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(1.2)
        assert any(c.id == "id_web" and c._removed for c in client._containers), "old web not removed"
        assert web_count() == webs_before + 1, "web not recreated by update-all"
        assert [c.name for c in client.containers.list()] == ["/util", "/web"]

        webs_mid = web_count()
        await pilot.press("u")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(1.2)
        assert [c.name for c in client.containers.list()] == ["/util", "/web"]
        assert web_count() == webs_mid + 1, "web not recreated by single update"

        await pilot.press("d")
        await pilot.pause(0.1)
        await pilot.press("n")
        await pilot.pause(0.2)
        web = next(c for c in client.containers.list() if c.name == "/web")
        assert web.status == "running", "cancelled stop changed container state"

        await pilot.press("d")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.4)
        web = next(c for c in client.containers.list() if c.name == "/web")
        assert web.status == "exited"
        await pilot.press("s")
        await pilot.pause(0.4)
        assert web.status == "running"
        await pilot.press("R")
        await pilot.pause(0.4)
        assert web.status == "running"
        await pilot.press("p")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert web.status == "paused"
        await pilot.press("s")
        await pilot.pause(0.4)
        assert web.status == "running"
        await pilot.press("x")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert web._removed
        await pilot.press("L")
        await pilot.pause(0.4)
        assert not app._log_following

        await pilot.press("q")
        await pilot.pause(0.2)
    print("TEST OK: scan, stats, update-detection, detail panel, 'u', update-all, rescan all pass")


if __name__ == "__main__":
    test_stale_worker_results_are_ignored()
    test_unmount_clears_worker_flags()
    test_update_recovers_after_recreate_failure()
    test_update_failure_policies()
    test_build_run_kwargs_preserves_runtime_settings()
    asyncio.run(test_stats_none_pair_regressions())
    asyncio.run(run_main())
    asyncio.run(test_update_preserves_compose_config())