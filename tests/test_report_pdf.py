"""report/pdf.py + backend/report_renderer/ — design_report.md §8.3: hardened PDF container.

The first half needs no Docker: the exact `docker run` argv, the failure mapping and the print CSS.
The second half runs the real renderer image and is skipped when the engine or the image is not
there (build it: `uv run document-analyzer-build-report-image`)."""

import io
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from pypdf import PdfReader

from document_analyzer.report import pdf as pdf_module
from document_analyzer.report.pdf import (
    DEFAULT_RENDERER_IMAGE,
    PdfParams,
    PdfRenderError,
    PdfUnavailable,
    build_docker_command,
    check_renderer_available,
    render_pdf,
)
from document_analyzer.report.render import TEMPLATES_DIR, render_report_html
from document_analyzer.shared import container_engine

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "report_project"
HOSTILE = '<script>alert(1)</script><img src=x onerror=alert(2)>" onmouseover="alert(3)'


def _argv(**overrides):
    params = PdfParams(**overrides)
    return build_docker_command(Path("/stage/in"), Path("/stage/out"), params, "report-abc", "rep-1", "2026-10-01"), params


def _pairs(cmd):
    return {cmd[i]: cmd[i + 1] for i in range(len(cmd) - 1) if cmd[i].startswith("-")}


# --- the docker run command (no Docker needed) ---------------------------------------------------------


def test_command_has_every_hardening_flag():
    cmd, params = _argv()
    flags = _pairs(cmd)
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert flags["--network"] == "none"
    assert "--read-only" in cmd
    assert flags["--cap-drop"] == "ALL"
    assert flags["--security-opt"] == "no-new-privileges"
    assert flags["--user"] == "65534:65534"
    assert flags["--pull"] == "never" and flags["--log-driver"] == "none"
    assert flags["--memory"] == flags["--memory-swap"] == "1024m"
    assert flags["--cpus"] == "1.0" and flags["--pids-limit"] == "256"
    assert flags["--tmpfs"] == "/tmp:rw,noexec,nosuid,size=256m"
    assert flags["--ulimit"] == "nofile=1024:1024"
    assert flags["--name"] == "report-abc"
    assert "--privileged" not in cmd and "--network=host" not in cmd and "--pid" not in cmd and "--ipc" not in cmd


def test_command_mounts_exactly_two_directories_input_read_only():
    cmd, _ = _argv()
    mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--mount"]
    assert [m.replace("\\", "/") for m in mounts] == ["type=bind,src=/stage/in,dst=/in,readonly", "type=bind,src=/stage/out,dst=/out"]
    assert "-v" not in cmd and "--volume" not in cmd and "--volumes-from" not in cmd


def test_command_forwards_only_explicit_environment():
    cmd, _ = _argv()
    envs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-e"]
    assert envs == ["HOME=/tmp", "REPORT_ID=rep-1", "REPORT_DATE=2026-10-01"]
    assert "--env-file" not in cmd and "--env" not in cmd


def test_command_runs_only_the_render_script_in_the_pinned_image():
    cmd, params = _argv()
    assert cmd[-2:] == ["python", "/opt/render_pdf.py"]
    assert cmd[-3] == params.image == DEFAULT_RENDERER_IMAGE
    assert "sh" not in cmd and "-c" not in cmd


def test_command_honours_limits_and_gvisor():
    cmd, _ = _argv(memory_mb=2048, cpus=2.0, pids_limit=64, tmpfs_mb=128, runtime="runsc", image="img:9")
    flags = _pairs(cmd)
    assert flags["--memory"] == "2048m" and flags["--cpus"] == "2.0" and flags["--pids-limit"] == "64"
    assert flags["--tmpfs"].endswith("size=128m") and flags["--runtime"] == "runsc" and "img:9" in cmd
    assert "--runtime" not in _argv()[0]


@pytest.mark.parametrize("bad", ["/a,b/in", "/a,readonly=false,src=/etc/in"])
def test_comma_in_a_mount_path_is_refused(bad):
    with pytest.raises(ValueError, match="comma"):
        build_docker_command(Path(bad), Path("/out"), PdfParams(), "n", "rep", "2026-10-01")
    with pytest.raises(ValueError, match="comma"):
        build_docker_command(Path("/in"), Path(bad), PdfParams(), "n", "rep", "2026-10-01")


@pytest.mark.parametrize("value", ["", "x" * 65, "a\nb", "a;b", "<b>", "a=b"])
def test_unsafe_header_values_are_refused(value):
    with pytest.raises(ValueError):
        build_docker_command(Path("/in"), Path("/out"), PdfParams(), "n", value, "2026-10-01")
    with pytest.raises(ValueError):
        build_docker_command(Path("/in"), Path("/out"), PdfParams(), "n", "rep", value)


def test_params_come_from_the_frozen_job_params(monkeypatch):
    monkeypatch.delenv("REPORT_SANDBOX_RUNTIME", raising=False)
    assert PdfParams.from_job_params(None) == PdfParams()
    params = PdfParams.from_job_params({"renderer_image": "r:2", "pdf_timeout_seconds": 30, "pdf_memory_mb": 512})
    assert (params.image, params.timeout_seconds, params.memory_mb, params.runtime) == ("r:2", 30, 512, None)
    monkeypatch.setenv("REPORT_SANDBOX_RUNTIME", " runsc ")
    assert PdfParams.from_job_params({}).runtime == "runsc"  # operational, read at call time


def test_router_freezes_the_same_default_image():
    from document_analyzer.report import router

    assert router.DEFAULT_RENDERER_IMAGE is DEFAULT_RENDERER_IMAGE


# --- source-level invariants of the container side -------------------------------------------------------


def test_renderer_script_disables_js_and_blocks_every_other_request():
    source = (ROOT / "backend" / "report_renderer" / "render_pdf.py").read_text(encoding="utf-8")
    assert "java_script_enabled=False" in source
    assert 'service_workers="block"' in source and "accept_downloads=False" in source
    assert 'context.route("**/*"' in source and "route.abort()" in source
    assert "url == SOURCE_URL or url.startswith(\"data:\")" in source  # the only two things that may load
    assert "html.escape(report_id)" in source and "html.escape(report_date)" in source
    assert "/in/report.html" in source and "/out/report.pdf" in source


def test_dockerfile_pins_the_base_image_and_copies_only_the_script():
    dockerfile = (ROOT / "backend" / "report_renderer" / "Dockerfile").read_text(encoding="utf-8")
    from_line = next(line for line in dockerfile.splitlines() if line.startswith("FROM "))
    assert re.search(r":v\d+\.\d+\.\d+-", from_line) and "latest" not in from_line
    assert [l for l in dockerfile.splitlines() if l.startswith(("COPY", "ADD"))] == ["COPY render_pdf.py /opt/render_pdf.py"]
    tag = re.search(r":v(\d+\.\d+\.\d+)-", from_line).group(1)
    assert f"playwright=={tag}" in dockerfile  # the client is pinned to the base image's version
    instructions = [l.split()[0] for l in dockerfile.splitlines() if l.strip() and not l.startswith("#")]
    assert "ENTRYPOINT" not in instructions and "USER" not in instructions and "CMD" not in instructions  # pdf.py sets them on `docker run`


def test_print_css_has_page_breaks_and_repeating_table_headers():
    css = (TEMPLATES_DIR / "report.css").read_text(encoding="utf-8")
    block = css[css.index("@media print"):]
    for rule in ("break-after: page", "break-inside: avoid", "table-header-group", "print-color-adjust: exact", "position: fixed"):
        assert rule in block


def test_image_build_context_is_exactly_the_renderer_files():
    import tarfile

    from document_analyzer.report.image import build_context

    context, digest = build_context()
    with tarfile.open(fileobj=io.BytesIO(context)) as tar:
        assert sorted(tar.getnames()) == ["Dockerfile", "render_pdf.py"]
    assert len(digest) == 64 and build_context()[1] == digest


# --- failure mapping (fake docker) ------------------------------------------------------------------------


class _FakePopen:
    """Stands in for `docker run`: optionally writes the PDF into the mounted /out directory."""

    def __init__(self, cmd, *, exit_code=0, write=b"%PDF-1.7 fake", hang=False, **_kw):
        self.cmd, self.exit_code, self.hang = cmd, exit_code, hang
        self.stdout, self.stderr = io.BytesIO(b""), io.BytesIO(b"boom detail")
        out = next(a.split("src=")[1].split(",")[0] for a in cmd if a.endswith("dst=/out"))
        self.in_html = Path(next(a.split("src=")[1].split(",")[0] for a in cmd if a.endswith("dst=/in,readonly")), "report.html").read_bytes()
        if write is not None:
            (Path(out) / "report.pdf").write_bytes(write)
        self.killed = False

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            self.killed = True
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return self.exit_code

    def kill(self):
        self.killed = True


@pytest.fixture
def fake_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(container_engine, "docker_env", lambda: {})
    monkeypatch.setattr(container_engine, "shared_workdir", lambda kind: tmp_path)
    calls = {"popen": [], "quiet": []}

    def make(**opts):
        def popen(cmd, **kw):
            proc = _FakePopen(cmd, **opts, **kw)
            calls["popen"].append(proc)
            return proc

        monkeypatch.setattr(pdf_module.subprocess, "Popen", popen)
        monkeypatch.setattr(pdf_module, "_docker_quiet", lambda cmd, env: calls["quiet"].append(cmd))
        return calls

    return make


def test_success_returns_the_pdf_and_cleans_up(fake_docker, tmp_path):
    calls = fake_docker(write=b"%PDF-1.7 hello")
    assert render_pdf("<p>hi é</p>", "rep-1", "2026-10-01", PdfParams(timeout_seconds=5)) == b"%PDF-1.7 hello"
    assert calls["popen"][0].in_html == "<p>hi é</p>".encode("utf-8")
    assert list(tmp_path.iterdir()) == []  # staged input and output are gone


def test_cleanup_also_happens_on_failure(fake_docker, tmp_path):
    fake_docker(exit_code=1, write=None)
    with pytest.raises(PdfRenderError):
        render_pdf("<p/>", "rep-1", "2026-10-01")
    assert list(tmp_path.iterdir()) == []


def test_timeout_kills_the_container_and_fails_the_pdf(fake_docker):
    calls = fake_docker(hang=True, write=None)
    with pytest.raises(PdfRenderError, match="timed out after 3s"):
        render_pdf("<p/>", "rep-1", "2026-10-01", PdfParams(timeout_seconds=3))
    assert any(cmd[:2] == ["docker", "kill"] for cmd in calls["quiet"])
    assert any(cmd[:3] == ["docker", "rm", "-f"] for cmd in calls["quiet"])


@pytest.mark.parametrize("code", [125, 126, 127])
def test_docker_level_failures_are_infrastructure(fake_docker, code):
    fake_docker(exit_code=code, write=None)
    with pytest.raises(PdfUnavailable, match=f"exit {code}"):
        render_pdf("<p/>", "rep-1", "2026-10-01")


def test_renderer_failure_is_a_render_error_with_the_tail_of_stderr(fake_docker):
    fake_docker(exit_code=1, write=None)
    with pytest.raises(PdfRenderError, match="boom detail"):
        render_pdf("<p/>", "rep-1", "2026-10-01")


def test_missing_or_non_pdf_output_is_refused(fake_docker):
    fake_docker(write=None)
    with pytest.raises(PdfRenderError, match="no PDF"):
        render_pdf("<p/>", "rep-1", "2026-10-01")
    fake_docker(write=b"<html>not a pdf</html>")
    with pytest.raises(PdfRenderError, match="not a PDF"):
        render_pdf("<p/>", "rep-1", "2026-10-01")


def test_oversized_output_and_input_are_refused(fake_docker, monkeypatch):
    fake_docker(write=b"%PDF-" + b"0" * 50)
    monkeypatch.setattr(pdf_module, "PDF_MAX_BYTES", 10)
    with pytest.raises(PdfRenderError, match="over the 10 byte limit"):
        render_pdf("<p/>", "rep-1", "2026-10-01")
    monkeypatch.setattr(pdf_module, "HTML_MAX_BYTES", 5)
    with pytest.raises(PdfRenderError, match="report HTML"):
        render_pdf("<p>too long</p>", "rep-1", "2026-10-01")


def test_engine_down_is_infrastructure(monkeypatch):
    def down():
        raise container_engine.ContainerEngineUnavailable("engine down")

    monkeypatch.setattr(container_engine, "docker_env", down)
    with pytest.raises(PdfUnavailable, match="engine down"):
        render_pdf("<p/>", "rep-1", "2026-10-01")


def test_missing_image_is_reported_with_the_build_command(monkeypatch):
    monkeypatch.setattr(container_engine, "verify_engine", lambda: "docker")
    monkeypatch.setattr(container_engine, "image_present", lambda image: False)
    with pytest.raises(PdfUnavailable, match="document-analyzer-build-report-image"):
        check_renderer_available(PdfParams())


# --- the real container ------------------------------------------------------------------------------------


def _renderer_ready() -> bool:
    try:
        container_engine.verify_engine()
        return container_engine.image_present(PdfParams.from_job_params().image)
    except Exception:  # noqa: BLE001
        return False


needs_renderer = pytest.mark.skipif(not _renderer_ready(), reason="renderer image not built (uv run document-analyzer-build-report-image)")


def _pages(pdf: bytes) -> list[str]:
    return [page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages]


@pytest.fixture(scope="module")
def full_html():
    return render_report_html(json.loads((FIXTURE / "full.golden.json").read_text(encoding="utf-8")))


@needs_renderer
def test_real_container_renders_the_fixture_report(full_html):
    pdf = render_pdf(full_html, "rep-fixture-1", "2026-10-01", PdfParams(timeout_seconds=120))
    pages = _pages(pdf)
    assert pdf.startswith(b"%PDF-") and len(pages) >= 2
    text = " ".join(pages)
    assert "Fixture Shop" in text or "Full Assessment" in text
    assert "Executive summary" in text and "Security findings" in text and "Estimate" in text
    # Running header and page numbers on every page, and the cover on its own page.
    assert all("Report rep-fixture-1" in page and "2026-10-01" in page for page in pages)
    assert all(f"Page {i} of {len(pages)}" in page for i, page in enumerate(pages, 1))
    assert "Executive summary" not in pages[0]


class _Listener:
    """A host HTTP server that records every request. The container has no network, so it should
    stay empty; it is the second witness that nothing tried to leave."""

    def __init__(self):
        self.hits: list[str] = []
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("0.0.0.0", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://host.docker.internal:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


@needs_renderer
def test_real_container_does_not_run_scripts_or_fetch_anything():
    listener = _Listener()
    base = listener.base
    # Raw HTML, bypassing the template's escaping: this is the worst case the container must survive.
    hostile = f"""<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="{base}/css"><style>@import url({base}/import); body {{ background: url({base}/bg); }}</style>
<body onload="document.body.innerHTML='PWNED-ONLOAD'">
<p id="p">STATIC-OK</p>
<script>document.getElementById('p').textContent = 'PWNED-JS'; fetch('{base}/fetch'); new Image().src = '{base}/beacon';</script>
<img src="{base}/img" alt="x"><iframe src="{base}/iframe"></iframe><object data="{base}/object"></object>
<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="inline ok">"""
    try:
        pdf = render_pdf(hostile, "rep-hostile", "2026-10-01", PdfParams(timeout_seconds=120))
    finally:
        listener.close()
    text = " ".join(_pages(pdf))
    assert "STATIC-OK" in text  # rendered, so the page really was processed
    assert "PWNED" not in text  # neither the script nor the onload handler ran
    assert listener.hits == []  # and nothing was fetched


@needs_renderer
def test_real_container_blocks_a_meta_refresh_navigation():
    # A navigation away is the one thing a script-free page can still attempt. Chromium follows it, the
    # request gate aborts it, and the render still ends in a PDF with nothing fetched.
    listener = _Listener()
    try:
        pdf = render_pdf(
            f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={listener.base}/refresh"><p>STATIC</p>',
            "rep-refresh", "2026-10-01", PdfParams(timeout_seconds=120),
        )
    finally:
        listener.close()
    assert pdf.startswith(b"%PDF-") and "PWNED" not in " ".join(_pages(pdf))
    assert listener.hits == []


@needs_renderer
def test_real_container_shows_hostile_repository_text_as_text_only():
    report = json.loads((FIXTURE / "full.golden.json").read_text(encoding="utf-8"))
    report["architecture"]["subsystems"][0].update(name=HOSTILE, summary=HOSTILE, summary_grounding="verified")
    report["executive_summary"] = {"text": HOSTILE, "status": "verified", "note": None}
    text = " ".join(_pages(render_pdf(render_report_html(report), "rep-escaped", "2026-10-01")))
    assert "<script>alert(1)</script>" in text  # visible literally: it was escaped, never parsed as markup


@needs_renderer
def test_real_container_timeout_fails_cleanly_and_leaves_no_container(full_html):
    with pytest.raises(PdfRenderError, match="timed out"):
        render_pdf(full_html, "rep-slow", "2026-10-01", PdfParams(timeout_seconds=1))
    listing = container_engine.docker(["ps", "-a", "--filter", "name=report-", "--format", "{{.Names}}"])
    assert listing.stdout.strip() == ""
