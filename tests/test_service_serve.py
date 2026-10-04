"""Tests for ``service.serve.run`` itself (as opposed to ``test_service_cli.py``,
which mocks ``run_serve`` out entirely to test ``cli.py``'s own argv wiring).

Small, synthetic corpus only -- no real ``~/.claude`` data.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from claudeglass.service import serve, storelock
from claudeglass.service.contracts import ServeOptions
from claudeglass.service.storelock import StoreLock

from helpers import turn_line, user_str_line, write_jsonl


def _write_session(root: Path, slug: str, session_id: str, lines: list[dict]) -> Path:
    project_dir = root / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(path, lines)
    # Comfortably outside the watcher's live-file window so this tick
    # treats it as a stable, finished transcript.
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path


def test_once_prints_watcher_stats_line(tmp_path: Path, capsys):
    """``serve --once`` (S1-perf/S1-api release-verification finding):
    before this fix the ``--once`` code path ran the watcher tick and
    exited without printing anything, discarding the exact
    ``WatcherStats`` fields (``discovery_s``/``parse_s``/``store_s``,
    ``errors``, ...) ``docs/api.md`` documents as a first-class
    diagnostic surface. A one-shot/cron/verification run had no way to
    see what the tick did short of opening the store directly.
    """
    root = tmp_path / "projects"
    _write_session(
        root,
        "proj-a",
        "sess-a1",
        [
            turn_line(timestamp="2026-09-18T12:00:00.000Z", input_tokens=100, output_tokens=10),
            turn_line(timestamp="2026-09-18T12:05:00.000Z", input_tokens=120, output_tokens=12),
        ],
    )
    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")

    rc = serve.run(options, once=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "claudeglass serve --once:" in out
    assert "duration_s=" in out
    assert "discovery_s=" in out
    assert "parse_s=" in out
    assert "store_s=" in out
    assert "files_parsed=1" in out
    assert "sessions_upserted=1" in out
    assert "errors=0" in out


def test_once_with_no_sessions_still_prints_a_clean_stats_line(tmp_path: Path, capsys):
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")

    rc = serve.run(options, once=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "files_parsed=0" in out
    assert "errors=0" in out


def test_once_uses_the_store_path_when_given(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    store_path = tmp_path / "elsewhere" / "dev.db"
    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config", store_path=store_path)

    assert serve.run(options, once=True) == 0

    assert store_path.exists()
    assert not (tmp_path / "config" / serve.STORE_FILENAME).exists()


def test_serve_start_refreshes_an_outdated_hook_file(tmp_path: Path):
    """ROB-P7: a hook file this tool itself wrote, now older than what
    this version ships, is refreshed as soon as ``serve`` starts -- not
    just on the next ``capture connect``."""
    import hashlib
    import json
    from importlib import resources

    from claudeglass import capture_catalogue as cat

    root = tmp_path / "projects"
    config_dir = tmp_path / "config"
    hooks_dir = config_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    packaged = (resources.files("claudeglass") / "hooks" / cat.HOOK_SCRIPT).read_bytes()
    old = b"# an older copy this tool wrote\n"
    (hooks_dir / cat.HOOK_SCRIPT).write_bytes(old)
    (hooks_dir / ".manifest.json").write_text(
        json.dumps({cat.HOOK_SCRIPT: hashlib.sha256(old).hexdigest()}), encoding="utf-8"
    )
    options = ServeOptions(projects_root=root, config_dir=config_dir)

    assert serve.run(options, once=True) == 0

    assert (hooks_dir / cat.HOOK_SCRIPT).read_bytes() == packaged


def test_a_store_another_serve_holds_is_refused(tmp_path: Path, capsys):
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    config_dir = tmp_path / "config"
    options = ServeOptions(projects_root=root, config_dir=config_dir)
    held = StoreLock(config_dir / serve.STORE_FILENAME)
    held.acquire({"pid": 4242, "bind": "127.0.0.1", "port": 8765, "once": False})
    try:
        rc = serve.run(options, once=True)
    finally:
        held.release()

    assert rc == 1
    err = capsys.readouterr().err
    assert "process 4242" in err
    assert "http://127.0.0.1:8765" in err
    assert "--store" in err
    # Refused before touching the database.
    assert not (config_dir / serve.STORE_FILENAME).exists()


def test_the_lock_is_released_when_serve_returns(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")

    assert serve.run(options, once=True) == 0
    assert storelock.holder(tmp_path / "config" / serve.STORE_FILENAME) is None
    assert serve.run(options, once=True) == 0


def test_the_dashboard_answers_while_the_first_scan_is_still_running(tmp_path: Path, monkeypatch):
    """The port is bound before the first scan, which runs on the
    watcher's thread; until it finishes, /api/health says so."""
    import http.client
    import json
    import threading

    from claudeglass.service import watcher as watcher_mod

    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", [turn_line(timestamp="2026-09-18T12:00:00.000Z")])
    release_scan = threading.Event()
    real_run_once = watcher_mod.FileWatcher._run_once

    def _slow_first_scan(self, stats):
        release_scan.wait(10)
        return real_run_once(self, stats)

    monkeypatch.setattr(watcher_mod.FileWatcher, "_run_once", _slow_first_scan)

    servers = []

    class _RecordingServer(serve.ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(serve, "ThreadingHTTPServer", _RecordingServer)
    # No real schtasks/systemctl probe from a test.
    from claudeglass import installer

    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: None)

    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config", port=0, poll_interval_s=60)
    result: dict = {}
    thread = threading.Thread(target=lambda: result.setdefault("rc", serve.run(options)), daemon=True)
    thread.start()

    def _health() -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", servers[0].server_port, timeout=10)
        try:
            conn.request("GET", "/api/health")
            return json.loads(conn.getresponse().read())["data"]
        finally:
            conn.close()

    try:
        deadline = time.monotonic() + 10
        while not servers and time.monotonic() < deadline:
            time.sleep(0.01)
        assert servers, "serve never bound its port"
        during = _health()
        assert during["status"] == "starting"
        assert during["scan"]["running"] is True
        lock_info = storelock.holder(tmp_path / "config" / serve.STORE_FILENAME)
        assert lock_info["port"] == servers[0].server_port

        release_scan.set()
        deadline = time.monotonic() + 10
        while _health()["status"] != "ok" and time.monotonic() < deadline:
            time.sleep(0.05)
        after = _health()
        assert after["status"] == "ok"
        assert after["watcher"]["sessions_upserted"] == 1
    finally:
        release_scan.set()
        if servers:
            servers[0].shutdown()
        thread.join(timeout=15)
    assert result.get("rc") == 0
    assert storelock.holder(tmp_path / "config" / serve.STORE_FILENAME) is None


def test_a_port_in_use_is_reported_not_raised(tmp_path: Path, capsys):
    import socket

    root = tmp_path / "projects"
    root.mkdir(parents=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        options = ServeOptions(projects_root=root, config_dir=tmp_path / "config", port=port)
        rc = serve.run(options)
    assert rc == 1
    assert f"port {port}" in capsys.readouterr().err
    assert storelock.holder(tmp_path / "config" / serve.STORE_FILENAME) is None


def test_the_lock_is_dropped_when_its_holder_dies(tmp_path: Path):
    import subprocess
    import sys

    store_path = tmp_path / "service.db"
    holder_script = (
        "import sys, time\n"
        "from claudeglass.service.storelock import StoreLock\n"
        f"StoreLock({str(store_path)!r}).acquire({{'pid': 1}})\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", holder_script], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        assert storelock.holder(store_path) == {"pid": 1}
        with pytest.raises(storelock.StoreLockedError):
            StoreLock(store_path).acquire({"pid": 2})
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # Windows lets go of a dead process's locks shortly after, not at once.
    lock = StoreLock(store_path)
    lock.acquire({"pid": 2}, wait_s=10)
    lock.release()


# -- the package's own code changing on disk ---------------------------------


def _touch_code(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    # A modification time of its own, however coarse the file system's clock.
    stamp = time.time_ns() + 10**9
    os.utime(path, ns=(stamp, stamp))


def _serve_on_a_fake_package(tmp_path: Path, monkeypatch, *, relaunch: bool, **option_kwargs):
    """Start ``serve.run`` on its own thread, watching a small fake
    package under ``tmp_path`` for code changes instead of this one.
    Returns ``(package, server, thread, result, relaunches, health)``."""
    import http.client
    import json
    import threading

    from claudeglass import installer
    from claudeglass.service.codewatch import CodeWatch

    package = tmp_path / "package"
    package.mkdir()
    _touch_code(package / "__init__.py", '__version__ = "1.0.0"\n')
    _touch_code(package / "report.py", "X = 1\n")
    monkeypatch.setattr(serve, "CodeWatch", lambda: CodeWatch(package))
    relaunches: list[int] = []

    def _relaunch(pid):
        relaunches.append(pid)
        return relaunch

    monkeypatch.setattr(serve, "relaunch_after_exit", _relaunch)
    # No real schtasks/systemctl probe from a test.
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: None)
    servers = []

    class _RecordingServer(serve.ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(serve, "ThreadingHTTPServer", _RecordingServer)
    root = tmp_path / "projects"
    root.mkdir()
    options = ServeOptions(
        projects_root=root, config_dir=tmp_path / "config", port=0, poll_interval_s=0.05, **option_kwargs
    )
    result: dict = {}
    thread = threading.Thread(target=lambda: result.setdefault("rc", serve.run(options)), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not servers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert servers, "serve never bound its port"

    def _health() -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", servers[0].server_port, timeout=10)
        try:
            conn.request("GET", "/api/health")
            return json.loads(conn.getresponse().read())["data"]
        finally:
            conn.close()

    return package, servers[0], thread, result, relaunches, _health


def _wait_for(predicate, timeout_s: float = 10) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_exit_on_code_change_exits_so_the_service_starts_again(tmp_path: Path, monkeypatch, capsys):
    package, server, thread, result, relaunches, health = _serve_on_a_fake_package(
        tmp_path, monkeypatch, relaunch=True, exit_on_code_change=True
    )
    try:
        assert _wait_for(lambda: health()["code"] is not None)
        assert health()["code"]["changed"] is False
        _touch_code(package / "__init__.py", '__version__ = "2.0.0"\n')
        thread.join(timeout=15)
        assert not thread.is_alive(), "serve kept running on the old code"
    finally:
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=15)
    assert result.get("rc") == serve.EXIT_CODE_CHANGED
    # Once, for this very process.
    assert relaunches == [os.getpid()]
    assert "code changed on disk" in capsys.readouterr().err
    assert storelock.holder(tmp_path / "config" / serve.STORE_FILENAME) is None


def test_exit_on_code_change_stays_up_when_it_cannot_be_started_again(tmp_path: Path, monkeypatch):
    """No relaunch arranged (on Windows: no ClaudeGlass task to start
    again): exiting would leave no dashboard at all, so it stays up and
    says to restart it -- and doesn't claim it will restart by itself."""
    package, server, thread, result, relaunches, health = _serve_on_a_fake_package(
        tmp_path, monkeypatch, relaunch=False, exit_on_code_change=True
    )
    try:
        assert _wait_for(lambda: health()["code"] is not None)
        _touch_code(package / "report.py", "X = 2\n")
        assert _wait_for(lambda: bool(relaunches))
        time.sleep(0.3)  # several more ticks
        assert thread.is_alive()
        assert relaunches == [os.getpid()]  # tried once, not every tick
        data = health()
        assert data["status"] == "outdated"
        assert data["code"]["changed"] is True
        assert "install-service" in data["message"]
        assert "restarts by itself" not in data["message"]
    finally:
        server.shutdown()
        thread.join(timeout=15)
    assert result.get("rc") == 0


def test_without_exit_on_code_change_a_code_change_is_only_reported(tmp_path: Path, monkeypatch):
    package, server, thread, result, relaunches, health = _serve_on_a_fake_package(
        tmp_path, monkeypatch, relaunch=True
    )
    try:
        assert _wait_for(lambda: health()["code"] is not None)
        _touch_code(package / "__init__.py", '__version__ = "2.0.0"\n')
        assert _wait_for(lambda: health()["status"] == "outdated")
        time.sleep(0.3)
        assert thread.is_alive()
        assert relaunches == []
        data = health()
        assert data["code"]["version_on_disk"] == "2.0.0"
        assert "restarts by itself" not in data["message"]
    finally:
        server.shutdown()
        thread.join(timeout=15)
    assert result.get("rc") == 0


def _recent(hours_ago: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_once_writes_the_typical_piece_of_work_for_a_survey_that_answers_your_messages(tmp_path: Path):
    """The service's daily run builds the report and then your typical piece of
    work from one load of the store (``serve._coaching_typical``): the median
    piece of work of the last 30 days, here 5 sessions of one message and 3
    replies each. A feedback-only setup needs no coaching notes on for it to run."""
    import json

    from claudeglass import coaching

    root = tmp_path / "projects"
    for index in range(5):
        _write_session(
            root,
            "proj-a",
            f"sess-{index}",
            [
                user_str_line("build it", origin={"kind": "human"}, timestamp=_recent(6 - index * 0.1 + 0.05)),
                *[
                    turn_line(
                        timestamp=_recent(6 - index * 0.1 - reply * 0.01), input_tokens=100 * (index + 1), output_tokens=0
                    )
                    for reply in range(3)
                ],
            ],
        )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[capture]\nfeedback = ["feedback_skill"]\n', encoding="utf-8")
    options = ServeOptions(projects_root=root, config_dir=config_dir)

    assert serve.run(options, once=True) == 0

    # 300, 600, 900, 1200 and 1500 tokens: the median is 900. Counts only: nothing else of a session is in the file.
    data = json.loads(coaching.path(config_dir).read_text(encoding="utf-8"))
    assert data["typical_piece_tokens"] == 900
    assert set(data) == {
        "version", "built_at", "days", "split_run", "plan_fresh", "thresholds", "typical_piece_tokens", "muted", "once",
    }
    # No tip answers yet: no hint is muted or shown once.
    assert data["muted"] == [] and data["once"] == []


def test_once_leaves_coaching_json_alone_while_no_survey_item_or_coaching_note_reads_it(tmp_path: Path):
    from claudeglass import coaching

    root = tmp_path / "projects"
    root.mkdir(parents=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[capture]\nfeedback = ["feedback_note"]\n', encoding="utf-8")

    assert serve.run(ServeOptions(projects_root=root, config_dir=config_dir), once=True) == 0

    assert not coaching.path(config_dir).exists()
