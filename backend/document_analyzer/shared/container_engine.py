"""The container engine every container in this system runs on, chosen by
`CONTAINER_ENGINE` (design.md §5a "Engine").

Shared by Agent 1 (Graphify extraction, worker/graphify/) and Agent 3 (the pytest
sandbox, agent3/worker/sandbox.py), and by the image builders and the doctor. Ported from
Galvanise's runner/engine.py, driven through the `docker` CLI client instead of the Docker
SDK. Nothing else shells out to `docker` or needs to know which engine is in use.

- `colima` (default on macOS/Linux): a Colima profile's Docker runtime. The ambient CLI
  context is never consulted: every `docker` invocation gets `DOCKER_HOST` set to the
  socket `colima status <profile>` reports (and `DOCKER_CONTEXT` removed), and
  `verify_engine` refuses an engine that doesn't name itself `colima` / `colima-<profile>`.
  So `docker context use desktop-linux` can't silently move containers to Docker Desktop.
  Bind mounts only work for host paths Colima shares with its VM — by default `$HOME` and
  `/tmp/colima` — so anything mounted into a container is staged under `shared_workdir()`
  first; a path elsewhere (e.g. `/var/folders/...` on macOS) would arrive empty.
- `docker` (default on Windows, where Colima doesn't exist): whatever engine the `docker`
  CLI already points at (its current context, `DOCKER_CONTEXT` or `DOCKER_HOST`), e.g.
  Docker Desktop. `verify_engine` only requires it to answer; Docker Desktop shares host
  drives, so there is no shared-path check.

The engine is always the one configured: an unavailable engine is reported as such, never
swapped for the other one. Either way the environment is only the CLI's own: `docker run`
never forwards it into a container, and both sandboxes pass nothing but explicit `--env`
values.
"""

import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

DEFAULT_PROFILE = "default"
ENGINES = ("colima", "docker")
_DEFAULT_SHARED_ROOTS = ("~", "/tmp/colima")


class ContainerEngineUnavailable(RuntimeError):
    """The configured engine can't be used: an unknown `CONTAINER_ENGINE`, the docker CLI
    or Colima missing, the Colima profile not running (or its socket leading elsewhere), or
    the docker engine not answering."""


def engine_kind() -> str:
    """`CONTAINER_ENGINE`, defaulting to `docker` on Windows and `colima` elsewhere."""
    value = (os.environ.get("CONTAINER_ENGINE") or "").strip().lower()
    if not value:
        return "docker" if sys.platform == "win32" else "colima"
    if value not in ENGINES:
        raise ContainerEngineUnavailable(f"CONTAINER_ENGINE={value!r} is not one of {', '.join(ENGINES)}")
    return value


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
    """Environment for the `docker` CLI process: pinned to the Colima profile whatever the
    context, or (docker engine) the ambient environment unchanged."""
    if engine_kind() == "docker":
        return dict(os.environ)
    env = {k: v for k, v in os.environ.items() if k != "DOCKER_CONTEXT"}
    env["DOCKER_HOST"] = resolve_socket(colima_profile())
    return env


def docker(args: list[str], timeout: float = 60, **kwargs) -> subprocess.CompletedProcess:
    """`docker <args>` against the configured engine, text mode, never raising on exit code."""
    if shutil.which("docker") is None:
        hint = "install Docker Desktop" if engine_kind() == "docker" else "brew install docker"
        raise ContainerEngineUnavailable(f"docker CLI client is not installed ({hint})")
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


def _docker_engine_name() -> str:
    """Not cached: with the ambient context, the engine may be started (or switched) later."""
    proc = docker(["info", "--format", "{{.Name}}"], timeout=30)
    if proc.returncode != 0:
        raise ContainerEngineUnavailable(
            f"the docker engine is not reachable (is Docker Desktop running?): {proc.stderr.strip()[-300:]}"
        )
    return proc.stdout.strip() or "docker"


def verify_engine() -> str:
    """Confirms the configured engine answers (and, for Colima, that the socket leads to
    this profile's VM); returns the name `docker info` reports."""
    if engine_kind() == "docker":
        return _docker_engine_name()
    profile = colima_profile()
    return _verified(profile, resolve_socket(profile))


def describe() -> str:
    """Which engine is configured, for logs: `colima (profile 'default')` or `docker`."""
    kind = engine_kind()
    return f"colima (profile '{colima_profile()}')" if kind == "colima" else kind


def qualified_image_name(image: str) -> str:
    """The fully qualified form Docker stores a short name under:
    `galvanise-agent3-runner:0.1` -> `docker.io/library/galvanise-agent3-runner:0.1`.
    Names that already carry a registry (a first component with `.` or `:`, or
    `localhost`) are returned unchanged."""
    first, _, rest = image.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return image
    return f"docker.io/{image}" if rest else f"docker.io/library/{image}"


def image_present(image: str) -> bool:
    """Whether the engine has `image`. Docker Desktop 29.6.1 with the containerd image store
    intermittently answers "No such image" to `docker image inspect <short name>` for an
    image that exists (design_agent3.md §7); the fully qualified name always resolves, so a
    miss on the short name is retried under it. Harmless on Colima."""
    if docker(["image", "inspect", "--format", "{{.Id}}", image]).returncode == 0:
        return True
    qualified = qualified_image_name(image)
    return qualified != image and docker(["image", "inspect", "--format", "{{.Id}}", qualified]).returncode == 0


def shared_roots() -> list[Path]:
    extra = [p for p in (os.environ.get("COLIMA_EXTRA_MOUNTS") or "").split(os.pathsep) if p.strip()]
    return [Path(p).expanduser().resolve() for p in (*_DEFAULT_SHARED_ROOTS, *extra)]


def assert_shared(path: Path) -> Path:
    """Refuses a host path Colima doesn't share with its VM (it would mount empty). The
    docker engine (Docker Desktop) shares host drives, so any path is accepted there."""
    resolved = Path(path).expanduser().resolve()
    if engine_kind() == "docker":
        return resolved
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
    reach the daemon), into the configured engine only. Build output streams to the terminal."""
    engine = verify_engine()
    print(f"building {image} on {engine} ({describe()})", file=sys.stderr)
    cmd = ["docker", "build", "--tag", image, "--label", label, "-"]
    return subprocess.run(cmd, input=context, check=False, env=docker_env()).returncode
