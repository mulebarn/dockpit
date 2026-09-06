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
        self.status = "exited"
        return True

    def remove(self):
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
        self.connections.append((container, kwargs))


class FakeNetworks:
    def __init__(self, client):
        self._client = client

    def get(self, name):
        net = next((n for n in self._client._networks if n.name == name), None)
        if net is None:
            raise docker.errors.NotFound(f"network {name} not found")
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
        image = next((i for i in self._client._images if i.ref == ref), None)
        container = FakeContainer(kwargs["name"], ref, image=image, status="running")
        if kwargs.get("labels"):
            container.attrs.setdefault("Config", {})["Labels"] = kwargs["labels"]
        if kwargs.get("working_dir"):
            container.attrs.setdefault("Config", {})["WorkingDir"] = kwargs["working_dir"]
        self._client._last_run_kwargs = kwargs
        self._client._containers.append(container)
        return container


class FakeDockerClient:
    def __init__(self, containers, images):
        self._containers = containers
        self._images = images
        self._networks = []
        self._last_run_kwargs = None
        self.containers = FakeContainers(self)
        self.images = FakeImages(self)
        self.networks = FakeNetworks(self)

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


async def test_stats_none_pair_regressions():
    state = {"client": None}

    def from_env():
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
        assert "MEM –" in header and "CPU 50.6%" in header, header

        snapshot = {web.id: (20.0, 0.4), util.id: (0.0, None)}
        try:
            app._apply_stats(snapshot)
        except TypeError as exc:
            raise AssertionError(f"apply_stats crashed on None pair: {exc}")
        header = app.stats.content if isinstance(app.stats.content, str) else str(app.stats.content)
        assert "CPU 10.0%" in header and "MEM 0.4%" in header, header


async def test_update_preserves_compose_config():
    state = {"client": None}

    def from_env():
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

    def from_env():
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
        assert "2 containers" in header and "1 update" in header and "CPU 20.0%" in header

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
        await pilot.pause(1.2)
        assert any(c.id == "id_web" and c._removed for c in client._containers), "old web not removed"
        assert web_count() == webs_before + 1, "web not recreated by update-all"
        assert [c.name for c in client.containers.list()] == ["/util", "/web"]

        webs_mid = web_count()
        await pilot.press("u")
        await pilot.pause(1.2)
        assert [c.name for c in client.containers.list()] == ["/util", "/web"]
        assert web_count() == webs_mid + 1, "web not recreated by single update"

        await pilot.press("q")
        await pilot.pause(0.2)
    print("TEST OK: scan, stats, update-detection, detail panel, 'u', update-all, rescan all pass")


if __name__ == "__main__":
    asyncio.run(test_stats_none_pair_regressions())
    asyncio.run(run_main())
    asyncio.run(test_update_preserves_compose_config())