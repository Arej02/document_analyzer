"""One disposable, hardened Graphify container per job — ported from Galvanise's
runner/container.py, driven through the `docker` CLI client against Colima's engine
(shared/container_engine.py: an explicit Colima socket, never Docker Desktop or the
ambient context), the same way Agent 3's sandbox is (agent3/worker/sandbox.py).

The repo is untrusted input (CLAUDE.md), so the container gets: a copy of only the
selected regular files (symlinks are never followed), read-only source and control
mounts, writable job-scoped output/scratch mounts, no network (AST parsing needs none,
unlike Galvanise's original internet-enabled workers), a read-only root, no
capabilities, no-new-privileges, a non-root user, memory/CPU/pids limits, a wall-clock
timeout, and an explicit environment (never the host's, never a credential). The
container is removed by name whatever happens, and its output is validated
(validate.py) before the caller maps any of it.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from document_analyzer.graphify_worker import protocol as p
from document_analyzer.shared import container_engine
from document_analyzer.shared.container_engine import ContainerEngineUnavailable
from document_analyzer.worker.graphify.validate import Expected, GraphifyOutputError, ValidatedOutput, validate_output

DEFAULT_IMAGE = "document-analyzer-graphify:0.1"
WORKER_USER = "10001:10001"
_DOCKER_FAILURE_EXIT_CODES = {125, 126, 127}
_KILLED_EXIT_CODE = 137


class GraphifyUnavailableError(RuntimeError):
    """Colima not running / not Colima, docker CLI missing, or the image not built."""


class GraphifyRunError(RuntimeError):
    """The container ran but extraction failed (timeout, OOM, worker error)."""


class NoAnalysableFiles(Exception):
    """Graphify accepted none of the requested files (worker exit code 2)."""


@dataclass(frozen=True)
class GraphifyParams:
    image: str = DEFAULT_IMAGE
    timeout_seconds: int = 900
    memory_mb: int = 2048
    cpus: float = 1
    pids_limit: int = 256
    max_output_bytes: int = 512 * 1024 * 1024
    max_file_bytes: int = 2 * 1024 * 1024
    workdir: str | None = None
    runtime: str | None = None  # "runsc" for gVisor, same as AGENT3_SANDBOX_RUNTIME

    @classmethod
    def from_env(cls) -> "GraphifyParams":
        return cls(
            image=os.environ.get("AGENT1_GRAPHIFY_IMAGE") or DEFAULT_IMAGE,
            timeout_seconds=int(os.environ.get("AGENT1_GRAPHIFY_TIMEOUT_SECONDS", 900)),
            memory_mb=int(os.environ.get("AGENT1_GRAPHIFY_MEMORY_MB", 2048)),
            max_file_bytes=int(os.environ.get("AGENT1_GRAPHIFY_MAX_FILE_BYTES", 2 * 1024 * 1024)),
            workdir=os.environ.get("AGENT1_GRAPHIFY_WORKDIR") or None,
            runtime=os.environ.get("AGENT1_GRAPHIFY_RUNTIME") or None,
        )

    def options_hash(self) -> str:
        return p.sha256_hex(p.canonical_dumps({k: v for k, v in asdict(self).items() if k != "workdir"}))


@dataclass(frozen=True)
class JobPaths:
    root: Path

    @property
    def source(self) -> Path:
        return self.root / "source"

    @property
    def control(self) -> Path:
        return self.root / "control"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def scratch(self) -> Path:
        return self.root / "scratch"


def default_workdir() -> Path:
    """Under $HOME by default (container_engine.shared_workdir): Colima only shares the
    home directory with its VM, so a bind mount from the system temp dir (/var/folders on
    macOS) would arrive empty."""
    return container_engine.shared_workdir("graphify")


def build_docker_command(paths: JobPaths, params: GraphifyParams, container_name: str) -> list[str]:
    """The full `docker run` argv. Pure, so every security flag is unit-testable without Docker."""
    for path in (paths.source, paths.control, paths.output, paths.scratch):
        if "," in str(path):
            # --mount is comma-separated; a comma in the path would inject mount options.
            raise ValueError(f"mount path must not contain a comma: {path}")
    cmd = [
        "docker", "run",
        "--name", container_name,
        "--pull", "never",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=268435456",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", WORKER_USER,
        "--memory", f"{params.memory_mb}m",
        "--memory-swap", f"{params.memory_mb}m",
        "--cpus", str(params.cpus),
        "--pids-limit", str(params.pids_limit),
        "--ulimit", "nofile=1024:1024",
        "--env", "HOME=/tmp",
        "--env", "GRAPHIFY_MAX_WORKERS=1",
    ]
    if params.runtime:
        cmd += ["--runtime", params.runtime]
    cmd += [
        "--mount", f"type=bind,src={paths.source},dst={p.SOURCE_MOUNT},readonly",
        "--mount", f"type=bind,src={paths.control},dst={p.CONTROL_MOUNT},readonly",
        "--mount", f"type=bind,src={paths.output},dst={p.OUTPUT_MOUNT}",
        "--mount", f"type=bind,src={paths.scratch},dst={p.SCRATCH_MOUNT}",
        params.image,
        "analyse",
    ]
    return cmd


def _docker(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return container_engine.docker(args, timeout=timeout)
    except ContainerEngineUnavailable as exc:
        raise GraphifyUnavailableError(str(exc)) from exc


def check_available(params: GraphifyParams) -> None:
    try:
        container_engine.verify_engine()
    except ContainerEngineUnavailable as exc:
        raise GraphifyUnavailableError(str(exc)) from exc
    if _docker(["image", "inspect", "--format", "{{.Id}}", params.image]).returncode != 0:
        raise GraphifyUnavailableError(
            f"graphify image {params.image!r} not found (build it: uv run document-analyzer-build-graphify-image)"
        )


@lru_cache(maxsize=4)
def load_capabilities(image: str) -> dict[str, Any]:
    """The manifest the image generated (and smoke-tested) at build time."""
    proc = _docker(
        ["run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
         "--user", WORKER_USER, image, "show-capabilities"],
        timeout=120,
    )
    if proc.returncode != 0:
        raise GraphifyUnavailableError(f"could not read graphify capabilities: {proc.stderr.strip()[-500:]}")
    try:
        manifest = p.strict_loads(proc.stdout)
    except ValueError as exc:
        raise GraphifyUnavailableError(f"graphify capability manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("extensions"), list):
        raise GraphifyUnavailableError("graphify capability manifest has no extension list")
    return manifest


def _copy_selected(repo_path: Path, files: list[str], dest: Path) -> str:
    """Copy regular files only (never following a symlink) and return the source digest."""
    digest = hashlib.sha256()
    for rel in files:
        src = repo_path / rel
        if src.is_symlink() or not src.is_file():
            raise GraphifyRunError(f"selected file is not a regular file: {rel}")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        target.write_bytes(data)
        digest.update(p.id_hash(rel, hashlib.sha256(data).hexdigest()).encode())
    return digest.hexdigest()


def _open_permissions(paths: JobPaths) -> None:
    # The container runs as uid 10001: it has to read source/control and write output/scratch.
    for root, dirs, names in os.walk(paths.root):
        os.chmod(root, 0o755)
        for name in names:
            os.chmod(os.path.join(root, name), 0o644)
    for writable in (paths.output, paths.scratch):
        os.chmod(writable, 0o777)


def run_extraction(
    repo_path: Path, files: list[str], params: GraphifyParams, job_id: str, capability_digest: str = ""
) -> ValidatedOutput:
    """Runs Graphify over `files` (paths relative to `repo_path`) and returns validated output.

    The job directory (copied source, output) is removed before returning, whatever happens."""
    try:
        workdir = container_engine.assert_shared(Path(params.workdir)) if params.workdir else default_workdir()
    except ContainerEngineUnavailable as exc:
        raise GraphifyUnavailableError(str(exc)) from exc
    workdir.mkdir(parents=True, exist_ok=True)
    attempt_id = uuid.uuid4().hex
    root = Path(tempfile.mkdtemp(prefix=f"graphify-{job_id[:12]}-", dir=workdir))
    paths = JobPaths(root)
    container_name = f"agent1-graphify-{attempt_id[:24]}"
    try:
        for d in (paths.source, paths.control, paths.output, paths.scratch):
            d.mkdir()
        source_digest = _copy_selected(repo_path, files, paths.source)
        request = {
            "schema_version": p.REQUEST_SCHEMA_VERSION,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "snapshot_id": job_id,
            "source_digest": source_digest,
            "options_hash": params.options_hash(),
            "capability_digest": capability_digest,
            "files": files,
            "max_output_bytes": params.max_output_bytes,
        }
        (paths.control / p.REQUEST_FILE).write_text(json.dumps(request), encoding="utf-8")
        _open_permissions(paths)

        cmd = build_docker_command(paths, params, container_name)
        try:
            env = container_engine.docker_env()
        except ContainerEngineUnavailable as exc:
            raise GraphifyUnavailableError(str(exc)) from exc
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=params.timeout_seconds, check=False, env=env
            )
        except subprocess.TimeoutExpired as exc:
            raise GraphifyRunError(f"graphify extraction timed out after {params.timeout_seconds}s") from exc
        except OSError as exc:
            raise GraphifyUnavailableError(f"could not run docker: {exc}") from exc

        if proc.returncode == p.EXIT_OK:
            return validate_output(
                paths.output,
                Expected(
                    job_id=job_id,
                    attempt_id=attempt_id,
                    snapshot_id=job_id,
                    source_digest=source_digest,
                    options_hash=params.options_hash(),
                    max_output_bytes=params.max_output_bytes,
                ),
            )
        error = _read_error(paths.output)
        if proc.returncode == p.EXIT_NO_ANALYSABLE_FILES:
            raise NoAnalysableFiles(error or "graphify found no analysable files")
        if proc.returncode in _DOCKER_FAILURE_EXIT_CODES and not error:
            raise GraphifyUnavailableError(f"docker run failed (exit {proc.returncode}): {proc.stderr.strip()[-1000:]}")
        if proc.returncode == _KILLED_EXIT_CODE:
            raise GraphifyRunError("graphify container was killed (memory or pids limit)")
        raise GraphifyRunError(f"graphify worker exited {proc.returncode}: {error or proc.stderr.strip()[-1000:]}")
    finally:
        try:
            _docker(["rm", "-f", container_name])
        except GraphifyUnavailableError:
            pass  # engine gone mid-job; nothing more we can remove from here
        shutil.rmtree(root, ignore_errors=True)


def _read_error(output: Path) -> str | None:
    try:
        raw = (output / p.ERROR_FILE).read_bytes()[:4096]
        body = json.loads(raw)
        return f"{body.get('code')}: {body.get('message')}"
    except (OSError, ValueError, AttributeError):
        return None


__all__ = [
    "GraphifyOutputError",
    "GraphifyParams",
    "GraphifyRunError",
    "GraphifyUnavailableError",
    "NoAnalysableFiles",
    "build_docker_command",
    "check_available",
    "load_capabilities",
    "run_extraction",
]
