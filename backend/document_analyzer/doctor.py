"""`uv run document-analyzer-doctor` — proves the container setup works on Colima.

Every container in this system (Agent 1's Graphify extraction, Agent 3's pytest sandbox,
both image builds) runs on one Colima profile's Docker engine, never Docker Desktop
(shared/container_engine.py). This checks each prerequisite and prints OK / WARN / FAIL,
including a real bind-mount round trip through a throwaway, network-less container, since
"the path isn't shared with the Colima VM" is the failure that otherwise shows up only as
an empty mount mid-job. Exits 1 if anything FAILs. Read-only apart from the probe files it
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
    return container_engine.docker(["image", "inspect", "--format", "{{.Id}}", image]).returncode == 0


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
            return False, f"{probe_dir} mounted empty or stale — is it shared with the Colima VM?"
        return True, f"{root} is visible inside containers"
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def run() -> int:
    report = _Report()
    profile = container_engine.colima_profile()

    report.line("OK" if shutil.which("colima") else "FAIL", "colima installed", shutil.which("colima") or "brew install colima")
    report.line("OK" if shutil.which("docker") else "FAIL", "docker CLI client installed",
                shutil.which("docker") or "brew install docker (the client only; Docker Desktop is not used)")

    try:
        socket = container_engine.resolve_socket(profile)
        report.line("OK", f"Colima profile '{profile}' running on the docker runtime", socket)
        name = container_engine.verify_engine()
        report.line("OK", "engine is Colima", f"docker info reports '{name}'; the ambient docker context is ignored")
    except ContainerEngineUnavailable as exc:
        report.line("FAIL", f"Colima profile '{profile}'", str(exc))
        return 1

    images = {"graphify": GraphifyParams.from_env().image, "agent3": SandboxParams.from_env().image}
    present = {}
    for kind, image in images.items():
        present[kind] = _image_present(image)
        command = "document-analyzer-build-graphify-image" if kind == "graphify" else "document-analyzer-build-agent3-image"
        report.line("OK" if present[kind] else "FAIL", f"{kind} image {image}", "" if present[kind] else f"uv run {command}")

    probe_image = next((images[k] for k in ("agent3", "graphify") if present[k]), None)
    for kind in ("graphify", "agent3"):
        if probe_image is None:
            report.line("WARN", f"{kind} work dir mount", "skipped: no image to probe with")
            continue
        try:
            ok, detail = _mount_probe(kind, probe_image)
        except ContainerEngineUnavailable as exc:
            ok, detail = False, str(exc)
        report.line("OK" if ok else "FAIL", f"{kind} work dir is shared with the Colima VM", detail)

    info = container_engine.docker(["info", "--format", "{{json .Runtimes}}"])
    try:
        runtimes = set(json.loads(info.stdout or "{}"))
    except ValueError:
        runtimes = set()
    if "runsc" in runtimes:
        report.line("OK", "gVisor (runsc) registered")
    else:
        report.line("WARN", "gVisor (runsc) not registered in the Colima VM",
                    "fine for development; production needs it (design_agent3.md §7)")

    print("\nall checks passed" if not report.failed else "\nsome checks FAILED")
    return 1 if report.failed else 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
