"""shared/container_engine.py — every container runs on the Colima profile's engine,
never Docker Desktop or whatever the ambient docker context points at."""

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
