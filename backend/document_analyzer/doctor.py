"""`uv run document-analyzer-doctor` — proves the container setup works on the configured engine.

Every container in this system (Agent 1's Graphify extraction, Agent 3's pytest sandbox,
both image builds) runs on the engine `CONTAINER_ENGINE` selects: a Colima profile
(default on macOS/Linux) or the ambient Docker engine, e.g. Docker Desktop (default on
Windows) — shared/container_engine.py. This prints which one is active, checks each
prerequisite and prints OK / WARN / FAIL, including a real bind-mount round trip through a
throwaway, network-less container, since "the path isn't shared with the Colima VM" is the
failure that otherwise shows up only as an empty mount mid-job. Exits 1 if anything FAILs. Read-only apart from the probe files it
writes and deletes under the shared work dirs.
"""

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from document_analyzer.agent3.worker.sandbox import SandboxParams  # noqa: E402
from document_analyzer.report.pdf import PdfParams  # noqa: E402
from document_analyzer.shared import container_engine  # noqa: E402
from document_analyzer.shared.container_engine import ContainerEngineUnavailable  # noqa: E402
from document_analyzer.worker.graphify.runner import GraphifyParams  # noqa: E402


class _Report:
    def __init__(self):
        self.failed = False

    def line(self, status: str, label: str, detail: str = "") -> None:
        self.failed |= status == "FAIL"
        print(f"{status:<4}  {label}" + (f" — {detail}" if detail else ""))


def _image_present(image: str) -> bool:
    return container_engine.image_present(image)


def _mount_probe(kind: str, image: str) -> tuple[bool, str]:
    """Writes a file under the shared work dir and reads it back from inside a container."""
    root = container_engine.shared_workdir(kind)
    probe_dir = Path(tempfile.mkdtemp(prefix="doctor-", dir=root))
    try:
        token = uuid.uuid4().hex
        (probe_dir / "probe.txt").write_text(token)
        os.chmod(probe_dir, 0o755)
        os.chmod(probe_dir / "probe.txt", 0o644)
        proc = container_engine.docker(
            [
                "run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--user", "65534:65534",
                "--mount", f"type=bind,src={probe_dir},dst=/probe,readonly",
                "--entrypoint", "cat", image, "/probe/probe.txt",
            ],
            timeout=120,
        )
        if proc.returncode != 0:
            return False, proc.stderr.strip()[-300:] or f"exit {proc.returncode}"
        if proc.stdout.strip() != token:
            return False, f"{probe_dir} mounted empty or stale — is it shared with the engine's VM?"
        return True, f"{root} is visible inside containers"
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def run() -> int:
    report = _Report()
    try:
        kind = container_engine.engine_kind()
    except ContainerEngineUnavailable as exc:
        report.line("FAIL", "CONTAINER_ENGINE", str(exc))
        return 1
    print(f"engine: {container_engine.describe()} (set CONTAINER_ENGINE=colima|docker to switch)\n")

    if kind == "colima":
        profile = container_engine.colima_profile()
        report.line("OK" if shutil.which("colima") else "FAIL", "colima installed", shutil.which("colima") or "brew install colima")
        report.line("OK" if shutil.which("docker") else "FAIL", "docker CLI client installed",
                    shutil.which("docker") or "brew install docker (the client only; Docker Desktop is not needed)")
        try:
            socket = container_engine.resolve_socket(profile)
            report.line("OK", f"Colima profile '{profile}' running on the docker runtime", socket)
            name = container_engine.verify_engine()
            report.line("OK", "engine is Colima", f"docker info reports '{name}'; the ambient docker context is ignored")
        except ContainerEngineUnavailable as exc:
            report.line("FAIL", f"Colima profile '{profile}'", str(exc))
            return 1
    else:
        report.line("OK" if shutil.which("docker") else "FAIL", "docker CLI installed",
                    shutil.which("docker") or "install Docker Desktop (or another Docker engine)")
        try:
            name = container_engine.verify_engine()
            report.line("OK", "docker engine reachable", f"docker info reports '{name}' (the current docker context)")
        except ContainerEngineUnavailable as exc:
            report.line("FAIL", "docker engine", str(exc))
            return 1

    images = {"graphify": GraphifyParams.from_env().image, "agent3": SandboxParams.from_env().image}
    present = {}
    for work, image in images.items():
        present[work] = _image_present(image)
        command = "document-analyzer-build-graphify-image" if work == "graphify" else "document-analyzer-build-agent3-image"
        report.line("OK" if present[work] else "FAIL", f"{work} image {image}",
                    "" if present[work] else f"uv run {command} (images are per engine: build once on each)")

    # The report renderer only ever fails the PDF, never the report (design_report.md §4), so a
    # missing image is a WARN, not a FAIL.
    report_image = PdfParams.from_job_params().image
    present["report"] = _image_present(report_image)
    images["report"] = report_image
    report.line("OK" if present["report"] else "WARN", f"report renderer image {report_image}",
                "" if present["report"] else "uv run document-analyzer-build-report-image (without it reports have no PDF; images are per engine)")

    probe_image = next((images[k] for k in ("agent3", "graphify", "report") if present[k]), None)
    for work in ("graphify", "agent3", "report"):
        if probe_image is None:
            report.line("WARN", f"{work} work dir mount", "skipped: no image to probe with")
            continue
        try:
            ok, detail = _mount_probe(work, probe_image)
        except ContainerEngineUnavailable as exc:
            ok, detail = False, str(exc)
        report.line("OK" if ok else "FAIL", f"{work} work dir is visible inside containers", detail)

    info = container_engine.docker(["info", "--format", "{{json .Runtimes}}"])
    try:
        runtimes = set(json.loads(info.stdout or "{}"))
    except ValueError:
        runtimes = set()
    if "runsc" in runtimes:
        report.line("OK", "gVisor (runsc) registered")
    else:
        report.line("WARN", "gVisor (runsc) not registered with the engine",
                    "fine for development; production needs it (design_agent3.md §7)")

    print("\nall checks passed" if not report.failed else "\nsome checks FAILED")
    return 1 if report.failed else 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
