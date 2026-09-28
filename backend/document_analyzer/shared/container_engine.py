"""The one container engine this system uses: a Colima profile's Docker runtime.

Shared by Agent 1 (Graphify extraction, worker/graphify/) and Agent 3 (the pytest
sandbox, agent3/worker/sandbox.py), and by the image builders. Ported from Galvanise's
runner/engine.py, driven through the `docker` CLI client instead of the Docker SDK.

Docker Desktop is never used. The ambient CLI context is never consulted either: every
`docker` invocation gets `DOCKER_HOST` set to the socket `colima status <profile>` reports
(and `DOCKER_CONTEXT` removed), and `verify_engine` refuses an engine that doesn't name
itself `colima` / `colima-<profile>`. So `docker context use desktop-linux` on this
machine can't silently move containers to Docker Desktop. That environment is only the
CLI's own: `docker run` never forwards it into a container, and both sandboxes pass
nothing but explicit `--env` values.

Bind mounts only work for host paths Colima shares with its VM — by default `$HOME` and
`/tmp/colima` — so anything mounted into a container is staged under `shared_workdir()`
first; a path elsewhere (e.g. the system temp dir, `/var/folders/...` on macOS) would
arrive in the container empty.
"""

import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

DEFAULT_PROFILE = "default"
_DEFAULT_SHARED_ROOTS = ("~", "/tmp/colima")


class ContainerEngineUnavailable(RuntimeError):
    """Colima or the docker CLI is missing, the profile isn't running, or the socket
    doesn't lead to that Colima profile's engine."""


def colima_profile() -> str:
    return (os.environ.get("COLIMA_PROFILE") or DEFAULT_PROFILE).strip()


def expected_engine_names(profile: str) -> set[str]:
    """What `docker info` reports as Name for a Colima VM (the VM hostname)."""
    return {"colima"} if profile == DEFAULT_PROFILE else {f"colima-{profile}"}


@lru_cache(maxsize=4)
def resolve_socket(profile: str | None = None) -> str:
    """The profile's Docker socket, from `colima status <profile> --json`. Cached per
    process on success only (a failure is retried on the next call)."""
    profile = profile or colima_profile()
    colima = shutil.which("colima")
    if colima is None:
        raise ContainerEngineUnavailable("colima is not installed (brew install colima)")
    try:
        proc = subprocess.run(
            [colima, "status", profile, "--json"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerEngineUnavailable(f"could not run colima status: {exc}") from exc
    if proc.returncode != 0:
        raise ContainerEngineUnavailable(
            f"Colima profile '{profile}' is not running (start it: colima start {profile} --runtime docker)"
        )
    try:
        status = json.loads(proc.stdout)
        socket = status["docker_socket"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ContainerEngineUnavailable(
            f"Colima profile '{profile}' reports no docker socket; it must use the docker runtime "
            f"(colima start {profile} --runtime docker)"
        ) from exc
    if not socket:
        raise ContainerEngineUnavailable(f"Colima profile '{profile}' reports an empty docker socket")
    return socket if "://" in socket else f"unix://{socket}"


def docker_env() -> dict[str, str]:
    """Environment for the `docker` CLI process: pinned to Colima, whatever the context."""
    env = {k: v for k, v in os.environ.items() if k != "DOCKER_CONTEXT"}
    env["DOCKER_HOST"] = resolve_socket(colima_profile())
    return env


def docker(args: list[str], timeout: float = 60, **kwargs) -> subprocess.CompletedProcess:
    """`docker <args>` against the Colima engine, text mode, never raising on exit code."""
    if shutil.which("docker") is None:
        raise ContainerEngineUnavailable("docker CLI client is not installed (brew install docker)")
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=kwargs.pop("text", True),
            timeout=timeout,
            check=False,
            env=docker_env(),
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerEngineUnavailable(f"could not run docker {args[:1]}: {exc}") from exc


@lru_cache(maxsize=4)
def _verified(profile: str, socket: str) -> str:
    proc = docker(["info", "--format", "{{.Name}}"], timeout=30)
    if proc.returncode != 0:
        raise ContainerEngineUnavailable(f"Colima's docker engine at {socket} is not reachable: {proc.stderr.strip()[-300:]}")
    name = proc.stdout.strip()
    if name not in expected_engine_names(profile):
        raise ContainerEngineUnavailable(
            f"engine at {socket} reports '{name}', expected {sorted(expected_engine_names(profile))} "
            "(refusing anything that isn't the Colima profile)"
        )
    return name


def verify_engine() -> str:
    """Confirms the socket leads to this Colima profile's engine; returns its name."""
    profile = colima_profile()
    return _verified(profile, resolve_socket(profile))


def shared_roots() -> list[Path]:
    extra = [p for p in (os.environ.get("COLIMA_EXTRA_MOUNTS") or "").split(os.pathsep) if p.strip()]
    return [Path(p).expanduser().resolve() for p in (*_DEFAULT_SHARED_ROOTS, *extra)]


def assert_shared(path: Path) -> Path:
    """Refuses a host path Colima doesn't share with its VM (it would mount empty)."""
    resolved = Path(path).expanduser().resolve()
    for root in shared_roots():
        if resolved == root or root in resolved.parents:
            return resolved
    raise ContainerEngineUnavailable(
        f"{resolved} is not shared with the Colima VM, so a bind mount of it would be empty. "
        f"Use a directory under {', '.join(str(r) for r in shared_roots())}, or add it to the "
        "profile's mounts (colima start --mount <dir>:w) and to COLIMA_EXTRA_MOUNTS."
    )


def shared_workdir(kind: str) -> Path:
    """Where to stage files a container will mount: CONTAINER_WORKDIR/<kind>, by default
    ~/.cache/document-analyzer/<kind>. Created on demand, and checked to be shared."""
    base = Path(os.environ.get("CONTAINER_WORKDIR") or "~/.cache/document-analyzer").expanduser()
    path = assert_shared(base / kind)
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_image(image: str, context: bytes, label: str) -> int:
    """`docker build` from an in-memory tar context (so only the files the caller chose
    reach the daemon), into the Colima engine only. Build output streams to the terminal."""
    engine = verify_engine()
    print(f"building {image} on {engine} (Colima profile '{colima_profile()}')", file=sys.stderr)
    cmd = ["docker", "build", "--tag", image, "--label", label, "-"]
    return subprocess.run(cmd, input=context, check=False, env=docker_env()).returncode
