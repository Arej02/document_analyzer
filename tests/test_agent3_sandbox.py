"""Agent 3 sandbox — tasks_agent3.md Phase 0, first task; design_agent3.md §7.

Unit tests (no containers) check the `docker run` flags, env config, JUnit parsing and
the timeout/unavailable paths with a fake `Popen` and a stubbed Colima engine. Live
smoke tests run real containers on Colima and prove each restriction holds; they skip
unless the Colima profile is running and the runner image is built:

    colima start --runtime docker
    uv run document-analyzer-build-agent3-image
"""

import io
import re
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from document_analyzer.agent3.worker import sandbox
from document_analyzer.shared import container_engine
from document_analyzer.shared.container_engine import ContainerEngineUnavailable
from document_analyzer.agent3.worker.sandbox import (
    SandboxParams,
    SandboxUnavailableError,
    build_docker_command,
    check_sandbox_available,
    parse_junit,
    run_pytest,
)

from fakes import stub_colima

WORKSPACE = Path("/some/workspace")


@pytest.fixture(autouse=True)
def _colima(request, monkeypatch, tmp_path_factory):
    """Unit tests get a stubbed Colima engine (tests/fakes.py) whose shared dir sits outside
    the test's tmp_path (tests pass tmp_path itself as the workspace); live tests use the
    real one."""
    if request.node.get_closest_marker("live_container") is None:
        stub_colima(monkeypatch, tmp_path_factory.mktemp("colima-shared"))


def _cmd(**overrides) -> list[str]:
    return build_docker_command(WORKSPACE, SandboxParams(**overrides), "agent3-test", "SENTINEL")


def _flag_value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


# ---------------------------------------------------------------- unit: command


def test_command_has_every_security_flag():
    cmd = _cmd(memory_mb=512, cpus=1, pids_limit=128, tmpfs_mb=64)

    assert cmd[:3] == ["docker", "run", "--rm"]
    assert _flag_value(cmd, "--network") == "none"
    assert "--read-only" in cmd
    assert _flag_value(cmd, "--tmpfs") == "/tmp:rw,noexec,nosuid,size=64m"
    assert _flag_value(cmd, "--cap-drop") == "ALL"
    assert _flag_value(cmd, "--security-opt") == "no-new-privileges"
    assert _flag_value(cmd, "--user") == "65534:65534"
    assert _flag_value(cmd, "--memory") == "512m"
    assert _flag_value(cmd, "--memory-swap") == "512m"
    assert _flag_value(cmd, "--cpus") == "1"
    assert _flag_value(cmd, "--pids-limit") == "128"
    assert _flag_value(cmd, "--ulimit") == "nofile=1024:1024"
    assert _flag_value(cmd, "--pull") == "never"
    assert _flag_value(cmd, "--log-driver") == "none"
    assert _flag_value(cmd, "--name") == "agent3-test"


def test_workspace_mount_is_readonly_bind_and_image_follows_it():
    cmd = _cmd(image="img:1")
    mount = _flag_value(cmd, "--mount")

    assert mount == f"type=bind,src={WORKSPACE},dst=/work,readonly"
    assert "-v" not in cmd and "--volume" not in cmd
    assert _flag_value(cmd, "-w") == "/work"
    assert cmd[-4:-2] == ["img:1", "sh"]
    assert "--junitxml=/tmp/junit.xml" in cmd[-1] and "SENTINEL" in cmd[-1]


def test_host_env_is_never_forwarded():
    cmd = _cmd()
    env_values = [cmd[i + 1] for i, a in enumerate(cmd) if a in ("-e", "--env")]

    assert env_values == ["PYTHONDONTWRITEBYTECODE=1", "HOME=/tmp"]
    assert "--env-file" not in cmd


def test_runtime_only_when_set():
    assert "--runtime" not in _cmd()
    assert _flag_value(_cmd(runtime="runsc"), "--runtime") == "runsc"


def test_comma_in_workspace_path_is_rejected():
    with pytest.raises(ValueError, match="comma"):
        build_docker_command(Path("/tmp/a,readonly=false"), SandboxParams(), "n", "s")


def test_from_env_defaults(monkeypatch):
    for var in ("AGENT3_RUNNER_IMAGE", "AGENT3_SANDBOX_TIMEOUT_SECONDS", "AGENT3_SANDBOX_MEMORY_MB", "AGENT3_SANDBOX_RUNTIME"):
        monkeypatch.delenv(var, raising=False)

    assert SandboxParams.from_env() == SandboxParams(
        image="galvanise-agent3-runner:0.1", timeout_seconds=120, memory_mb=1024, runtime=None
    )


def test_from_env_overrides(monkeypatch):
    monkeypatch.setenv("AGENT3_RUNNER_IMAGE", "img:2")
    monkeypatch.setenv("AGENT3_SANDBOX_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("AGENT3_SANDBOX_MEMORY_MB", "256")
    monkeypatch.setenv("AGENT3_SANDBOX_RUNTIME", "runsc")

    params = SandboxParams.from_env()
    assert (params.image, params.timeout_seconds, params.memory_mb, params.runtime) == ("img:2", 30, 256, "runsc")


# ---------------------------------------------------------------- unit: junit

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
  <testcase classname="tests.units.u.test_contract" name="test_ok" time="0.01"/>
  <testcase classname="tests.units.u.test_contract" name="test_bad" time="0.01">
    <failure message="assert 1 == 2">trace</failure></testcase>
  <testcase classname="tests.units.u.test_behaviour" name="test_boom" time="0.01">
    <error message="fixture blew up">trace</error></testcase>
  <testcase classname="tests.units.u.test_behaviour" name="test_skip" time="0.0">
    <skipped message="later" type="pytest.skip">skip</skipped></testcase>
</testsuite></testsuites>"""


def test_parse_junit_outcomes():
    results = parse_junit(JUNIT)

    assert [(r.name, r.outcome, r.message) for r in results] == [
        ("test_ok", "passed", None),
        ("test_bad", "failed", "assert 1 == 2"),
        ("test_boom", "error", "fixture blew up"),
        ("test_skip", "skipped", "later"),
    ]
    assert results[2].classname == "tests.units.u.test_behaviour"


@pytest.mark.parametrize(
    "xml",
    [
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuites>&a;</testsuites>',
        '<!doctype x SYSTEM "file:///etc/passwd"><testsuites/>',
        "<testsuites><unclosed>",
        "x" * (sandbox.JUNIT_MAX_BYTES + 1),
    ],
    ids=["entity", "doctype", "malformed", "oversized"],
)
def test_parse_junit_rejects_hostile_or_bad_xml(xml):
    with pytest.raises(ValueError):
        parse_junit(xml)


# ---------------------------------------------------------------- unit: run_pytest with a fake docker


class _FakePopen:
    """Stands in for the docker CLI. `hang=True` makes the first wait() time out."""

    def __init__(self, stdout: bytes, returncode: int, hang: bool = False, stderr: bytes = b""):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    def wait(self, timeout=None):
        if self._hang:
            self._hang = False
            raise subprocess.TimeoutExpired("docker", timeout)
        return self.returncode

    def kill(self):
        self.killed = True


def _patch_docker(monkeypatch, make_stdout, returncode=0, hang=False, stderr=b""):
    calls = {"popen": None, "run": []}

    def fake_popen(cmd, **kwargs):
        sentinel = re.search(r"echo (AGENT3-JUNIT-\w+)", cmd[-1]).group(1)
        calls["popen"] = cmd
        return _FakePopen(make_stdout(sentinel), returncode, hang=hang, stderr=stderr)

    def fake_run(cmd, **kwargs):
        calls["run"].append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sandbox.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    return calls


def test_run_pytest_parses_output_and_junit(monkeypatch, tmp_path):
    _patch_docker(monkeypatch, lambda s: f"1 failed, 1 passed\n\n{s}\n{JUNIT}".encode(), returncode=1)

    result = run_pytest(tmp_path, SandboxParams())

    assert result.status == "completed" and result.exit_code == 1
    assert result.output_tail.strip() == "1 failed, 1 passed"
    assert result.junit_xml == JUNIT
    assert (result.passed, result.failed) == (1, 2)


def test_run_pytest_timeout_kills_container(monkeypatch, tmp_path):
    calls = _patch_docker(monkeypatch, lambda s: b"partial output", returncode=137, hang=True)

    result = run_pytest(tmp_path, SandboxParams(timeout_seconds=1))

    name = _flag_value(calls["popen"], "--name")
    assert ["docker", "kill", name] in calls["run"]
    assert ["docker", "rm", "-f", name] in calls["run"]
    assert result.status == "timed_out"
    assert result.tests == [] and result.output_tail == "partial output"


def test_run_pytest_exit_137_is_killed(monkeypatch, tmp_path):
    _patch_docker(monkeypatch, lambda s: f"\n{s}\n".encode(), returncode=137)

    assert run_pytest(tmp_path, SandboxParams()).status == "killed"


def test_run_pytest_missing_docker_binary(monkeypatch, tmp_path):
    def no_docker(cmd, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sandbox.subprocess, "Popen", no_docker)

    with pytest.raises(SandboxUnavailableError):
        run_pytest(tmp_path, SandboxParams())


def test_run_pytest_docker_failure_without_sentinel_is_unavailable(monkeypatch, tmp_path):
    _patch_docker(monkeypatch, lambda s: b"", returncode=125, stderr=b"Unable to find image")

    with pytest.raises(SandboxUnavailableError, match="Unable to find image"):
        run_pytest(tmp_path, SandboxParams())


def test_run_pytest_caps_host_output(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "HOST_STDOUT_CAP_BYTES", 1000)
    _patch_docker(monkeypatch, lambda s: b"x" * 50_000)

    result = run_pytest(tmp_path, SandboxParams())

    assert len(result.output_tail) == 1000 and result.tests == []


def test_run_pytest_bad_junit_gives_no_tests(monkeypatch, tmp_path):
    _patch_docker(monkeypatch, lambda s: f"out\n{s}\n<!DOCTYPE x><testsuites/>".encode(), returncode=1)

    assert run_pytest(tmp_path, SandboxParams()).tests == []


def test_check_sandbox_available_requires_runsc(monkeypatch):
    def fake_run(cmd, **kwargs):
        stdout = '{"runc":{}}' if "info" in cmd else "ok"
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    check_sandbox_available(SandboxParams())  # default runtime: fine
    with pytest.raises(SandboxUnavailableError, match="runsc"):
        check_sandbox_available(SandboxParams(runtime="runsc"), require_gvisor=True)


@pytest.mark.parametrize(
    ("image", "qualified"),
    [
        ("galvanise-agent3-runner:0.1", "docker.io/library/galvanise-agent3-runner:0.1"),
        ("avish/runner:1", "docker.io/avish/runner:1"),
        ("ghcr.io/owner/galvanise-agent3-runner@sha256:abc", "ghcr.io/owner/galvanise-agent3-runner@sha256:abc"),
        ("localhost:5000/runner:1", "localhost:5000/runner:1"),
        ("localhost/runner:1", "localhost/runner:1"),
    ],
)
def test_qualified_image_name(image, qualified):
    assert sandbox.qualified_image_name(image) == qualified


def test_check_sandbox_available_retries_a_short_name_under_its_qualified_name(monkeypatch):
    """Docker Desktop's flaky short-name `docker image inspect` (design_agent3.md §7)."""
    inspected = []

    def fake_run(cmd, **kwargs):
        if cmd[1:3] == ["image", "inspect"]:
            inspected.append(cmd[-1])
            if cmd[-1] == "galvanise-agent3-runner:0.1":
                return subprocess.CompletedProcess(cmd, 1, "", "No such image: galvanise-agent3-runner:0.1")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    check_sandbox_available(SandboxParams())

    assert inspected == ["galvanise-agent3-runner:0.1", "docker.io/library/galvanise-agent3-runner:0.1"]


def test_check_sandbox_available_image_really_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1, "", "No such image")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    with pytest.raises(SandboxUnavailableError, match="document-analyzer-build-agent3-image"):
        check_sandbox_available(SandboxParams())


def test_check_sandbox_available_colima_down(monkeypatch):
    def not_running():
        raise ContainerEngineUnavailable("Colima profile 'default' is not running")

    monkeypatch.setattr(container_engine, "verify_engine", not_running)

    with pytest.raises(SandboxUnavailableError, match="not running"):
        check_sandbox_available(SandboxParams())


def test_run_pytest_mounts_a_copy_under_the_colima_shared_dir_and_removes_it(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_x.py").write_text("def test_x():\n    pass\n")
    calls = _patch_docker(monkeypatch, lambda s: f"\n{s}\n".encode())
    seen = {}
    real_popen = sandbox.subprocess.Popen

    def recording_popen(cmd, **kwargs):
        src = _flag_value(cmd, "--mount").split(",")[1].removeprefix("src=")
        seen["src"], seen["files"], seen["env"] = src, sorted(p.name for p in Path(src).iterdir()), kwargs["env"]
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(sandbox.subprocess, "Popen", recording_popen)
    run_pytest(ws, SandboxParams())

    shared = container_engine.shared_workdir("agent3")
    assert Path(seen["src"]).is_relative_to(shared) and seen["files"] == ["test_x.py"]
    assert seen["env"]["DOCKER_HOST"] == "unix:///fake/colima/docker.sock"
    assert list(shared.iterdir()) == []  # staged copy deleted
    assert calls["popen"] is not None


def test_run_pytest_refuses_a_workspace_that_contains_the_staging_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(container_engine, "shared_workdir", lambda kind: tmp_path / "inside")
    with pytest.raises(ValueError, match="contains the sandbox staging dir"):
        run_pytest(tmp_path, SandboxParams())


# ---------------------------------------------------------------- live smoke tests (real Docker)


def _sandbox_ready() -> bool:
    try:
        check_sandbox_available(SandboxParams())
        return True
    except SandboxUnavailableError:
        return False


_LIVE = _sandbox_ready()
_skip_live = pytest.mark.skipif(
    not _LIVE, reason="needs the Colima profile running and the galvanise-agent3-runner image"
)


def live(test):
    return pytest.mark.live_container(_skip_live(test))
LIVE_PARAMS = SandboxParams(timeout_seconds=15, memory_mb=256)


def _workspace(tmp_path: Path, test_source: str) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_sandbox_probe.py").write_text(textwrap.dedent(test_source), encoding="utf-8")
    return ws


def _outcomes(result) -> dict[str, str]:
    return {t.name: t.outcome for t in result.tests}


@live
def test_live_positive_control(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        def test_passes():
            assert 1 + 1 == 2

        def test_fails():
            assert 1 + 1 == 3
        """,
    )
    result = run_pytest(ws, LIVE_PARAMS)

    assert result.status == "completed"
    assert _outcomes(result) == {"test_passes": "passed", "test_fails": "failed"}
    assert (result.passed, result.failed) == (1, 1)
    assert result.tests[0].classname == "test_sandbox_probe"


@live
def test_live_no_network(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        import socket
        import pytest

        def test_no_tcp():
            with pytest.raises(OSError):
                socket.create_connection(("1.1.1.1", 53), 2)

        def test_no_dns():
            with pytest.raises(OSError):
                socket.getaddrinfo("example.com", 80)
        """,
    )
    result = run_pytest(ws, LIVE_PARAMS)

    assert _outcomes(result) == {"test_no_tcp": "passed", "test_no_dns": "passed"}


@live
def test_live_read_only_root(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        import pytest

        def test_workspace_is_read_only():
            with pytest.raises(OSError):
                open("/work/x", "w")

        def test_root_is_read_only():
            with pytest.raises(OSError):
                open("/etc/x", "w")

        def test_tmp_is_writable():
            with open("/tmp/x", "w") as f:
                f.write("ok")
        """,
    )
    result = run_pytest(ws, LIVE_PARAMS)

    assert set(_outcomes(result).values()) == {"passed"} and len(result.tests) == 3
    assert not (ws / "x").exists()


@live
def test_live_non_root_user(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        import os

        def test_nobody():
            assert os.getuid() == 65534 and os.getgid() == 65534
        """,
    )
    assert _outcomes(run_pytest(ws, LIVE_PARAMS)) == {"test_nobody": "passed"}


@live
def test_live_host_env_not_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT3_CANARY_SECRET", "do-not-leak")
    ws = _workspace(
        tmp_path,
        """
        import os

        def test_no_canary():
            assert "AGENT3_CANARY_SECRET" not in os.environ
            assert "SUPABASE_SERVICE_ROLE_KEY" not in os.environ
        """,
    )
    result = run_pytest(ws, LIVE_PARAMS)

    assert _outcomes(result) == {"test_no_canary": "passed"}
    assert "do-not-leak" not in result.output_tail


@live
def test_live_memory_limit(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        def test_allocate_twice_the_limit():
            data = b"\\x01" * (512 * 1024 * 1024)
            assert len(data) > 0
        """,
    )
    result = run_pytest(ws, LIVE_PARAMS)

    assert result.status == "killed" or _outcomes(result).get("test_allocate_twice_the_limit") != "passed"
    assert result.status != "timed_out"


@live
def test_live_pids_limit(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        import threading
        import time
        import pytest

        def test_thread_bomb_is_capped():
            stop = threading.Event()
            threads = []
            with pytest.raises(RuntimeError):
                for _ in range(1000):
                    t = threading.Thread(target=stop.wait)
                    t.start()
                    threads.append(t)
            stop.set()
        """,
    )
    result = run_pytest(ws, LIVE_PARAMS)

    assert _outcomes(result) == {"test_thread_bomb_is_capped": "passed"}


@live
def test_live_timeout_kills_and_removes_container(tmp_path, monkeypatch):
    names = []
    real_build = sandbox.build_docker_command

    def spy(workspace, params, container_name, sentinel):
        names.append(container_name)
        return real_build(workspace, params, container_name, sentinel)

    monkeypatch.setattr(sandbox, "build_docker_command", spy)
    ws = _workspace(
        tmp_path,
        """
        import time

        def test_sleeps_forever():
            time.sleep(600)
        """,
    )
    start = time.monotonic()
    result = run_pytest(ws, SandboxParams(timeout_seconds=5, memory_mb=256))

    assert result.status == "timed_out"
    assert time.monotonic() - start < 30
    left = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={names[0]}", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    assert left.stdout.strip() == ""


def _has_runsc() -> bool:
    try:
        return _LIVE and "runsc" in sandbox.docker_runtimes()
    except SandboxUnavailableError:
        return False


@pytest.mark.skipif(not _has_runsc(), reason="gVisor (runsc) is not registered with Docker")
def test_live_gvisor_runtime(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        def test_runs():
            assert True
        """,
    )
    result = run_pytest(ws, SandboxParams(timeout_seconds=30, memory_mb=256, runtime="runsc"))

    assert _outcomes(result) == {"test_runs": "passed"}
