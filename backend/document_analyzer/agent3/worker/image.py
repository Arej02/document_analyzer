"""Build Agent 3's runner image (`backend/agent3_runner/`) into the configured engine.

`uv run document-analyzer-build-agent3-image`. Same engine selection as the Graphify image
builder (worker/graphify/image.py, shared/container_engine.py): with Colima the socket comes
from `colima status` and an engine that isn't the profile's is refused; with
CONTAINER_ENGINE=docker it goes to the ambient Docker engine. The context is exactly the Dockerfile and
requirements.txt, sent as a tar on stdin.
"""

import hashlib
import io
import sys
import tarfile
from pathlib import Path

from document_analyzer.agent3.worker.sandbox import SandboxParams
from document_analyzer.shared.container_engine import ContainerEngineUnavailable, build_image

RUNNER_DIR = Path(__file__).resolve().parents[3] / "agent3_runner"


def build_context() -> tuple[bytes, str]:
    buf = io.BytesIO()
    digest = hashlib.sha256()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in sorted(p for p in RUNNER_DIR.iterdir() if p.is_file()):
            data = path.read_bytes()
            digest.update(path.name.encode() + b"\0" + hashlib.sha256(data).digest())
            info = tarfile.TarInfo(path.name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue(), digest.hexdigest()


def main() -> None:
    image = SandboxParams.from_env().image
    context, digest = build_context()
    try:
        code = build_image(image, context, f"document-analyzer.agent3-runner.context-digest={digest}")
    except ContainerEngineUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
