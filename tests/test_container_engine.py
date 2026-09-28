"""shared/container_engine.py — every container runs on the configured engine: the Colima
profile's (never whatever the ambient docker context points at), or, with
CONTAINER_ENGINE=docker, the ambient Docker engine. Never a silent switch between them."""

import json
import subprocess
from pathlib import Path

import pytest

from document_analyzer.shared import container_engine as ce
from document_analyzer.shared.container_engine import ContainerEngineUnavailable


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ce.resolve_socket.cache_clear()
    ce._verified.cache_clear()
    monkeypatch.delenv("COLIMA_PROFILE", raising=False)
    monkeypatch.setenv("CONTAINER_ENGINE", "colima")  # host-OS independent; docker-mode tests override
    monkeypatch.setattr(ce.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}")
    yield
    ce.resolve_socket.cache_clear()
    ce._verified.cache_clear()


def _fake(monkeypatch, *, status=None, status_code=0, engine_name="colima"):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env")))
        if cmd[1] == "status":
            return subprocess.CompletedProcess(cmd, status_code, json.dumps(status or {}), "not running")
        if cmd[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(cmd, 0, engine_name + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ce.subprocess, "run", fake_run)
    return calls


def test_socket_comes_from_colima_status_for_the_profile(monkeypatch):
    monkeypatch.setenv("COLIMA_PROFILE", "work")
    calls = _fake(monkeypatch, status={"docker_socket": "/Users/me/.colima/work/docker.sock"})

    assert ce.resolve_socket(ce.colima_profile()) == "unix:///Users/me/.colima/work/docker.sock"
    assert calls[0][0][1:] == ["status", "work", "--json"]


def test_stopped_profile_is_unavailable_and_not_cached(monkeypatch):
    _fake(monkeypatch, status_code=1)
    with pytest.raises(ContainerEngineUnavailable, match="colima start default"):
        ce.resolve_socket("default")
    _fake(monkeypatch, status={"docker_socket": "/s.sock"})
    assert ce.resolve_socket("default") == "unix:///s.sock"  # started later: picked up


def test_a_profile_without_the_docker_runtime_is_refused(monkeypatch):
    _fake(monkeypatch, status={"runtime": "containerd"})
    with pytest.raises(ContainerEngineUnavailable, match="--runtime docker"):
        ce.resolve_socket("default")


def test_missing_colima_is_unavailable(monkeypatch):
    monkeypatch.setattr(ce.shutil, "which", lambda name: None)
    with pytest.raises(ContainerEngineUnavailable, match="brew install colima"):
        ce.resolve_socket("default")


def test_docker_env_pins_colima_and_drops_the_ambient_context(monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    monkeypatch.setenv("DOCKER_HOST", "unix:///Users/me/.docker/run/docker.sock")
    _fake(monkeypatch, status={"docker_socket": "/Users/me/.colima/default/docker.sock"})

    env = ce.docker_env()

    assert env["DOCKER_HOST"] == "unix:///Users/me/.colima/default/docker.sock"
    assert "DOCKER_CONTEXT" not in env


def test_every_docker_call_carries_the_colima_host(monkeypatch):
    calls = _fake(monkeypatch, status={"docker_socket": "/c.sock"})
    ce.docker(["image", "inspect", "x"])
    [(cmd, env)] = [c for c in calls if c[0][0] == "docker"]
    assert cmd == ["docker", "image", "inspect", "x"] and env["DOCKER_HOST"] == "unix:///c.sock"


@pytest.mark.parametrize(("profile", "name", "ok"), [
    ("default", "colima", True),
    ("work", "colima-work", True),
    ("default", "docker-desktop", False),
    ("work", "colima", False),
])
def test_verify_engine_accepts_only_the_profiles_colima_vm(monkeypatch, profile, name, ok):
    monkeypatch.setenv("COLIMA_PROFILE", profile)
    _fake(monkeypatch, status={"docker_socket": "/c.sock"}, engine_name=name)
    if ok:
        assert ce.verify_engine() == name
    else:
        with pytest.raises(ContainerEngineUnavailable, match="refusing"):
            ce.verify_engine()


def test_assert_shared_accepts_home_and_rejects_the_system_temp_dir(monkeypatch):
    monkeypatch.delenv("COLIMA_EXTRA_MOUNTS", raising=False)
    assert ce.assert_shared(Path.home() / ".cache" / "x") == (Path.home() / ".cache" / "x").resolve()
    assert ce.assert_shared(Path("/tmp/colima/job"))
    with pytest.raises(ContainerEngineUnavailable, match="not shared with the Colima VM"):
        ce.assert_shared(Path("/var/folders/ab/T/tmpjob"))


def test_extra_mounts_extend_the_shared_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("COLIMA_EXTRA_MOUNTS", str(tmp_path))
    assert ce.assert_shared(tmp_path / "job") == (tmp_path / "job").resolve()


def test_shared_workdir_defaults_under_home_and_honours_container_workdir(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTAINER_WORKDIR", raising=False)
    monkeypatch.setattr(Path, "mkdir", lambda self, **kw: None)
    assert ce.shared_workdir("graphify") == (Path.home() / ".cache" / "document-analyzer" / "graphify").resolve()
    monkeypatch.setenv("CONTAINER_WORKDIR", "/var/folders/elsewhere")
    with pytest.raises(ContainerEngineUnavailable):
        ce.shared_workdir("graphify")


# --- engine selection ---------------------------------------------------------------


@pytest.mark.parametrize(("platform", "expected"), [("win32", "docker"), ("darwin", "colima"), ("linux", "colima")])
def test_engine_defaults_to_docker_on_windows_only(monkeypatch, platform, expected):
    monkeypatch.delenv("CONTAINER_ENGINE")
    monkeypatch.setattr(ce.sys, "platform", platform)
    assert ce.engine_kind() == expected


@pytest.mark.parametrize("value", ["docker", " Docker ", "colima"])
def test_engine_setting_is_case_and_space_insensitive(monkeypatch, value):
    monkeypatch.setenv("CONTAINER_ENGINE", value)
    assert ce.engine_kind() == value.strip().lower()


def test_unknown_engine_is_refused(monkeypatch):
    monkeypatch.setenv("CONTAINER_ENGINE", "podman")
    with pytest.raises(ContainerEngineUnavailable, match="colima, docker"):
        ce.engine_kind()


# --- docker engine (e.g. Docker Desktop on Windows) ------------------------------------


def test_docker_engine_uses_the_ambient_environment_and_never_colima(monkeypatch):
    monkeypatch.setenv("CONTAINER_ENGINE", "docker")
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    monkeypatch.setenv("DOCKER_HOST", "npipe:////./pipe/docker_engine")
    monkeypatch.setattr(ce.shutil, "which", lambda name: None if name == "colima" else f"C:/bin/{name}")
    calls = _fake(monkeypatch, engine_name="docker-desktop")

    env = ce.docker_env()
    assert env["DOCKER_CONTEXT"] == "desktop-linux" and env["DOCKER_HOST"] == "npipe:////./pipe/docker_engine"
    assert ce.verify_engine() == "docker-desktop"
    assert not [c for c in calls if c[0][1] == "status"]  # colima never consulted


def test_docker_engine_not_answering_is_unavailable_and_retried(monkeypatch):
    monkeypatch.setenv("CONTAINER_ENGINE", "docker")

    def down(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "Cannot connect to the Docker daemon")

    monkeypatch.setattr(ce.subprocess, "run", down)
    with pytest.raises(ContainerEngineUnavailable, match="Docker Desktop running"):
        ce.verify_engine()
    _fake(monkeypatch, engine_name="docker-desktop")
    assert ce.verify_engine() == "docker-desktop"  # started later: picked up


def test_docker_engine_accepts_any_mount_path(monkeypatch):
    monkeypatch.setenv("CONTAINER_ENGINE", "docker")
    monkeypatch.delenv("COLIMA_EXTRA_MOUNTS", raising=False)
    assert ce.assert_shared(Path("/var/folders/ab/T/tmpjob")) == Path("/var/folders/ab/T/tmpjob").resolve()


def test_describe_names_the_engine(monkeypatch):
    monkeypatch.setenv("COLIMA_PROFILE", "work")
    assert ce.describe() == "colima (profile 'work')"
    monkeypatch.setenv("CONTAINER_ENGINE", "docker")
    assert ce.describe() == "docker"


# --- image lookup ------------------------------------------------------------------------


@pytest.mark.parametrize(("short_ok", "qualified_ok", "expected", "inspected"), [
    (True, False, True, ["runner:1"]),
    (False, True, True, ["runner:1", "docker.io/library/runner:1"]),
    (False, False, False, ["runner:1", "docker.io/library/runner:1"]),
])
def test_image_present_retries_a_short_name_under_its_qualified_name(
    monkeypatch, short_ok, qualified_ok, expected, inspected
):
    monkeypatch.setenv("CONTAINER_ENGINE", "docker")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd[-1])
        ok = short_ok if cmd[-1] == "runner:1" else qualified_ok
        return subprocess.CompletedProcess(cmd, 0 if ok else 1, "", "")

    monkeypatch.setattr(ce.subprocess, "run", fake_run)
    assert ce.image_present("runner:1") is expected
    assert seen == inspected
