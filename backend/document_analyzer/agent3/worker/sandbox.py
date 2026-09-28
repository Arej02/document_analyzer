"""Sandboxed pytest runner — design_agent3.md §7, specs_agent3.md NFR2.

Runs pytest over a workspace folder inside the `galvanise-agent3-runner` image, on the
Colima profile's Docker engine (shared/container_engine.py; never Docker Desktop),
with no network, a read-only root, no capabilities, a non-root user,
memory/CPU/pids limits and a wall-clock timeout, then parses the JUnit XML.

The code under test is generated from untrusted repos, so everything coming
back out of the container (output, JUnit XML) is untrusted too: it's read with
a size cap and the XML is refused if it declares a DOCTYPE or entities.

The sandbox protects the *host*, not the *verdict*. Generated code runs in the
same process as pytest and could fake its own results (design_agent3.md §7,
"Known limitation").
"""

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from document_analyzer.shared import container_engine
from document_analyzer.shared.container_engine import ContainerEngineUnavailable

DEFAULT_RUNNER_IMAGE = "galvanise-agent3-runner:0.1"

# Bytes of pytest output (the tail) and of JUnit XML the container sends back.
OUTPUT_TAIL_BYTES = 64 * 1024
JUNIT_MAX_BYTES = 5_000_000
# Host-side cap on what's kept from the docker CLI's stdout/stderr. Anything past it is
# drained and dropped, so a container writing straight to its stdout can't fill host memory or disk.
HOST_STDOUT_CAP_BYTES = 10 * 1024 * 1024
HOST_STDERR_CAP_BYTES = 64 * 1024
# Docker's own "the container never ran" exit codes (daemon error, command not executable/found).
_DOCKER_FAILURE_EXIT_CODES = {125, 126, 127}
_KILLED_EXIT_CODE = 137  # SIGKILL: OOM killer or a pids/limit kill
_KILL_GRACE_SECONDS = 30
_MESSAGE_MAX_CHARS = 2000

Outcome = Literal["passed", "failed", "error", "skipped"]
Status = Literal["completed", "timed_out", "killed"]


class SandboxUnavailableError(RuntimeError):
    """Colima not running (or not Colima), docker CLI missing, image missing, or gVisor
    required but absent.

    An infrastructure error (the worker fails the job), never a unit verdict.
    """


@dataclass(frozen=True)
class SandboxParams:
    image: str = DEFAULT_RUNNER_IMAGE
    timeout_seconds: int = 120
    memory_mb: int = 1024
    cpus: float = 1
    pids_limit: int = 256
    tmpfs_mb: int = 256
    runtime: str | None = None  # "runsc" for gVisor

    @classmethod
    def from_env(cls) -> "SandboxParams":
        return cls(
            image=os.environ.get("AGENT3_RUNNER_IMAGE") or DEFAULT_RUNNER_IMAGE,
            timeout_seconds=int(os.environ.get("AGENT3_SANDBOX_TIMEOUT_SECONDS", 120)),
            memory_mb=int(os.environ.get("AGENT3_SANDBOX_MEMORY_MB", 1024)),
            runtime=os.environ.get("AGENT3_SANDBOX_RUNTIME") or None,
        )


@dataclass(frozen=True)
class TestCaseResult:
    __test__ = False  # not a pytest test class

    classname: str  # dotted module path, e.g. tests.units.x.test_contract
    name: str
    outcome: Outcome
    message: str | None = None


@dataclass(frozen=True)
class SandboxResult:
    status: Status
    exit_code: int | None
    tests: list[TestCaseResult]
    output_tail: str
    junit_xml: str
    duration_s: float

    @property
    def passed(self) -> int:
        return sum(1 for t in self.tests if t.outcome == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tests if t.outcome in ("failed", "error"))


def require_gvisor_from_env() -> bool:
    return os.environ.get("AGENT3_REQUIRE_GVISOR", "false").strip().lower() in ("1", "true", "yes")


def build_docker_command(workspace: Path, params: SandboxParams, container_name: str, sentinel: str) -> list[str]:
    """The full `docker run` argv. Pure, so every security flag is unit-testable without Docker."""
    src = str(workspace)
    if "," in src:
        # --mount is comma-separated; a comma in the path would inject mount options.
        raise ValueError(f"workspace path must not contain a comma: {src!r}")

    script = (
        "python -m pytest -q -p no:cacheprovider --junitxml=/tmp/junit.xml >/tmp/out.txt 2>&1; c=$?; "
        f"tail -c {OUTPUT_TAIL_BYTES} /tmp/out.txt; echo; echo {sentinel}; "
        f"head -c {JUNIT_MAX_BYTES} /tmp/junit.xml 2>/dev/null; exit $c"
    )
    cmd = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--pull", "never",
        "--log-driver", "none",
        "--network", "none",
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,size={params.tmpfs_mb}m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "--memory", f"{params.memory_mb}m",
        "--memory-swap", f"{params.memory_mb}m",
        "--cpus", str(params.cpus),
        "--pids-limit", str(params.pids_limit),
        "--ulimit", "nofile=1024:1024",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "HOME=/tmp",
    ]
    if params.runtime:
        cmd += ["--runtime", params.runtime]
    cmd += [
        # --mount, not -v: Windows paths contain ':'.
        "--mount", f"type=bind,src={src},dst=/work,readonly",
        "-w", "/work",
        params.image,
        "sh", "-c", script,
    ]
    return cmd


def parse_junit(xml: str) -> list[TestCaseResult]:
    """Parse pytest's JUnit XML. Raises ValueError on oversized, entity-bearing or malformed input."""
    if len(xml) > JUNIT_MAX_BYTES:
        raise ValueError("junit xml too large")
    upper = xml.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValueError("junit xml must not declare a DOCTYPE or entities")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"invalid junit xml: {exc}") from exc

    results: list[TestCaseResult] = []
    for case in root.iter("testcase"):
        outcome: Outcome = "passed"
        message = None
        for tag, candidate in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            child = case.find(tag)
            if child is not None:
                outcome = candidate
                message = (child.get("message") or child.text or "")[:_MESSAGE_MAX_CHARS] or None
                break
        results.append(
            TestCaseResult(
                classname=case.get("classname", ""),
                name=case.get("name", ""),
                outcome=outcome,
                message=message,
            )
        )
    return results


def _engine_env() -> dict[str, str]:
    """The docker CLI's environment, pinned to the Colima profile (shared/container_engine.py)."""
    try:
        return container_engine.docker_env()
    except ContainerEngineUnavailable as exc:
        raise SandboxUnavailableError(str(exc)) from exc


def run_pytest(workspace: Path, params: SandboxParams) -> SandboxResult:
    """Run pytest over `workspace` (mounted read-only at /work) inside the sandbox.

    The workspace is copied into a fresh directory under the Colima-shared work dir
    (container_engine.shared_workdir) and that copy is mounted, since Colima only shares
    $HOME (and /tmp/colima) with its VM — a workspace in the system temp dir would
    otherwise arrive empty. The copy is deleted afterwards whatever happens."""
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")
    env = _engine_env()
    try:
        staging_root = container_engine.shared_workdir("agent3")
    except ContainerEngineUnavailable as exc:
        raise SandboxUnavailableError(str(exc)) from exc
    if staging_root.resolve() == workspace or workspace in staging_root.resolve().parents:
        raise ValueError(f"workspace {workspace} contains the sandbox staging dir {staging_root}")
    staged = Path(tempfile.mkdtemp(prefix="agent3-ws-", dir=staging_root))
    try:
        mounted = staged / "work"
        shutil.copytree(workspace, mounted, symlinks=True)  # never follow a host symlink
        return _run_mounted(mounted, params, env)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _run_mounted(workspace: Path, params: SandboxParams, env: dict[str, str]) -> SandboxResult:
    _make_world_readable(workspace)

    container_name = f"agent3-{uuid.uuid4().hex}"
    sentinel = f"AGENT3-JUNIT-{secrets.token_hex(16)}"
    cmd = build_docker_command(workspace, params, container_name, sentinel)

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
    except OSError as exc:  # FileNotFoundError: no docker binary
        raise SandboxUnavailableError(f"could not run docker: {exc}") from exc

    stdout = _CappedReader(proc.stdout, HOST_STDOUT_CAP_BYTES)
    stderr = _CappedReader(proc.stderr, HOST_STDERR_CAP_BYTES)

    timed_out = False
    try:
        exit_code = proc.wait(timeout=params.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Killing the CLI doesn't stop the container; kill it through the daemon.
        _docker_quiet(["docker", "kill", container_name])
        try:
            exit_code = proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            exit_code = proc.wait()
        _docker_quiet(["docker", "rm", "-f", container_name])
    duration_s = time.monotonic() - start

    out = stdout.result().decode("utf-8", errors="replace")
    err = stderr.result().decode("utf-8", errors="replace")

    marker = f"\n{sentinel}\n"
    if marker in out:
        output_tail, junit_xml = out.split(marker, 1)
    else:
        output_tail, junit_xml = out, ""
        if not timed_out and exit_code in _DOCKER_FAILURE_EXIT_CODES:
            raise SandboxUnavailableError(f"docker run failed (exit {exit_code}): {err.strip()[-2000:]}")

    try:
        tests = parse_junit(junit_xml) if junit_xml.strip() else []
    except ValueError:
        tests = []

    if timed_out:
        status: Status = "timed_out"
    elif exit_code == _KILLED_EXIT_CODE:
        status = "killed"
    else:
        status = "completed"

    return SandboxResult(
        status=status,
        exit_code=exit_code,
        tests=tests,
        output_tail=output_tail[-OUTPUT_TAIL_BYTES:],
        junit_xml=junit_xml,
        duration_s=duration_s,
    )


def docker_runtimes() -> set[str]:
    """Runtimes registered with the Docker daemon (e.g. {"runc", "runsc"})."""
    result = _docker_checked(["docker", "info", "--format", "{{json .Runtimes}}"], "docker daemon is not reachable")
    try:
        return set(json.loads(result or "{}"))
    except json.JSONDecodeError as exc:
        raise SandboxUnavailableError(f"could not read docker runtimes: {exc}") from exc


def check_sandbox_available(params: SandboxParams, require_gvisor: bool = False) -> None:
    """Raise SandboxUnavailableError unless Colima's engine, the runner image and (if needed) gVisor are there."""
    try:
        container_engine.verify_engine()
    except ContainerEngineUnavailable as exc:
        raise SandboxUnavailableError(str(exc)) from exc
    _check_image_present(params.image)
    wanted = "runsc" if require_gvisor else params.runtime
    if wanted and wanted not in docker_runtimes():
        raise SandboxUnavailableError(f"docker runtime {wanted!r} is not registered")
    if require_gvisor and params.runtime != "runsc":
        raise SandboxUnavailableError("AGENT3_REQUIRE_GVISOR is set but AGENT3_SANDBOX_RUNTIME is not 'runsc'")


def qualified_image_name(image: str) -> str:
    """The fully qualified form Docker stores a short name under:
    `galvanise-agent3-runner:0.1` -> `docker.io/library/galvanise-agent3-runner:0.1`.
    Names that already carry a registry (a first component with `.` or `:`, or
    `localhost`) are returned unchanged."""
    first, _, rest = image.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return image
    return f"docker.io/{image}" if rest else f"docker.io/library/{image}"


def _check_image_present(image: str) -> None:
    # Observed on Docker Desktop 29.6.1 with the containerd image store (before the move to
    # Colima; kept because it's harmless on any engine): it intermittently answered
    # "No such image" to `docker image inspect <short name>` for an image that exists
    # (and that `docker run` resolves fine); the fully qualified name always resolves.
    # So a miss on the short name is retried under the qualified one before failing.
    missing = f"runner image {image!r} not found (build it: uv run document-analyzer-build-agent3-image)"
    try:
        _docker_checked(["docker", "image", "inspect", "--format", "{{.Id}}", image], missing)
    except SandboxUnavailableError:
        qualified = qualified_image_name(image)
        if qualified == image:
            raise
        _docker_checked(["docker", "image", "inspect", "--format", "{{.Id}}", qualified], missing)


class _CappedReader:
    """Drains a pipe on a thread, keeping at most `cap` bytes and discarding the rest."""

    def __init__(self, stream: IO[bytes] | None, cap: int):
        self._chunks: list[bytes] = []
        self._size = 0
        self._cap = cap
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self._thread.start()

    def _drain(self, stream: IO[bytes] | None) -> None:
        if stream is None:
            return
        for chunk in iter(lambda: stream.read(65536), b""):
            room = self._cap - self._size
            if room > 0:
                self._chunks.append(chunk[:room])
                self._size += min(len(chunk), room)
        stream.close()

    def result(self) -> bytes:
        self._thread.join(timeout=_KILL_GRACE_SECONDS)
        return b"".join(self._chunks)


def _make_world_readable(workspace: Path) -> None:
    # mkdtemp dirs are 0700; the container runs as uid 65534 and has to read /work.
    if os.name == "nt":
        return
    for root, dirs, files in os.walk(workspace):
        os.chmod(root, 0o755)
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chmod(path, 0o644)


def _docker_quiet(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, capture_output=True, timeout=_KILL_GRACE_SECONDS, env=container_engine.docker_env())
    except (OSError, subprocess.TimeoutExpired, ContainerEngineUnavailable):
        pass


def _docker_checked(cmd: list[str], message: str) -> str:
    env = _engine_env()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_KILL_GRACE_SECONDS, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxUnavailableError(f"{message}: {exc}") from exc
    if result.returncode != 0:
        raise SandboxUnavailableError(f"{message}: {result.stderr.strip()[-2000:]}")
    return result.stdout.strip()
