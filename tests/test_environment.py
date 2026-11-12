"""Tests for the kosa8 Harbor environment against the fake kosa8 Cloud API (no network)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from harbor.environments.base import BaseEnvironment
from harbor.environments.factory import EnvironmentFactory
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from harbor.utils.import_path import import_class

from harbor_kosa8 import Kosa8Environment
from harbor_kosa8._kosa8 import AsyncKosa8Client
from tests.fake_cloud import FakeCloud


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeCloud:
    fake = FakeCloud()
    monkeypatch.setattr(
        Kosa8Environment,
        "_make_client",
        lambda self: AsyncKosa8Client(api_key=fake.api_key, http_client=fake.async_client(), stream=self.options.stream),
    )
    # The fake runs commands on this machine; keep them inside tmp_path.
    monkeypatch.chdir(tmp_path)
    return fake


def make_env(tmp_path: Path, *, docker_image: str | None = "python:3.12-alpine", **kwargs: object) -> Kosa8Environment:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    workdir = tmp_path / "app"
    workdir.mkdir(exist_ok=True)
    envdir = tmp_path / "environment"
    envdir.mkdir(exist_ok=True)
    opts: dict[str, object] = {"workspace": str(ws), "persist_dirs": [], "persist_workdir": False}
    opts.update(kwargs)
    return Kosa8Environment(
        environment_dir=envdir,
        environment_name="hello",
        session_id="hello__abc__env",
        trial_paths=TrialPaths(trial_dir=tmp_path / "trial"),
        task_env_config=EnvironmentConfig(docker_image=docker_image, workdir=str(workdir)),
        **opts,
    )


def test_import_path_loads_the_class() -> None:
    cls = import_class("harbor_kosa8:Kosa8Environment", base=BaseEnvironment, label="environment")
    assert cls is Kosa8Environment
    assert Kosa8Environment.type() == "kosa8"


def test_factory_creates_it_from_the_import_path(tmp_path: Path) -> None:
    (tmp_path / "environment").mkdir()
    env = EnvironmentFactory.create_environment_from_import_path(
        "harbor_kosa8:Kosa8Environment",
        environment_dir=tmp_path / "environment",
        environment_name="hello",
        session_id="hello__abc__env",
        trial_paths=TrialPaths(trial_dir=tmp_path / "trial"),
        task_env_config=EnvironmentConfig(docker_image="python:3.12-alpine"),
        template="node",
    )
    assert isinstance(env, Kosa8Environment)
    assert env.options.template == "node"


def test_options_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_env(tmp_path, ttl_s="not a number")


def test_preflight_needs_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOSA8_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        Kosa8Environment.preflight()
    monkeypatch.setenv("KOSA8_API_KEY", "k8c_live_x")
    Kosa8Environment.preflight({"template": "python"})


def test_requires_an_image_unless_told_otherwise(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="docker_image"):
        make_env(tmp_path, docker_image=None)
    make_env(tmp_path, docker_image=None, use_task_image=False)


async def test_start_exec_stop(cloud: FakeCloud, tmp_path: Path) -> None:
    env = make_env(tmp_path, ttl_s=600)
    await env.start(force_build=False)
    assert cloud.requests[0] == ("POST", "/v1/sandboxes", {"template": "python", "ttl_s": 600})
    r = await env.exec("echo out; echo err >&2; pwd; exit 4", env={"A": "1"})
    assert r.return_code == 4
    assert r.stdout.splitlines()[0] == "out"
    assert Path(r.stdout.splitlines()[1]).resolve() == (tmp_path / "app").resolve()
    assert r.stderr == "err\n"
    execs = [b for (_, p, b) in cloud.requests if p.endswith("/exec")]
    assert all(b.get("image") == "python:3.12-alpine" for b in execs)
    sid = env.sandbox_id
    await env.stop(delete=True)
    assert cloud.sandboxes[sid]["state"] == "deleted"


async def test_template_image_sends_no_image(cloud: FakeCloud, tmp_path: Path) -> None:
    env = make_env(tmp_path, use_task_image=False)
    await env.start(force_build=False)
    await env.exec("true")
    assert all("image" not in b for (_, p, b) in cloud.requests if p.endswith("/exec"))
    await env.stop(delete=True)


async def test_stop_without_delete_keeps_the_sandbox(cloud: FakeCloud, tmp_path: Path) -> None:
    env = make_env(tmp_path)
    await env.start(force_build=False)
    sid = env.sandbox_id
    await env.stop(delete=False)
    assert cloud.sandboxes[sid]["state"] == "running"


async def test_file_and_dir_transfer(cloud: FakeCloud, tmp_path: Path) -> None:
    env = make_env(tmp_path)
    await env.start(force_build=False)
    src = tmp_path / "local.bin"
    data = os.urandom(70_000)
    src.write_bytes(data)
    remote = str(tmp_path / "remote" / "f.bin")
    await env.upload_file(src, remote)
    out = tmp_path / "back" / "f.bin"
    await env.download_file(remote, out)
    assert out.read_bytes() == data

    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.txt").write_text("A")
    (tree / "sub" / "b.txt").write_text("B")
    rdir = str(tmp_path / "remote_tree")
    await env.upload_dir(tree, rdir)
    assert (Path(rdir) / "sub" / "b.txt").read_text() == "B"
    back = tmp_path / "tree_back"
    await env.download_dir(rdir, back)
    assert (back / "a.txt").read_text() == "A"
    assert (back / "sub" / "b.txt").read_text() == "B"
    assert not any((tmp_path / "ws" / ".kosa8-run").iterdir())

    with pytest.raises(FileNotFoundError):
        await env.download_file(str(tmp_path / "nope"), tmp_path / "x")
    await env.stop(delete=True)


async def test_timeout_raises(cloud: FakeCloud, tmp_path: Path) -> None:
    if not (Path("/usr/bin/timeout").exists() or Path("/bin/timeout").exists()):
        pytest.skip("no timeout binary on this machine")
    env = make_env(tmp_path)
    await env.start(force_build=False)
    with pytest.raises(RuntimeError, match="timed out"):
        await env.exec("sleep 5", timeout_sec=1)


async def test_host_request_limit_raises(cloud: FakeCloud, tmp_path: Path) -> None:
    env = make_env(tmp_path)
    await env.start(force_build=False)
    cloud.fail_exec_with = (502, {"error": "host_failed", "message": "exec failed on the host: The operation was aborted due to timeout"})
    with pytest.raises(RuntimeError, match="30 s"):
        await env.exec("sleep 100")


async def test_failed_start_deletes_the_sandbox(cloud: FakeCloud, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path)

    async def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("upload failed")

    monkeypatch.setattr(env, "_upload_environment_dir_after_start", boom)
    with pytest.raises(RuntimeError):
        await env.start(force_build=False)
    assert all(b["state"] == "deleted" for b in cloud.sandboxes.values())


def test_workdir_is_persisted_but_system_dirs_never(tmp_path: Path) -> None:
    env = make_env(tmp_path, persist_dirs=["/tmp", "/usr", "/"])
    s = env._make_session(extra_dirs=("/app", "/etc", "/ws-not"))  # noqa: SLF001
    assert tuple(s.persist_dirs) == ("/tmp", "/app", "/ws-not")


def test_unknown_options_are_rejected_on_newer_harbor(tmp_path: Path) -> None:
    from harbor_kosa8 import environment as m

    if not m._HARBOR_OPTIONS:  # noqa: SLF001
        pytest.skip("Harbor 0.25.0 passes unknown --ek keys through without checking them")
    with pytest.raises(ValueError, match="Unknown option"):
        make_env(tmp_path, nonsense=1)
