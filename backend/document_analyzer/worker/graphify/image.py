"""Build the Graphify extraction image from a reviewed, minimal build context.

Ported from Galvanise's runner/image.py, but driven through the `docker` CLI client like
Agent 3's sandbox instead of the Docker SDK, and always into the Colima profile's engine
(shared/container_engine.py), never Docker Desktop. The context holds exactly
the Dockerfile, the hash-locked requirements, and the stdlib-only graphify_worker
package, so credentials, job data and the rest of the backend never reach the image.

Run with `uv run document-analyzer-build-graphify-image`.
"""

import hashlib
import io
import sys
import tarfile
from pathlib import Path

from document_analyzer.shared import container_engine
from document_analyzer.shared.container_engine import ContainerEngineUnavailable
from document_analyzer.worker.graphify.runner import GraphifyParams

_BACKEND = Path(__file__).resolve().parents[3]
RUNNER_DIR = _BACKEND / "graphify_runner"
PACKAGE_DIR = _BACKEND / "document_analyzer" / "graphify_worker"


def context_files() -> dict[str, Path]:
    files = {
        "Dockerfile": RUNNER_DIR / "Dockerfile",
        "requirements.lock": RUNNER_DIR / "requirements.lock",
    }
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        files[f"graphify_worker/{path.name}"] = path
    return files


def build_context() -> tuple[bytes, str]:
    """Deterministic tar of the build context and its content digest."""
    buf = io.BytesIO()
    digest = hashlib.sha256()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for arcname, path in context_files().items():
            data = path.read_bytes()
            digest.update(arcname.encode() + b"\0" + hashlib.sha256(data).digest())
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue(), digest.hexdigest()


def main() -> None:
    image = GraphifyParams.from_env().image
    context, digest = build_context()
    try:
        code = container_engine.build_image(image, context, f"document-analyzer.graphify.context-digest={digest}")
    except ContainerEngineUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
