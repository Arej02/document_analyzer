"""PDF rendering of the stored report HTML (design_report.md §8.3).

`render_pdf(html, ...)` runs the report renderer image (`backend/report_renderer/`: headless
Chromium through Playwright) as a throwaway container on the configured engine
(shared/container_engine.py: never `docker` directly). The container has no network, a read-only
root, no capabilities, `no-new-privileges`, a non-root user, memory / CPU / pids limits, a tmpfs
for Chromium's scratch space and a wall-clock timeout. JavaScript is off and Chromium aborts every
request except the single input file (`report_renderer/render_pdf.py`). It mounts exactly two host
directories: the input read-only and a separate output directory, both staged under the engine's
shared work dir, and nothing else.

What comes back out is untrusted too: the PDF is read with a size cap and must start with `%PDF-`,
and the container's stdout/stderr are drained with caps.

Failures are `PdfUnavailable` (engine, image or gVisor missing: infrastructure) or `PdfRenderError`
(the container ran and failed, timed out or produced no usable PDF). The worker turns either into
`pdf_status = failed`; neither can fail a report.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from document_analyzer.shared import container_engine
from document_analyzer.shared.container_engine import ContainerEngineUnavailable

DEFAULT_RENDERER_IMAGE = "galvanise-report-renderer:0.1"

HTML_MAX_BYTES = 20 * 1024 * 1024
PDF_MAX_BYTES = 50 * 1024 * 1024
HOST_STDOUT_CAP_BYTES = 64 * 1024
HOST_STDERR_CAP_BYTES = 64 * 1024
_DOCKER_FAILURE_EXIT_CODES = {125, 126, 127}
_KILL_GRACE_SECONDS = 30
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:/ -]{1,64}$")


class PdfUnavailable(RuntimeError):
    """The engine, the renderer image or a required runtime is missing (infrastructure)."""


class PdfRenderError(RuntimeError):
    """The renderer ran but produced no usable PDF (failure, timeout, kill or a bad file)."""


@dataclass(frozen=True)
class PdfParams:
    image: str = DEFAULT_RENDERER_IMAGE
    timeout_seconds: int = 120
    memory_mb: int = 1024
    cpus: float = 1.0
    pids_limit: int = 256
    tmpfs_mb: int = 256
    runtime: str | None = None  # "runsc" for gVisor (REPORT_SANDBOX_RUNTIME)

    @classmethod
    def from_job_params(cls, params: dict | None = None) -> "PdfParams":
        """The values frozen into `report_generation_jobs.params` (design_report.md §12); the
        runtime is operational, so it is read from the environment at call time."""
        params = params or {}
        return cls(
            image=params.get("renderer_image") or DEFAULT_RENDERER_IMAGE,
            timeout_seconds=int(params.get("pdf_timeout_seconds") or 120),
            memory_mb=int(params.get("pdf_memory_mb") or 1024),
            runtime=(os.environ.get("REPORT_SANDBOX_RUNTIME") or "").strip() or None,
        )


def build_docker_command(in_dir: Path, out_dir: Path, params: PdfParams, container_name: str, report_id: str, report_date: str) -> list[str]:
    """The full `docker run` argv. Pure, so every hardening flag is unit-testable without Docker."""
    for label, path in (("input", in_dir), ("output", out_dir)):
        if "," in str(path):
            raise ValueError(f"{label} path must not contain a comma (it would inject mount options): {path}")
    for label, value in (("report id", report_id), ("report date", report_date)):
        if not _SAFE_VALUE.match(value):
            raise ValueError(f"{label} has characters the header does not allow: {value!r}")
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
        "-e", "HOME=/tmp",
        "-e", f"REPORT_ID={report_id}",
        "-e", f"REPORT_DATE={report_date}",
    ]
    if params.runtime:
        cmd += ["--runtime", params.runtime]
    cmd += [
        # --mount, not -v: Windows paths contain ':'.
        "--mount", f"type=bind,src={in_dir},dst=/in,readonly",
        "--mount", f"type=bind,src={out_dir},dst=/out",
        params.image,
        "python", "/opt/render_pdf.py",
    ]
    return cmd


def check_renderer_available(params: PdfParams | None = None) -> None:
    """Raises `PdfUnavailable` unless the configured engine answers, the image is present and,
    when a runtime is configured, it is registered."""
    params = params or PdfParams.from_job_params()
    try:
        container_engine.verify_engine()
        present = container_engine.image_present(params.image)
    except ContainerEngineUnavailable as exc:
        raise PdfUnavailable(str(exc)) from exc
    if not present:
        raise PdfUnavailable(f"renderer image {params.image!r} not found (build it: uv run document-analyzer-build-report-image)")
    if params.runtime:
        import json

        proc = container_engine.docker(["info", "--format", "{{json .Runtimes}}"])
        try:
            runtimes = set(json.loads(proc.stdout or "{}"))
        except ValueError:
            runtimes = set()
        if params.runtime not in runtimes:
            raise PdfUnavailable(f"docker runtime {params.runtime!r} is not registered")


def render_pdf(html: str, report_id: str, report_date: str, params: PdfParams | None = None) -> bytes:
    """The PDF bytes for one report's HTML. The files are staged under the engine's shared work
    dir (Colima shares only $HOME) and removed afterwards whatever happens."""
    params = params or PdfParams.from_job_params()
    data = html.encode("utf-8")
    if len(data) > HTML_MAX_BYTES:
        raise PdfRenderError(f"report HTML is {len(data)} bytes, over the {HTML_MAX_BYTES} byte limit")
    try:
        env = container_engine.docker_env()
        root = container_engine.shared_workdir("report")
    except ContainerEngineUnavailable as exc:
        raise PdfUnavailable(str(exc)) from exc

    staged = Path(tempfile.mkdtemp(prefix="report-", dir=root))
    try:
        in_dir, out_dir = staged / "in", staged / "out"
        in_dir.mkdir()
        out_dir.mkdir()
        (in_dir / "report.html").write_bytes(data)
        _make_readable(in_dir, out_dir)
        return _run(in_dir, out_dir, params, env, report_id, report_date)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _run(in_dir: Path, out_dir: Path, params: PdfParams, env: dict[str, str], report_id: str, report_date: str) -> bytes:
    container_name = f"report-{uuid.uuid4().hex}"
    cmd = build_docker_command(in_dir, out_dir, params, container_name, report_id, report_date)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    except OSError as exc:  # FileNotFoundError: no docker binary
        raise PdfUnavailable(f"could not run docker: {exc}") from exc
    stdout = _CappedReader(proc.stdout, HOST_STDOUT_CAP_BYTES)
    stderr = _CappedReader(proc.stderr, HOST_STDERR_CAP_BYTES)

    timed_out = False
    started = time.monotonic()
    try:
        exit_code = proc.wait(timeout=params.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _docker_quiet(["docker", "kill", container_name], env)  # killing the CLI does not stop the container
        try:
            exit_code = proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            exit_code = proc.wait()
        _docker_quiet(["docker", "rm", "-f", container_name], env)
    stdout.result()
    err = stderr.result().decode("utf-8", errors="replace").strip()

    if timed_out:
        raise PdfRenderError(f"PDF rendering timed out after {params.timeout_seconds}s")
    if exit_code in _DOCKER_FAILURE_EXIT_CODES:
        raise PdfUnavailable(f"docker run failed (exit {exit_code}): {err[-1000:]}")
    if exit_code != 0:
        raise PdfRenderError(f"the renderer exited with {exit_code} after {time.monotonic() - started:.0f}s: {err[-1000:]}")

    target = out_dir / "report.pdf"
    if not target.is_file() or target.is_symlink():
        raise PdfRenderError("the renderer produced no PDF")
    size = target.stat().st_size
    if size > PDF_MAX_BYTES:
        raise PdfRenderError(f"the PDF is {size} bytes, over the {PDF_MAX_BYTES} byte limit")
    pdf = target.read_bytes()
    if not pdf.startswith(b"%PDF-"):
        raise PdfRenderError("the renderer's output is not a PDF")
    return pdf


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


def _make_readable(in_dir: Path, out_dir: Path) -> None:
    # mkdtemp dirs are 0700; the container runs as uid 65534: it must read /in and write /out.
    if os.name == "nt":
        return
    os.chmod(in_dir.parent, 0o755)
    os.chmod(in_dir, 0o755)
    os.chmod(in_dir / "report.html", 0o644)
    os.chmod(out_dir, 0o777)


def _docker_quiet(cmd: list[str], env: dict[str, str]) -> None:
    try:
        subprocess.run(cmd, capture_output=True, timeout=_KILL_GRACE_SECONDS, env=env)
    except (OSError, subprocess.TimeoutExpired):
        pass
