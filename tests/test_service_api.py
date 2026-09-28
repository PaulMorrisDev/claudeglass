"""End-to-end tests for ``service.api``'s ``make_handler()`` (S1-api):
spin up a real ``ThreadingHTTPServer`` against a temp SQLite ``Store``
seeded via ``Store``'s own writers (mirroring
``tests/test_service_store.py``'s ``_seed`` convention) and a real
:class:`~claudeglass.corpus.Corpus` built the same way
``tests/test_cli.py``'s ``_write_project``/``corpus.load_corpus`` do,
then exercise every ``/api/*`` route ``docs/api.md`` documents.

``service.rebuild`` (S1-watcher's concurrently-written module) does not
exist in every checkout this suite runs from -- ``service/api.py``'s
own module docstring documents importing it lazily, inside the
function that needs it, for exactly this reason. This file never
imports the real thing: :func:`_install_fake_rebuild` installs a
minimal stand-in ``claudeglass.service.rebuild`` module (both in
``sys.modules`` and as a ``claudeglass.service`` package
attribute, so ``api.py``'s ``from . import rebuild`` resolves it either
way) whose ``corpus_from_store`` simply returns the pre-built
``Corpus`` regardless of its ``days``/``since``/``until``/``window_by``
arguments -- sufficient for exercising every report-backed route and
proving the CLI-JSON byte-parity contract, without depending on
S1-watcher landing first.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from claudeglass import corpus as corpus_mod
from claudeglass.config import ConfigError, load_config, load_session_overrides
from claudeglass.fixes import PROMPT_RESTART
from claudeglass.pricing import load_pricing
from claudeglass.profiles import catalogue as profile_catalogue
from claudeglass.profiles import schema as profile_schema
from claudeglass.render.json_out import render_json
from claudeglass.report import build_report
from claudeglass.service import api as service_api
from claudeglass.service.contracts import CodeState, ServeOptions, WatcherState, WatcherStats
from claudeglass.service.store import Store
from claudeglass.snapshots import Snapshot

from helpers import assert_privacy, turn_line, write_jsonl

#: Distinctive fake local-only strings -- same convention
#: ``tests/test_service_store.py`` uses for its own path-leak guard. If
#: any of these ever surfaces in a response body, a route has forwarded
#: a store-local-only column (``transcripts.path``, ``projects.root_path``
#: or ``profiles.toml_path``) in violation of ``docs/api.md``'s privacy
#: section.
_FAKE_PATH = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a\session-a.jsonl"
_FAKE_SUB_PATH = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a\session-a-agent-1.jsonl"
_FAKE_ROOT = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a"
_FAKE_PROFILE_PATH = r"C:\Users\definitely-not-a-real-person\.claude\claudeglass\profiles\p1.toml"
_LEAK_NEEDLES = (_FAKE_PATH, _FAKE_ROOT, _FAKE_PROFILE_PATH, "definitely-not-a-real-person")


def _build_corpus(tmp_path: Path) -> corpus_mod.Corpus:
    project_dir = tmp_path / "projects" / "proj-a"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "session-a.jsonl",
        [
            turn_line(input_tokens=100 + i, output_tokens=20 + i, cache_read_input_tokens=30)
            for i in range(3)
        ],
    )
    return corpus_mod.load_corpus([project_dir])


def _seed_store(store: Store, corpus: corpus_mod.Corpus) -> str:
    """Seed the store with rows for ``corpus``'s one session, following
    ``test_service_store.py``'s ``_seed`` shape. Returns the session id.
    """
    bundle = corpus.sessions[0]
    snapshot_id = store.upsert_snapshot(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        ts="2026-09-18T12:00:00Z",
        schema_version=1,
        digest_json=json.dumps({"agents": {}}),
    )
    store.upsert_session(
        session_id=bundle.session_id,
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        slug="proj-a",
        first_ts="2026-09-18T12:00:00Z",
        last_ts="2026-09-18T13:00:00Z",
        span_s=3600.0,
        archetype="plan-high-implement-low",
        mode="agentic",
        mode_source="tool-signature",
        purpose="refactor",
        purpose_source="intent-signature",
        entrypoint="cli",
        billing_mode="subscription",
        snapshot_id=snapshot_id,
        profile_id="p1",
        total_cost=1.23,
        total_tokens=450,
    )
    store.upsert_transcript(
        session_id=bundle.session_id,
        path=_FAKE_PATH,
        kind="top-level",
        mtime_ns=123,
        size_bytes=456,
        parser_version=3,
        digest_json=json.dumps({"turns": 3}),
        turns_agg=[
            {
                "day": "2026-09-18",
                "model": "claude-sonnet-5",
                "turns": 3,
                "input_tokens": 300,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 90,
                "output_tokens": 63,
                "thinking_tokens": 0,
                "cc_5m": 0,
                "cc_1h": 0,
                "cost": 1.23,
            }
        ],
        recache_turns=[
            {
                "turn_index": 1,
                "signature": "full-expiry",
                "cache_creation_tokens": 500,
                "preceding_primary": "HUMAN_TEXT",
                "gap_s": 400.0,
            }
        ],
        events=[{"kind": "compact_boundary", "subkind": None, "count": 1, "dropped_tokens_sum": 0, "duration_ms_sum": 0}],
        compactions=[
            {
                "ts": "2026-09-18T12:30:00Z",
                "pre_tokens": 1000,
                "post_tokens": 200,
                "dropped_tokens": 800,
                "trigger": "auto",
                "join_delta_s": 5.0,
            }
        ],
    )
    store.upsert_profile(profile_id="p1", name="implementation-heavy", toml_path=_FAKE_PROFILE_PATH)
    store.record_baseline(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        window_start="2026-09-11T00:00:00Z",
        window_end="2026-09-18T00:00:00Z",
        archetype="plan-high-implement-low",
        digest_json=json.dumps({"sessions": 1, "suggested_profile": "implementation-heavy"}),
    )
    store.set_tag(bundle.session_id, "purpose", "refactor-override")
    return bundle.session_id


def _install_fake_rebuild(monkeypatch, corpus: corpus_mod.Corpus) -> None:
    import claudeglass.service as service_pkg

    fake = types.ModuleType("claudeglass.service.rebuild")

    def corpus_from_store(store, *, days=None, since=None, until=None, window_by="last-reply", project_slugs=None):
        # project_slugs (additive, project-filter work): the one argument
        # this fake does *not* ignore -- api.py's own project filter
        # (_project_query/_build_report_model) is what a test in this
        # file exercises, and it can only see an effect if this stand-in
        # actually narrows `corpus` the same way the real
        # service.rebuild.corpus_from_store does.
        if project_slugs is None:
            return corpus
        allowed = set(project_slugs)
        return corpus_mod.Corpus(
            sessions=[b for b in corpus.sessions if b.slug in allowed],
            total_files=corpus.total_files,
            total_bytes=corpus.total_bytes,
            cache_hits=corpus.cache_hits,
            cache_misses=corpus.cache_misses,
            elapsed_s=corpus.elapsed_s,
        )

    fake.corpus_from_store = corpus_from_store
    # Both forms so `from . import rebuild` resolves it regardless of
    # whether Python's import machinery checks the package attribute or
    # sys.modules first -- see this module's own docstring.
    monkeypatch.setitem(sys.modules, "claudeglass.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)


class _ServerHandle:
    """Thin wrapper around a running ``ThreadingHTTPServer`` plus the
    fixtures behind it, for tests to issue requests against."""

    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread, *, corpus, store, options):
        self.server = server
        self.thread = thread
        self.corpus = corpus
        self.store = store
        self.options = options

    @property
    def port(self) -> int:
        return self.server.server_port

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ):
        """``headers`` overrides/extends the default ``Content-Type``
        this method sends whenever ``body`` is given -- used by the
        review-S3 same-origin tests to send ``Origin``/``Sec-Fetch-Site``
        or a deliberately wrong ``Content-Type``. ``raw_body``, when
        given, is sent verbatim instead of JSON-encoding ``body`` (also
        S3: a non-JSON payload with a spoofed ``Content-Type``)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            sent_headers: dict[str, str] = {}
            payload = raw_body
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                sent_headers["Content-Type"] = "application/json"
            if headers:
                sent_headers.update(headers)
            conn.request(method, path, body=payload, headers=sent_headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp, raw
        finally:
            conn.close()

    def get_json(self, path: str):
        resp, raw = self.request("GET", path)
        return resp, json.loads(raw)

    def post_json(self, path: str, body: dict):
        resp, raw = self.request("POST", path, body=body)
        return resp, json.loads(raw)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _start_server(tmp_path, monkeypatch, *, corpus=None, **handler_kwargs) -> _ServerHandle:
    """Shared setup behind the ``server`` fixture below -- factored out
    so a test that needs a non-default ``make_handler`` keyword (e.g.
    v3's ``service_registered``) can build its own handle without
    duplicating this whole sequence. ``corpus`` defaults to
    ``_build_corpus`` (3 turns, below recommend()'s minimum sample) --
    pass a bigger one (see ``test_recommend_contract.py``'s pattern) for
    a test that needs a recommendation to actually fire.
    """
    corpus = corpus if corpus is not None else _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()
    session_id = _seed_store(store, corpus)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)

    handler_cls = service_api.make_handler(store, options, **handler_kwargs)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    handle.session_id = session_id
    return handle


@pytest.fixture
def server(tmp_path, monkeypatch):
    handle = _start_server(tmp_path, monkeypatch)
    try:
        yield handle
    finally:
        handle.close()
        handle.store.close()


def _assert_no_leak(raw: bytes) -> None:
    text = raw.decode("utf-8")
    for needle in _LEAK_NEEDLES:
        assert needle not in text, f"{needle!r} leaked into response: {text[:500]}"


# -- envelope / headers ---------------------------------------------------


def test_every_response_has_security_headers_and_content_type(server):
    resp, _raw = server.request("GET", "/api/health")
    assert resp.getheader("Cache-Control") == "no-store"
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert resp.getheader("Content-Security-Policy")
    assert resp.getheader("Content-Type") == "application/json"


def test_unknown_route_is_404_not_found(server):
    resp, body = server.get_json("/api/does-not-exist")
    assert resp.status == 404
    assert body == {"ok": False, "error": {"code": "not_found", "message": body["error"]["message"]}}
    assert_privacy(body)


# -- finding 9: HEAD/PUT/DELETE/PATCH/OPTIONS --------------------------------


def test_head_health_matches_get_headers_with_no_body(server):
    resp, raw = server.request("HEAD", "/api/health")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/json"
    assert resp.getheader("Cache-Control") == "no-store"
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert raw == b""


def test_head_report_json_returns_the_unwrapped_routes_headers_with_no_body(server):
    # report.json is the one route with its own content type/envelope
    # rules (finding 1) -- confirm HEAD threads head_only through that
    # path too, not just the generic envelope one above.
    resp, raw = server.request("HEAD", "/api/report.json")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/json"
    assert raw == b""


def test_head_static_index_returns_no_body(server):
    resp, raw = server.request("HEAD", "/")
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/html")
    assert raw == b""


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS"])
def test_unsupported_methods_return_405_with_the_usual_envelope(server, method):
    resp, raw = server.request(method, "/api/health")
    assert resp.status == 405
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    body = json.loads(raw)
    assert body == {
        "ok": False,
        "error": {"code": "method_not_allowed", "message": f"{method} is not supported on this route"},
    }
    assert_privacy(body)


# -- store-backed routes ----------------------------------------------------


def test_health(server):
    resp, body = server.get_json("/api/health")
    assert resp.status == 200
    assert body["ok"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["schema_version"] >= 1
    assert "watcher" in body["data"]
    # v3: no `service_registered` probe was wired up (the `server`
    # fixture calls make_handler with no extra kwargs), so this must be
    # null ("unknown"), never folded into false.
    assert body["data"]["service_registered"] is None
    # No watcher_state wired up: nothing to judge the scanner by.
    assert body["data"]["message"] is None
    assert body["data"]["scan"] is None
    # No code_watch wired up: whether the code changed is unknown.
    assert body["data"]["code"] is None
    assert_privacy(body)
    _assert_no_leak(json.dumps(body).encode("utf-8"))


def test_health_reports_a_first_scan_in_progress(tmp_path, monkeypatch):
    state = WatcherState(running=True, scanning=True, scan_started_at="2026-09-23T10:00:00Z", phase="storing", done=12, total=340)
    handle = _start_server(tmp_path, monkeypatch, watcher_stats=lambda: None, watcher_state=lambda: state)
    try:
        resp, body = handle.get_json("/api/health")
        assert resp.status == 200
        data = body["data"]
        assert data["status"] == "starting"
        assert "12 of 340 sessions" in data["message"]
        assert data["scan"]["phase"] == "storing"
        assert (data["scan"]["done"], data["scan"]["total"]) == (12, 340)
        assert_privacy(body)
    finally:
        handle.close()
        handle.store.close()


def test_health_reports_a_failed_scan_as_degraded_with_its_reason(tmp_path, monkeypatch):
    stats = WatcherStats(errors=1, error_messages=("tick failed: OperationalError: database is locked",))
    state = WatcherState(running=True, last_success_at="2026-09-23T10:00:00Z", last_tick_failed=True)
    handle = _start_server(tmp_path, monkeypatch, watcher_stats=lambda: stats, watcher_state=lambda: state)
    try:
        _resp, body = handle.get_json("/api/health")
        assert body["data"]["status"] == "degraded"
        assert "database is locked" in body["data"]["message"]
        assert "10:00 UTC" in body["data"]["message"]
    finally:
        handle.close()
        handle.store.close()


_NOW = service_api.datetime(2026, 9, 23, 12, 0, tzinfo=service_api.timezone.utc)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (WatcherState(running=True, last_success_at="2026-09-23T11:59:30Z"), "ok"),
        (WatcherState(running=True), "starting"),
        (WatcherState(running=True, scanning=True, phase="reading", done=1, total=9), "starting"),
        (WatcherState(running=True, last_tick_failed=True), "degraded"),
        (WatcherState(running=True, last_success_at="2026-09-23T11:00:00Z", last_tick_failed=True), "degraded"),
        # The watcher thread died: nothing will ever update again.
        (WatcherState(running=False, last_success_at="2026-09-23T11:59:30Z"), "stale"),
        (WatcherState(running=False), "stale"),
        # Alive but has not finished a scan for over ten minutes.
        (WatcherState(running=True, last_success_at="2026-09-23T11:40:00Z"), "stale"),
        # A long scan in progress is not stale ...
        (
            WatcherState(
                running=True, scanning=True, scan_started_at="2026-09-23T11:30:00Z", last_success_at="2026-09-23T11:29:00Z"
            ),
            "ok",
        ),
        # ... until it has run for over an hour.
        (
            WatcherState(
                running=True, scanning=True, scan_started_at="2026-09-23T10:30:00Z", last_success_at="2026-09-23T10:29:00Z"
            ),
            "stale",
        ),
    ],
)
def test_health_status_table(state, expected):
    status, message = service_api._health_status(None, state, poll_interval_s=30.0, now=_NOW)
    assert status == expected
    assert (message is None) == (expected == "ok")


_CHANGED = CodeState(changed=True, changed_at="2026-09-23T11:58:00Z", version_on_disk="9.9.9", id="0123456789ab")


@pytest.mark.parametrize(
    "state",
    [
        None,
        WatcherState(running=True, last_success_at="2026-09-23T11:59:30Z"),
        WatcherState(running=True, scanning=True, phase="reading", done=1, total=9),
        WatcherState(running=False),
    ],
)
def test_health_outdated_comes_before_every_other_status(state):
    """Only a restart helps once the code changed on disk, so that is
    what health says, whatever the scanner is doing."""
    status, message = service_api._health_status(None, state, poll_interval_s=30.0, now=_NOW, code=_CHANGED)
    assert status == "outdated"
    assert "changed on disk at 11:58 UTC" in message
    assert f"({service_api._TOOL_VERSION} is running, 9.9.9 is on disk)" in message
    assert "claudeglass install-service" in message


def test_health_outdated_says_it_restarts_by_itself_with_exit_on_code_change():
    same_version = CodeState(changed=True, changed_at="2026-09-23T11:58:00Z", version_on_disk=service_api._TOOL_VERSION)
    _status, message = service_api._health_status(
        None, None, poll_interval_s=30.0, now=_NOW, code=same_version, restarts_itself=True
    )
    assert "restarts by itself" in message
    assert "claudeglass install-service" in message
    assert "is on disk" not in message  # no version to tell apart


def test_health_unchanged_code_leaves_the_status_alone():
    state = WatcherState(running=True, last_success_at="2026-09-23T11:59:30Z")
    unchanged = CodeState(version_on_disk=service_api._TOOL_VERSION, id="0123456789ab")
    assert service_api._health_status(None, state, poll_interval_s=30.0, now=_NOW, code=unchanged) == ("ok", None)


class _FakeCodeWatch:
    """``codewatch.CodeWatch``'s surface for ``make_handler``, counting checks."""

    def __init__(self, state: CodeState):
        self.current = state
        self.checks = 0

    def state(self) -> CodeState:
        return self.current

    def check(self) -> CodeState:
        self.checks += 1
        return self.current


def test_health_reports_the_code_block_and_outdated_status(tmp_path, monkeypatch):
    handle = _start_server(tmp_path, monkeypatch, code_watch=_FakeCodeWatch(_CHANGED))
    try:
        resp, body = handle.get_json("/api/health")
        assert resp.status == 200
        data = body["data"]
        assert data["status"] == "outdated"
        assert data["code"] == {
            "changed": True,
            "changed_at": "2026-09-23T11:58:00Z",
            "version_on_disk": "9.9.9",
            "id": "0123456789ab",
        }
        assert "install-service" in data["message"]
        assert_privacy(body)
    finally:
        handle.close()
        handle.store.close()


def test_health_still_answers_when_the_capture_block_cannot_be_worked_out(server, monkeypatch):
    """/api/health is how a code change on disk is reported, so a part of
    it that fails (here, as a changed module might) must not take the
    whole route down."""

    def _broken(*_args, **_kwargs):
        raise AttributeError("module has no attribute 'config_block'")

    monkeypatch.setattr(service_api, "load_config", _broken)
    resp, body = server.get_json("/api/health")
    assert resp.status == 200
    assert body["data"]["capture"] is None


def test_health_stale_threshold_grows_with_a_long_poll_interval():
    state = WatcherState(running=True, last_success_at="2026-09-23T11:40:00Z")
    status, _message = service_api._health_status(None, state, poll_interval_s=300.0, now=_NOW)
    assert status == "ok"  # 20 minutes is under ten 5-minute polls


@pytest.mark.parametrize("registered_value", [True, False])
def test_health_reports_service_registered_when_a_probe_is_wired_up(tmp_path, monkeypatch, registered_value):
    handle = _start_server(tmp_path, monkeypatch, service_registered=lambda: registered_value)
    try:
        resp, body = handle.get_json("/api/health")
        assert resp.status == 200
        assert body["data"]["service_registered"] is registered_value
        # v3: the field is a bare boolean -- confirm a wired-up (non-null)
        # probe result still can't smuggle a path/command string into the
        # response (see installer.py's module docstring on why api.py
        # never imports it directly).
        assert_privacy(body)
    finally:
        handle.close()
        handle.store.close()


def test_health_caches_the_service_registered_probe_for_ten_minutes(tmp_path, monkeypatch):
    calls = []

    def probe():
        calls.append(1)
        return True

    fake_time = {"now": 1_000.0}
    monkeypatch.setattr(service_api.time, "monotonic", lambda: fake_time["now"])

    handle = _start_server(tmp_path, monkeypatch, service_registered=probe)
    try:
        handle.get_json("/api/health")
        handle.get_json("/api/health")
        assert len(calls) == 1  # second request within the TTL is served from cache

        fake_time["now"] += service_api._SERVICE_REGISTERED_CACHE_TTL_S + 1
        handle.get_json("/api/health")
        assert len(calls) == 2  # cache expired -- probed again
    finally:
        handle.close()
        handle.store.close()


def test_summary(server):
    resp, body = server.get_json("/api/summary")
    assert resp.status == 200
    assert body["data"]["sessions"] == 1
    assert body["data"]["transcripts"] == 1
    assert_privacy(body)


def test_summary_rejects_bad_window_days(server):
    resp, body = server.get_json("/api/summary?window_days=not-a-number")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_summary_reports_cache_saved_and_cache_read_tokens(server):
    """Additive fields: cache_read_tokens (the fixture's one turns_agg
    row, 90) and cache_saved (that many tokens re-priced as input minus
    what they actually cost at claude-sonnet-5's cache-read rate --
    pricing.toml's 2.0/0.2 USD per million)."""
    resp, body = server.get_json("/api/summary")
    assert resp.status == 200
    assert body["data"]["cache_read_tokens"] == 90
    assert body["data"]["cache_saved"] == pytest.approx(90 * (2.0 - 0.2) / 1_000_000)
    assert_privacy(body)


def test_summary_accepts_since_and_until(server):
    """/api/summary now takes the same window params the report-backed
    routes do (docs/api.md), plus since/until -- but with no params at
    all the behaviour is unchanged (all-time)."""
    resp, body = server.get_json("/api/summary?since=2026-09-17T00:00:00Z&until=2026-09-18T13:30:00Z")
    assert resp.status == 200
    assert body["data"]["sessions"] == 1
    resp, body = server.get_json("/api/summary?until=2026-09-18T11:00:00Z")
    assert resp.status == 200
    assert body["data"]["sessions"] == 0
    resp, body = server.get_json("/api/summary?since=not-a-timestamp")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_sessions_listing_has_no_transcripts_key(server):
    resp, body = server.get_json("/api/sessions")
    assert resp.status == 200
    assert len(body["data"]) == 1
    assert "transcripts" not in body["data"][0]
    assert_privacy(body)


def test_sessions_rejects_bad_limit(server):
    resp, body = server.get_json("/api/sessions?limit=-1")
    assert resp.status == 400


def test_sessions_and_compactions_follow_the_window(server):
    """With a window the listings keep only what happened in it, so the
    Sessions and Usage tabs agree with the numbers above them."""
    _resp, all_sessions = server.get_json("/api/sessions?window=all")
    _resp, future = server.get_json("/api/sessions?since=2099-01-01T00:00:00Z")
    _resp, past = server.get_json("/api/sessions?since=2000-01-01T00:00:00Z")
    assert len(all_sessions["data"]) == len(past["data"]) == 1
    assert future["data"] == []
    _resp, compactions = server.get_json("/api/compactions?since=2099-01-01T00:00:00Z")
    assert compactions["data"] == []
    resp, _body = server.get_json("/api/sessions?since=yesterday")
    assert resp.status == 400


def test_session_detail(server):
    resp, body = server.get_json(f"/api/session/{server.session_id}")
    assert resp.status == 200
    assert body["data"]["id"] == server.session_id
    assert len(body["data"]["transcripts"]) == 1
    assert body["data"]["transcripts"][0]["kind"] == "top-level"
    assert "path" not in body["data"]["transcripts"][0]
    assert body["data"]["tags"] == {"purpose": "refactor-override"}
    assert_privacy(body)
    _assert_no_leak(json.dumps(body).encode("utf-8"))


def test_session_detail_has_no_turn_series_without_a_stored_digest(server):
    # _seed_store's transcript digest_json is the synthetic {"turns": 3}
    # shape (not a real encode_result payload), so Store.turns_for_session
    # can't decode it -- route_session must degrade gracefully and simply
    # omit turn_series/markers rather than 500 or fabricate empty lists.
    resp, body = server.get_json(f"/api/session/{server.session_id}")
    assert resp.status == 200
    assert "turn_series" not in body["data"]
    assert "markers" not in body["data"]


def test_session_detail_turn_series_and_markers(tmp_path, monkeypatch):
    # Deliverable 1.g: GET /api/session/<id> exposes turn_series/markers
    # sourced from the top-level transcript's stored digest.
    from claudeglass.cache import encode_result
    from claudeglass.model import EventKind, Turn, TranscriptMeta, TranscriptResult

    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()
    session_id = corpus.sessions[0].session_id
    store.upsert_session(session_id=session_id, project_slug="proj-a", slug="proj-a")

    result = TranscriptResult(
        meta=TranscriptMeta(path=_FAKE_PATH, kind="top-level", session_id=session_id),
        turns=[
            Turn(turn_index=1, ctx=1000, cache_creation_tokens=500, is_recache=False,
                 preceding_primary=EventKind.HUMAN_TEXT, human_prompt_chars=42),
            Turn(turn_index=2, ctx=1500, cache_creation_tokens=0, is_recache=True,
                 preceding_primary=EventKind.COMPACT_BOUNDARY),
        ],
    )
    store.upsert_transcript(
        session_id=session_id,
        path=_FAKE_PATH,
        kind="top-level",
        digest_json=json.dumps(encode_result(result)),
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)
    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    try:
        resp, body = handle.get_json(f"/api/session/{session_id}")
        assert resp.status == 200
        assert body["data"]["turn_series"] == [
            [1, 1000, 500, False, "human_text"],
            [2, 1500, 0, True, "compact_boundary"],
        ]
        assert body["data"]["markers"] == {"compactions": [2], "spawns": [], "human": [1]}
        assert body["data"]["truncated"] is False
        assert_privacy(body)
        _assert_no_leak(json.dumps(body).encode("utf-8"))
    finally:
        handle.close()
        store.close()


def test_session_detail_limit_markers(tmp_path, monkeypatch):
    # v3-limits wiring: GET /api/session/<id> exposes limit_markers
    # (limits.limit_markers) alongside turn_series/markers, sourced from
    # the same stored top-level transcript digest.
    from claudeglass.cache import encode_result
    from claudeglass.model import Event, EventKind, Turn, TranscriptMeta, TranscriptResult

    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()
    session_id = corpus.sessions[0].session_id
    store.upsert_session(session_id=session_id, project_slug="proj-a", slug="proj-a")

    result = TranscriptResult(
        meta=TranscriptMeta(path=_FAKE_PATH, kind="top-level", session_id=session_id),
        turns=[
            Turn(turn_index=1, ctx=1000, cache_creation_tokens=500, is_recache=False,
                 preceding_primary=EventKind.HUMAN_TEXT, human_prompt_chars=42, ts="2026-09-18T12:00:05.000Z"),
            Turn(turn_index=2, ctx=1500, cache_creation_tokens=25_000, is_recache=True,
                 gap_cause="limit", gap_s=10_795.0, ts="2026-09-18T15:00:10.000Z"),
        ],
        events=[
            Event(kind=EventKind.LIMIT_HIT, subkind="session_limit", ts="2026-09-18T12:00:05.000Z",
                  detail={"reset_minutes_of_day": 15 * 60}),
            Event(kind=EventKind.LIMIT_RESUME, ts="2026-09-18T15:00:00.000Z"),
        ],
    )
    store.upsert_transcript(
        session_id=session_id,
        path=_FAKE_PATH,
        kind="top-level",
        digest_json=json.dumps(encode_result(result)),
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)
    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    try:
        resp, body = handle.get_json(f"/api/session/{session_id}")
        assert resp.status == 200
        assert body["data"]["limit_markers"] == [
            {"ts": "2026-09-18T12:00:05.000Z", "kind": "limit_hit", "detail": {"reset_minutes_of_day": 900, "subkind": "session_limit"}},
            {"ts": "2026-09-18T15:00:00.000Z", "kind": "limit_resume", "detail": {}},
        ]
        assert_privacy(body)
        _assert_no_leak(json.dumps(body).encode("utf-8"))
    finally:
        handle.close()
        store.close()


def test_session_detail_not_found(server):
    resp, body = server.get_json("/api/session/does-not-exist")
    assert resp.status == 404
    assert body["error"]["code"] == "not_found"


def test_recache(server):
    resp, body = server.get_json("/api/recache")
    assert resp.status == 200
    assert body["data"]["by_signature"]["full-expiry"]["turns"] == 1
    assert_privacy(body)


def test_compactions(server):
    resp, body = server.get_json("/api/compactions")
    assert resp.status == 200
    assert body["data"][0]["dropped_tokens"] == 800
    assert_privacy(body)


def test_daily_usage_default_is_unchanged(server):
    resp, body = server.get_json("/api/daily-usage")
    assert resp.status == 200
    assert len(body["data"]) == 1
    row = body["data"][0]
    assert row["day"] == "2026-09-18" and row["model"] == "claude-sonnet-5"
    assert row["cache_read_tokens"] == 90
    assert "agent" not in row
    assert_privacy(body)


def test_daily_usage_accepts_the_shared_window_params(server, monkeypatch):
    resp, body = server.get_json("/api/daily-usage?since=2099-01-01T00:00:00Z")
    assert resp.status == 200
    assert body["data"] == []
    resp, body = server.get_json("/api/daily-usage?window=all")
    assert resp.status == 200
    assert len(body["data"]) == 1

    # "days=7" resolves against the real wall clock (discovery._resolve_window
    # does `datetime.now(timezone.utc) - timedelta(days=days)`), while the
    # seeded row's timestamp is a fixed 2026-09-18. Freeze discovery's notion
    # of "now" to just after that fixed timestamp so this assertion holds
    # regardless of the date this test happens to run on.
    from datetime import datetime, timezone

    from claudeglass import discovery

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(discovery, "datetime", _FrozenDatetime)

    resp, body = server.get_json("/api/daily-usage?days=7")
    assert resp.status == 200
    assert len(body["data"]) == 1  # the legacy param still works unchanged
    resp, body = server.get_json("/api/daily-usage?since=not-a-timestamp")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_daily_usage_rejects_bad_split(server):
    resp, body = server.get_json("/api/daily-usage?split=nonsense")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_daily_usage_split_agent_separates_main_from_subagents(server):
    # The fixture seeds one top-level transcript; add a subagent one so
    # split=agent has something to actually split.
    server.store.upsert_session(
        session_id="sess-sub",
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        slug="proj-a",
        first_ts="2026-09-18T12:00:00Z",
        last_ts="2026-09-18T13:00:00Z",
    )
    server.store.upsert_transcript(
        session_id="sess-sub",
        path=_FAKE_SUB_PATH,
        kind="subagent",
        agent_id="agent-1",
        agent_type="claude-implementer",
        mtime_ns=1,
        size_bytes=1,
        parser_version=3,
        digest_json=json.dumps({"turns": 1}),
        turns_agg=[
            {
                "day": "2026-09-18",
                "model": "claude-sonnet-5",
                "turns": 1,
                "input_tokens": 10,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 5,
                "output_tokens": 2,
                "thinking_tokens": 0,
                "cc_5m": 0,
                "cc_1h": 0,
                "cost": 0.01,
            }
        ],
    )
    resp, body = server.get_json("/api/daily-usage?split=agent")
    assert resp.status == 200
    by_agent = {row["agent"]: row for row in body["data"]}
    assert set(by_agent) == {"main", "subagent"}
    assert by_agent["main"]["turns"] == 3
    assert by_agent["subagent"]["turns"] == 1
    assert_privacy(body)
    resp, body = server.get_json("/api/daily-usage?split=model")
    assert resp.status == 200
    assert "agent" not in body["data"][0]


def test_profiles_listing_has_no_toml_path(server):
    resp, body = server.get_json("/api/profiles")
    assert resp.status == 200
    profiles = body["data"]["profiles"]
    user_entries = [p for p in profiles if p["source"] == "user"]
    assert user_entries == [
        {
            "id": "p1",
            "name": "implementation-heavy",
            "source": "user",
            "archetype": None,
            "for": [],
            "tasks": [],
            "updated_at": user_entries[0]["updated_at"],
        }
    ]
    assert_privacy(body)
    _assert_no_leak(json.dumps(body).encode("utf-8"))


def test_profiles_listing_includes_the_catalogue(server):
    resp, body = server.get_json("/api/profiles")
    assert resp.status == 200
    catalogue_ids = {p["id"] for p in body["data"]["profiles"] if p["source"] == "catalogue"}
    assert catalogue_ids == set(profile_catalogue.CATALOGUE_IDS)
    assert_privacy(body)


def test_profiles_listing_reports_the_baseline_suggested_profile(server):
    resp, body = server.get_json("/api/profiles")
    assert resp.status == 200
    assert body["data"]["suggested_profile_id"] == "implementation-heavy"


def test_baseline_returns_the_latest_capture_and_capture_status(server):
    resp, body = server.get_json("/api/baseline")
    assert resp.status == 200
    data = body["data"]
    assert data["baseline"]["archetype"] == "plan-high-implement-low"
    assert data["baseline"]["record"] == {"sessions": 1, "suggested_profile": "implementation-heavy"}
    assert len(data["history"]) == 1
    assert data["capture_status"]["started"] is False
    assert "not started" in data["capture_status"]["summary"]
    assert_privacy(body)


def test_baseline_is_null_when_none_recorded(tmp_path, monkeypatch):
    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)
    store = Store(tmp_path / "empty.db")
    store.open()
    config_dir = tmp_path / "empty-config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)
    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    try:
        resp, body = handle.get_json("/api/baseline")
        assert resp.status == 200
        assert body["data"]["baseline"] is None
        assert body["data"]["history"] == []
        assert_privacy(body)
    finally:
        handle.close()
        store.close()


def test_set_tag_round_trips(server):
    resp, body = server.post_json(
        f"/api/sessions/{server.session_id}/tags", {"key": "mode", "value": "interactive"}
    )
    assert resp.status == 200
    assert body["data"]["tags"]["mode"] == "interactive"
    assert body["data"]["session_id"] == server.session_id


def test_set_tag_bad_key_is_bad_request(server):
    resp, body = server.post_json(f"/api/sessions/{server.session_id}/tags", {"key": "bogus", "value": "x"})
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_set_tag_bad_value_type_is_bad_request(server):
    resp, body = server.post_json(f"/api/sessions/{server.session_id}/tags", {"key": "mode", "value": 5})
    assert resp.status == 400


def test_set_tag_unknown_session_is_not_found(server):
    resp, body = server.post_json("/api/sessions/does-not-exist/tags", {"key": "mode", "value": "x"})
    assert resp.status == 404


def test_set_tag_bad_json_body_is_bad_request(server):
    resp, raw = server.request(
        "POST", f"/api/sessions/{server.session_id}/tags", body=None
    )
    # No body at all still dispatches with body=None -> handler must
    # reject it as bad_request (key/value both missing).
    body = json.loads(raw)
    assert resp.status == 400


# -- v0.3 profile routes -----------------------------------------------------


def test_profile_diff_for_catalogue_profile_against_latest_snapshot(server):
    # The "server" fixture seeds one (schema-1, no "effective" field)
    # snapshot -- exercises the "a snapshot exists but the diff still
    # has to degrade gracefully" path; the "no snapshot at all" path is
    # covered separately below with a snapshot-free store.
    resp, body = server.get_json("/api/profiles/interactive-chat/diff")
    assert resp.status == 200
    data = body["data"]
    assert data["profile_id"] == "interactive-chat"
    assert data["scope"] == "user"
    assert data["notes"] == []
    assert isinstance(data["settings"], list) and data["settings"]
    assert "claudeglass apply interactive-chat" in data["apply_command"]
    assert data["launch_command"] == "claudeglass apply interactive-chat --launch"
    assert data["prompt"].endswith(PROMPT_RESTART)
    assert_privacy(body)
    _assert_no_leak(json.dumps(body).encode("utf-8"))


def test_profile_diff_rows_say_where_each_change_lands_and_come_with_a_prompt(server):
    resp, body = server.get_json("/api/profiles/interactive-chat/diff?scope=project-local")
    assert resp.status == 200
    data = body["data"]
    row = data["settings"][0]
    assert row["label"] and row["setting"] and row["agent"] is None
    assert row["where"] == ".claude/settings.local.json"
    assert data["dry_run_command"] == data["apply_command"] + " --dry-run"
    assert "settings profile" in data["prompt"] and "show me the diff" in data["prompt"]


def test_profile_diff_notes_missing_snapshot_when_store_has_none(tmp_path, monkeypatch):
    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)
    store = Store(tmp_path / "no-snapshot.db")
    store.open()
    config_dir = tmp_path / "no-snapshot-config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)
    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    try:
        resp, body = handle.get_json("/api/profiles/interactive-chat/diff")
        assert resp.status == 200
        assert any("no config snapshot" in note for note in body["data"]["notes"])
        assert_privacy(body)
    finally:
        handle.close()
        store.close()


def test_profile_diff_unknown_id_is_not_found(server):
    resp, body = server.get_json("/api/profiles/does-not-exist/diff")
    assert resp.status == 404


# -- SEC-P4/F4: path traversal via the <id> path segment -----------------
#
# The route regex (``[^/]+``) only ever sees one raw path segment, so a
# literal ".." with a real "/" would already be blocked by matching a
# *different* route (or none). Percent-encoding the separator
# (``%2F``/``%5C``) hides it from the regex; the dispatcher's own
# ``urllib.parse.unquote`` then decodes it back into a real ``/``/``\``
# before ``_load_profile_by_id`` ever sees it -- ``_ID_RE`` is the guard
# that must catch the decoded id.


@pytest.mark.parametrize("suffix", ["", "/diff"])
@pytest.mark.parametrize(
    "encoded_id",
    [
        "..%2F..%2F..%2Fetc%2Fpasswd",
        "C:%5CWindows%5Csystem32%5Cdrivers%5Cetc%5Chosts",
    ],
)
def test_profile_route_rejects_percent_encoded_path_traversal(server, encoded_id, suffix):
    resp, body = server.get_json(f"/api/profiles/{encoded_id}{suffix}")
    assert resp.status == 404
    assert body["ok"] is False


def test_profile_diff_rejects_a_user_profile_row_with_no_backing_file(server):
    # "p1" is a store-only fixture row (test_service_store.py's own
    # convention) with no real <config_dir>/profiles/p1.toml on disk --
    # the diff route must treat it the same as an unknown id, never
    # crash trying to read a file that was never written.
    resp, body = server.get_json("/api/profiles/p1/diff")
    assert resp.status == 404


def test_profile_diff_bad_scope_is_bad_request(server):
    resp, body = server.get_json("/api/profiles/interactive-chat/diff?scope=bogus")
    assert resp.status == 400


def test_profile_diff_never_includes_a_project_path(server):
    # The route never accepts a client-supplied project directory (see
    # route_profile_diff's own docstring note) -- a project-scoped scope
    # names the folder the command is run from, and a note says to run
    # it in the project's folder.
    resp, body = server.get_json("/api/profiles/interactive-chat/diff?scope=repo")
    assert resp.status == 200
    assert "--project-dir . " in body["data"]["apply_command"] + " "
    assert any("project's own folder" in note for note in body["data"]["notes"])


def test_create_profile_writes_a_real_toml_file_and_is_listed(server):
    payload = {"id": "my-new-profile", "name": "My New Profile", "settings": {"promptCacheTtl": "1h"}}
    resp, body = server.post_json("/api/profiles", payload)
    assert resp.status == 201
    assert body["data"] == {"id": "my-new-profile", "name": "My New Profile", "source": "user", "updated_at": body["data"]["updated_at"]}

    written = server.options.config_dir / "profiles" / "my-new-profile.toml"
    assert written.is_file()
    loaded = profile_schema.load_profile(written)
    assert loaded.settings["promptCacheTtl"] == "1h"

    resp, body = server.get_json("/api/profiles")
    ids = {p["id"] for p in body["data"]["profiles"]}
    assert "my-new-profile" in ids


def test_create_profile_rejects_unknown_key(server):
    resp, body = server.post_json("/api/profiles", {"id": "bad-profile", "bogus_key": 1})
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"
    assert "bogus_key" in body["error"]["message"]


def test_create_profile_rejects_catalogue_id(server):
    resp, body = server.post_json("/api/profiles", {"id": "interactive-chat", "name": "shadow"})
    assert resp.status == 409


def test_create_profile_conflict_without_replace_then_succeeds_with_it(server):
    resp, body = server.post_json("/api/profiles", {"id": "dup-profile", "name": "first"})
    assert resp.status == 201

    resp, body = server.post_json("/api/profiles", {"id": "dup-profile", "name": "second"})
    assert resp.status == 409
    assert body["error"]["code"] == "conflict"

    resp, body = server.post_json("/api/profiles?replace=1", {"id": "dup-profile", "name": "second"})
    assert resp.status == 201
    written = server.options.config_dir / "profiles" / "dup-profile.toml"
    assert profile_schema.load_profile(written).name == "second"


def test_create_profile_bad_body_is_bad_request(server):
    resp, raw = server.request("POST", "/api/profiles", body=None)
    body = json.loads(raw)
    assert resp.status == 400


# -- S3: same-origin / Content-Type guard on mutating routes -----------------


def test_post_wrong_content_type_is_bad_request(server):
    resp, raw = server.request(
        "POST",
        f"/api/sessions/{server.session_id}/tags",
        raw_body=json.dumps({"key": "mode", "value": "agentic"}).encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )
    body = json.loads(raw)
    assert resp.status == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "bad_request"
    # The tag was never set.
    resp2, tags_body = server.get_json(f"/api/session/{server.session_id}")
    assert tags_body["data"]["tags"].get("mode") != "agentic"


def test_post_content_type_with_charset_parameter_is_accepted(server):
    resp, raw = server.request(
        "POST",
        f"/api/sessions/{server.session_id}/tags",
        raw_body=json.dumps({"key": "mode", "value": "agentic"}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    body = json.loads(raw)
    assert resp.status == 200
    assert body["ok"] is True


def test_post_cross_origin_is_forbidden(server):
    resp, raw = server.request(
        "POST",
        f"/api/sessions/{server.session_id}/tags",
        body={"key": "mode", "value": "agentic"},
        headers={"Origin": "https://evil.example"},
    )
    body = json.loads(raw)
    assert resp.status == 403
    assert body["ok"] is False
    assert body["error"]["code"] == "forbidden"
    resp2, session_body = server.get_json(f"/api/session/{server.session_id}")
    assert session_body["data"]["tags"].get("mode") != "agentic"


def test_post_cross_site_sec_fetch_site_is_forbidden(server):
    resp, raw = server.request(
        "POST",
        "/api/profiles",
        body={"id": "csrf-test", "name": "CSRF test"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    body = json.loads(raw)
    assert resp.status == 403
    assert body["ok"] is False
    # Nothing was written.
    assert not (server.options.config_dir / "profiles" / "csrf-test.toml").exists()


def test_post_same_origin_is_allowed(server):
    resp, body = server.post_json(
        f"/api/sessions/{server.session_id}/tags",
        {"key": "mode", "value": "agentic"},
    )
    # post_json sends no Origin/Sec-Fetch-Site at all (plain
    # http.client), which must still be accepted -- but confirm the
    # explicit same-origin/same-origin-site case works too.
    assert resp.status == 200
    resp2, raw2 = server.request(
        "POST",
        f"/api/sessions/{server.session_id}/tags",
        body={"key": "mode", "value": "agentic"},
        headers={"Origin": f"http://127.0.0.1:{server.port}", "Sec-Fetch-Site": "same-origin"},
    )
    body2 = json.loads(raw2)
    assert resp2.status == 200
    assert body2["ok"] is True


def test_post_with_no_content_type_and_no_body_is_bad_request(server):
    """A request with Content-Length: 0 and no Content-Type header must
    be rejected as bad_request (never dispatched with an empty/None
    body) -- this is the same code path review S2's sibling finding
    (a missing header) used to reach the handler directly."""
    resp, raw = server.request("POST", f"/api/sessions/{server.session_id}/tags")
    body = json.loads(raw)
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_post_body_over_64kb_is_payload_too_large(server):
    """G5: a POST body over the 64 KB cap is rejected with 413, on any
    route -- checked here against ``/api/sessions/<id>/tags``, but the
    cap lives in ``do_POST`` itself, ahead of routing."""
    oversized = json.dumps({"key": "mode", "value": "x" * (65536)}).encode("utf-8")
    assert len(oversized) > 65536
    resp, raw = server.request(
        "POST",
        f"/api/sessions/{server.session_id}/tags",
        raw_body=oversized,
        headers={"Content-Type": "application/json"},
    )
    body = json.loads(raw)
    assert resp.status == 413
    assert body["ok"] is False
    assert body["error"]["code"] == "payload_too_large"
    # Nothing was written.
    resp2, session_body = server.get_json(f"/api/session/{server.session_id}")
    assert session_body["data"]["tags"].get("mode") != "x" * 65536


def test_post_body_at_64kb_limit_is_not_rejected_for_size(server):
    """The cap is inclusive of exactly 64 KB -- a body at that size must
    still reach routing/validation, not be turned away as too large."""
    # Pad the value so the whole JSON body lands at exactly 65536 bytes.
    padding_len = 65536 - len(json.dumps({"key": "mode", "value": ""}).encode("utf-8"))
    body_obj = {"key": "mode", "value": "x" * padding_len}
    raw_body = json.dumps(body_obj).encode("utf-8")
    assert len(raw_body) == 65536
    resp, raw = server.request(
        "POST",
        f"/api/sessions/{server.session_id}/tags",
        raw_body=raw_body,
        headers={"Content-Type": "application/json"},
    )
    body = json.loads(raw)
    # Not size-rejected -- it fails (or succeeds) on ordinary validation
    # instead, never on the 413 path.
    assert resp.status != 413
    assert resp.status == 200
    assert body["data"]["tags"]["mode"] == "x" * padding_len


# -- report-backed routes -----------------------------------------------------


def test_ttl_route(server):
    resp, body = server.get_json("/api/ttl")
    assert resp.status == 200
    assert body["ok"] is True
    assert_privacy(body)


@pytest.mark.parametrize("route", ["/api/carry", "/api/compaction-sim", "/api/plan-handoff", "/api/model-swap", "/api/waste"])
def test_v4_report_backed_routes_return_ok(server, route):
    """Mirrors test_ttl_route for the v4 wiring round's four new
    report-backed routes -- each just reads its own like-named section
    back out of the same assembled report `/api/ttl` already builds."""
    resp, body = server.get_json(route)
    assert resp.status == 200
    assert body["ok"] is True
    assert_privacy(body)


def test_report_backed_routes_carry_lead_columns(server):
    """``Table.lead_columns`` (display only) reaches the dashboard the
    same way ``value_labels`` does: in each section route's tables and in
    ``/api/report.json``."""
    from claudeglass.helptext import TABLE_COPY

    resp, body = server.get_json("/api/waste")
    assert resp.status == 200
    tables = {t["name"]: t for t in body["data"]["tables"]}
    assert tables["waste_summary"]["lead_columns"] == TABLE_COPY["waste_summary"].lead_columns
    assert tables["waste_by_cause"]["lead_columns"] == []

    resp, raw = server.request("GET", "/api/report.json")
    assert resp.status == 200
    report = json.loads(raw)["report"]
    tables = {t["name"]: t for s in report["sections"] for t in s["tables"]}
    assert tables["ttl_by_agent_type"]["lead_columns"] == TABLE_COPY["ttl_by_agent_type"].lead_columns


def test_report_keeps_each_snapshots_own_project(server):
    """The watcher files every snapshot under the machine-wide project, but
    a schema-2 snapshot names its own project, and the report groups by
    that, as the CLI's does. Overwriting it put every project under
    "(unknown project)"."""
    from claudeglass.service.store import GLOBAL_PROJECT_SLUG

    for ts, slug in (("2026-09-19T12:00:00Z", "slug:aaaaaaaaaaaa"), ("2026-09-19T13:00:00Z", "slug:bbbbbbbbbbbb")):
        server.store.upsert_snapshot(
            project_slug=GLOBAL_PROJECT_SLUG,
            ts=ts,
            schema_version=2,
            digest_json=json.dumps({"schema": 2, "project_slug": slug, "effective": {"model": "opus"}}),
        )
    resp, raw = server.request("GET", "/api/report.json")
    assert resp.status == 200
    tables = {t["name"]: t for s in json.loads(raw)["report"]["sections"] for t in s["tables"]}
    projects = ", ".join(row[2] for row in tables["config-groups"]["rows"])
    assert "slug:aaaaaaaaaaaa" in projects and "slug:bbbbbbbbbbbb" in projects
    assert "(unknown project)" not in projects


def test_v4_report_backed_routes_accept_since_until(server):
    for route in ("/api/carry", "/api/compaction-sim", "/api/plan-handoff", "/api/model-swap", "/api/waste"):
        resp, body = server.get_json(f"{route}?since=2026-08-01T00:00:00%2B00:00&until=2026-08-31T00:00:00%2B00:00")
        assert resp.status == 200
        assert body["ok"] is True

        resp, body = server.get_json(f"{route}?since=not-a-date")
        assert resp.status == 400
        assert body["error"]["code"] == "bad_request"


def test_recommendations_route(server):
    resp, body = server.get_json("/api/recommendations")
    assert resp.status == 200
    assert isinstance(body["data"], list)
    assert_privacy(body)


def _start_recommending_server(tmp_path, monkeypatch) -> _ServerHandle:
    """The default ``server`` fixture's 3-turn corpus never clears
    recommend()'s minimum sample, so this builds a bigger,
    cache-read-heavy one (same shape as test_recommend_contract.py's),
    which does."""
    project_dir = tmp_path / "projects" / "proj-b"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "session-b.jsonl",
        [
            turn_line(
                timestamp=f"2026-09-{10 + (i % 15):02d}T12:00:00.000Z",
                input_tokens=100,
                output_tokens=50,
                ephemeral_5m_input_tokens=1000,
                cache_read_input_tokens=5000,
            )
            for i in range(220)
        ],
    )
    return _start_server(tmp_path, monkeypatch, corpus=corpus_mod.load_corpus([project_dir]))


def test_ignoring_a_recommendation_marks_its_row_and_can_be_undone(tmp_path, monkeypatch):
    handle = _start_recommending_server(tmp_path, monkeypatch)
    try:
        _resp, body = handle.get_json("/api/recommendations")
        rows = body["data"]
        assert rows and all(row["ignored"] is False and row["ignored_before"] is None for row in rows)
        key = rows[0]["key"]

        resp, body = handle.post_json("/api/recommendations/ignore", {"keys": [key], "ignored": True})
        assert resp.status == 200
        assert body["data"] == {"keys": [key], "ignored": True, "active_profile_id": None}
        _resp, body = handle.get_json("/api/recommendations")
        row = next(r for r in body["data"] if r["key"] == key)
        assert row["ignored"] is True and row["ignored_in"] == "all" and row["ignored_at"]
        assert sum(r["ignored"] for r in body["data"]) == 1
        # The report itself stays complete.
        _resp, raw = handle.request("GET", "/api/report.json")
        assert key in raw.decode("utf-8")

        # Kept under the profile apply last marked active.
        (handle.options.config_dir / "active-profile").write_text("interactive-chat", encoding="utf-8")
        _resp, body = handle.get_json("/api/recommendations")
        assert not any(r["ignored"] for r in body["data"])
        _resp, body = handle.get_json("/api/profiles")
        assert body["data"]["active_profile_id"] == "interactive-chat"
        assert body["data"]["active_profile_name"]
        (handle.options.config_dir / "active-profile").unlink()
        _resp, body = handle.get_json("/api/profiles")
        assert body["data"]["active_profile_id"] is None and body["data"]["active_profile_name"] is None

        resp, body = handle.post_json("/api/recommendations/ignore", {"keys": [key], "ignored": False})
        assert resp.status == 200
        _resp, body = handle.get_json("/api/recommendations")
        assert not any(r["ignored"] for r in body["data"])
    finally:
        handle.close()


def test_ignore_checks_what_it_is_sent(tmp_path, monkeypatch):
    handle = _start_recommending_server(tmp_path, monkeypatch)
    try:
        _resp, body = handle.get_json("/api/recommendations")
        key = body["data"][0]["key"]
        for bad in (
            [],
            {"keys": key, "ignored": True},
            {"keys": [], "ignored": True},
            {"keys": ["../etc/passwd"], "ignored": True},
            {"keys": [key], "ignored": "yes"},
            {"keys": [key] * 101, "ignored": True},
        ):
            resp, body = handle.post_json("/api/recommendations/ignore", bad)
            assert resp.status == 400, bad
        resp, body = handle.post_json("/api/recommendations/ignore", {"keys": ["no-such-rule"], "ignored": True})
        assert resp.status == 404
        resp, _raw = handle.request(
            "POST",
            "/api/recommendations/ignore",
            body={"keys": [key], "ignored": True},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status == 403
        assert not (handle.options.config_dir / "ignored-recommendations.json").exists()
    finally:
        handle.close()


def test_recommendations_carry_a_key_and_saving_usd(tmp_path, monkeypatch):
    """Additive (Task Group B): every recommendation gets a deterministic
    key and its saving as a plain number, alongside the existing fields."""
    project_dir = tmp_path / "projects" / "proj-b"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "session-b.jsonl",
        [
            turn_line(
                timestamp=f"2026-09-{10 + (i % 15):02d}T12:00:00.000Z",
                input_tokens=100,
                output_tokens=50,
                ephemeral_5m_input_tokens=1000,
                cache_read_input_tokens=5000,
            )
            for i in range(220)
        ],
    )
    corpus = corpus_mod.load_corpus([project_dir])
    handle = _start_server(tmp_path, monkeypatch, corpus=corpus)
    try:
        resp, body = handle.get_json("/api/recommendations")
        assert resp.status == 200
        recs = body["data"]
        assert recs, "expected at least one recommendation from this cache-read-heavy corpus"
        keys = [rec["key"] for rec in recs]
        assert all(keys)
        assert len(keys) == len(set(keys))
        for rec in recs:
            assert "saving_usd" in rec
            assert rec["key"] == rec["id"] or rec["key"].startswith(rec["id"] + ":")
    finally:
        handle.close()


def test_ttl_and_recommendations_accept_since_until(server):
    # Shares _window_query with /api/report.json (already tested for
    # forwarding/cache-key behaviour above) -- confirm the other
    # report-backed routes also accept since/until rather than rejecting
    # them as unknown query params.
    resp, body = server.get_json("/api/ttl?since=2026-08-01T00:00:00%2B00:00&until=2026-08-31T00:00:00%2B00:00")
    assert resp.status == 200
    assert body["ok"] is True

    resp, body = server.get_json("/api/recommendations?since=2026-08-01T00:00:00%2B00:00")
    assert resp.status == 200
    assert isinstance(body["data"], list)

    resp, body = server.get_json("/api/ttl?since=not-a-date")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_config_diff_requires_key_or_auto_keys(server):
    resp, body = server.get_json("/api/config-diff")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_config_diff_auto_keys(server):
    resp, body = server.get_json("/api/config-diff?auto_keys=1")
    assert resp.status == 200
    assert isinstance(body["data"], list)


def test_config_diff_unknown_key_is_empty_not_error(server):
    resp, body = server.get_json("/api/config-diff?key=does-not-exist")
    assert resp.status == 200
    assert body["data"] == []


def _reconstruct_snapshots(store: Store) -> list[Snapshot] | None:
    """Mirror ``service/api.py``'s private ``_snapshots_from_store()``
    closure (not reachable from outside ``make_handler``) so this test
    can build the exact same ``snapshots`` argument the route's own
    ``build_report`` call used -- ``_seed_store`` above upserts one
    snapshot row, so the route never actually calls ``build_report``
    with ``snapshots=None``.
    """
    snaps: list[Snapshot] = []
    for row in store.snapshots():
        try:
            data = json.loads(row["digest_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        # Mirror the real closure: a snapshot's own project_slug wins;
        # the store's attribution only fills in when it has none.
        if not data.get("project_slug"):
            data["project_slug"] = row.get("project_slug")
        snaps.append(Snapshot(path=Path(""), ts=row["ts"], data=data))
    snaps.sort(key=lambda s: s.ts)
    return snaps or None


def test_report_json_matches_cli_json_for_same_corpus(server):
    """The core CLI-JSON byte-parity contract: ``/api/report.json``
    must equal ``render_json(build_report(same corpus, ...))``, modulo
    ``ReportMeta.generated_at`` (wall-clock; may differ by a second
    between the two ``build_report`` calls -- the same posture
    ``tests/test_cli.py``'s own byte-identical-output tests take for
    the Markdown "Generated at" line).
    """
    resp, raw = server.request("GET", "/api/report.json")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "application/json"

    config = load_config(server.options.config_dir)
    rates = load_pricing(path=config.pricing_path, config_dir=server.options.config_dir)
    projects = tuple(sorted({b.slug for b in server.corpus.sessions if b.slug}))
    # Mirrors api.py's own _build_report_model: store-set session tags
    # (review finding 7) must be folded into the overrides the report is
    # built with, the same way the real route does, or this "expected"
    # build drifts from `server.store`'s seeded `purpose` tag.
    try:
        overrides = load_session_overrides(server.options.config_dir)
    except ConfigError:
        overrides = {}
    overrides = {sid: dict(entry) for sid, entry in overrides.items()}
    for session_id, tags in server.store.all_tags().items():
        merged = overrides.get(session_id, {})
        merged.update(tags)
        overrides[session_id] = merged
    model = build_report(
        server.corpus,
        rates,
        config,
        projects=projects,
        window="last 30 days",
        snapshots=_reconstruct_snapshots(server.store),
        session_overrides=overrides,
        phases=True,
    )
    expected = json.loads(render_json(model))
    actual = json.loads(raw)

    expected["report"]["meta"]["generated_at"] = "STRIPPED"
    actual["report"]["meta"]["generated_at"] = "STRIPPED"
    assert actual == expected


def test_report_json_is_not_enveloped(server):
    # docs/api.md: report.json/.md/.html are the raw rendered document,
    # not {"ok": ..., "data": ...} -- confirm the top-level shape is the
    # renderer's own, not the envelope's.
    resp, raw = server.request("GET", "/api/report.json")
    body = json.loads(raw)
    assert "schema_version" in body and "report" in body
    assert "ok" not in body


def test_report_md_route(server):
    resp, raw = server.request("GET", "/api/report.md")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/markdown; charset=utf-8"
    assert len(raw) > 0
    _assert_no_leak(raw)


def test_report_html_route(server):
    resp, raw = server.request("GET", "/api/report.html")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/html; charset=utf-8"
    assert len(raw) > 0
    _assert_no_leak(raw)


def test_report_routes_reject_bad_window_days(server):
    resp, body = server.get_json("/api/report.json?window_days=nope")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_report_routes_reject_bad_since_and_until(server):
    resp, body = server.get_json("/api/report.json?since=not-a-date")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"

    resp, body = server.get_json("/api/report.json?until=also-not-a-date")
    assert resp.status == 400
    assert body["error"]["code"] == "bad_request"


def test_report_json_forwards_since_until_to_rebuild_and_ignores_default_window(server, monkeypatch):
    """Release-verification finding: the report-backed routes only ever
    accepted ``window_days`` and silently ignored ``since``/``until``,
    so ``/api/report.json?since=...&until=...`` was byte-identical to a
    plain ``/api/report.json`` (always the last-30-days window) instead
    of the CLI's ``report --since ... --until ...`` for the same span --
    breaking the parity ``docs/api.md`` promises. Confirms ``since``/
    ``until`` reach ``corpus_from_store`` and that ``window_days`` is
    *not* defaulted to 30 alongside them (mirrors the CLI's own
    ``--days``/``--since`` mutually-exclusive argparse group).
    """
    calls = []
    real_corpus = server.corpus

    def recording_corpus_from_store(store, *, days=None, since=None, until=None, window_by="last-reply", project_slugs=None):
        calls.append({"days": days, "since": since, "until": until, "window_by": window_by})
        return real_corpus

    import claudeglass.service as service_pkg

    fake = types.ModuleType("claudeglass.service.rebuild")
    fake.corpus_from_store = recording_corpus_from_store
    monkeypatch.setitem(sys.modules, "claudeglass.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)

    resp, raw = server.request(
        "GET", "/api/report.json?since=2026-08-01T00:00:00%2B00:00&until=2026-08-31T00:00:00%2B00:00"
    )
    assert resp.status == 200
    assert calls == [
        {
            "days": None,
            "since": "2026-08-01T00:00:00+00:00",
            "until": "2026-08-31T00:00:00+00:00",
            "window_by": "last-reply",
        }
    ]
    body = json.loads(raw)
    assert body["report"]["meta"]["window"] == "since 2026-08-01T00:00:00+00:00 until 2026-08-31T00:00:00+00:00"


def test_report_json_since_until_is_a_separate_cache_key_from_window_days(server, monkeypatch):
    calls = {"n": 0}
    real_corpus = server.corpus

    def counting_corpus_from_store(store, *, days=None, since=None, until=None, window_by="last-reply", project_slugs=None):
        calls["n"] += 1
        return real_corpus

    import claudeglass.service as service_pkg

    fake = types.ModuleType("claudeglass.service.rebuild")
    fake.corpus_from_store = counting_corpus_from_store
    monkeypatch.setitem(sys.modules, "claudeglass.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)

    resp1, _ = server.request("GET", "/api/report.json")  # default window_days=30
    resp2, _ = server.request("GET", "/api/report.json?since=2026-08-01T00:00:00%2B00:00")
    resp3, _ = server.request("GET", "/api/report.json?since=2026-08-01T00:00:00%2B00:00")  # cache hit
    assert resp1.status == resp2.status == resp3.status == 200
    assert calls["n"] == 2


def test_report_json_is_memoized_per_window(server, monkeypatch):
    calls = {"n": 0}
    real_corpus = server.corpus

    def counting_corpus_from_store(store, *, days=None, since=None, until=None, window_by="last-reply", project_slugs=None):
        calls["n"] += 1
        return real_corpus

    import claudeglass.service as service_pkg

    fake = types.ModuleType("claudeglass.service.rebuild")
    fake.corpus_from_store = counting_corpus_from_store
    monkeypatch.setitem(sys.modules, "claudeglass.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)

    resp1, _ = server.request("GET", "/api/report.json")
    resp2, _ = server.request("GET", "/api/report.json")
    assert resp1.status == 200
    assert resp2.status == 200
    assert calls["n"] == 1, "second request for the same window should hit the cache, not rebuild"

    # A different window_days is a different cache key -> rebuilds.
    resp3, _ = server.request("GET", "/api/report.json?window_days=7")
    assert resp3.status == 200
    assert calls["n"] == 2


def test_requests_for_a_window_already_being_built_share_that_build(server, monkeypatch):
    """A page opening several panels at once asks for the same window
    several times; only the first builds, the rest wait for it."""
    calls = {"n": 0}
    started, release = threading.Event(), threading.Event()
    real_corpus = server.corpus

    def slow_corpus_from_store(store, *, days=None, since=None, until=None, window_by="last-reply", project_slugs=None):
        calls["n"] += 1
        started.set()
        release.wait(10)
        return real_corpus

    import claudeglass.service as service_pkg

    fake = types.ModuleType("claudeglass.service.rebuild")
    fake.corpus_from_store = slow_corpus_from_store
    monkeypatch.setitem(sys.modules, "claudeglass.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)

    statuses = []

    def fetch():
        resp, _ = server.request("GET", "/api/report.json")
        statuses.append(resp.status)

    first = threading.Thread(target=fetch)
    first.start()
    assert started.wait(10)
    others = [threading.Thread(target=fetch) for _ in range(3)]
    for thread in others:
        thread.start()
    time.sleep(0.3)  # let them reach the build in progress
    release.set()
    for thread in [first, *others]:
        thread.join(10)
    assert statuses == [200] * 4
    assert calls["n"] == 1


def test_report_json_cache_invalidates_when_store_change_token_changes(server, monkeypatch):
    # Deliverable 1.f: the memo key is Store.change_token(), not the raw
    # connection object -- mutating the store (even without touching the
    # window_days cache key) must force a rebuild on the next request.
    calls = {"n": 0}
    real_corpus = server.corpus

    def counting_corpus_from_store(store, *, days=None, since=None, until=None, window_by="last-reply", project_slugs=None):
        calls["n"] += 1
        return real_corpus

    import claudeglass.service as service_pkg

    fake = types.ModuleType("claudeglass.service.rebuild")
    fake.corpus_from_store = counting_corpus_from_store
    monkeypatch.setitem(sys.modules, "claudeglass.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)

    resp1, _ = server.request("GET", "/api/report.json")
    assert resp1.status == 200
    assert calls["n"] == 1

    resp2, _ = server.request("GET", "/api/report.json")
    assert resp2.status == 200
    assert calls["n"] == 1  # unchanged store -> cache hit

    server.store.upsert_transcript(
        session_id=server.session_id,
        path=_FAKE_PATH + ".new",
        kind="subagent",
        digest_json=json.dumps({"turns": 1}),
    )

    # change_token moved: the kept report is served at once, marked as
    # refreshing, while a rebuild runs in the background.
    resp3, _ = server.request("GET", "/api/report.json")
    assert resp3.status == 200
    assert resp3.getheader("X-Figures-Refreshing") == "1"
    _wait_until_fresh(server, "/api/report.json")
    assert calls["n"] == 2


def _count_builds(server, monkeypatch) -> dict:
    """Swap in a rebuild module that counts report builds."""
    calls = {"n": 0}
    real_corpus = server.corpus

    def counting_corpus_from_store(store, *, days=None, since=None, until=None, window_by="last-reply", project_slugs=None):
        calls["n"] += 1
        return real_corpus

    import claudeglass.service as service_pkg

    fake = types.ModuleType("claudeglass.service.rebuild")
    fake.corpus_from_store = counting_corpus_from_store
    monkeypatch.setitem(sys.modules, "claudeglass.service.rebuild", fake)
    monkeypatch.setattr(service_pkg, "rebuild", fake, raising=False)
    return calls


def _touch_store(server, suffix: str) -> None:
    server.store.upsert_transcript(
        session_id=server.session_id,
        path=_FAKE_PATH + suffix,
        kind="subagent",
        digest_json=json.dumps({"turns": 1}),
    )


def _wait_until_fresh(server, path: str, timeout: float = 10.0):
    """Request ``path`` until its figures are no longer refreshing."""
    deadline = time.monotonic() + timeout
    while True:
        resp, raw = server.request("GET", path)
        if resp.getheader("X-Figures-Refreshing") is None or time.monotonic() > deadline:
            assert resp.getheader("X-Figures-Refreshing") is None, "background rebuild never finished"
            return resp, raw
        time.sleep(0.05)


def test_report_backed_routes_say_what_time_their_figures_are_from(server):
    resp, _ = server.request("GET", "/api/report.json")
    as_of = resp.getheader("X-Figures-As-Of")
    assert as_of and service_api._parse_utc(as_of) is not None
    assert resp.getheader("X-Figures-Refreshing") is None
    resp, _ = server.request("GET", "/api/recommendations")
    assert resp.getheader("X-Figures-As-Of") == as_of  # the same kept report
    resp, _ = server.request("GET", "/api/health")
    assert resp.getheader("X-Figures-As-Of") is None


def test_a_report_kept_too_long_is_rebuilt_before_it_is_served(server, monkeypatch):
    calls = _count_builds(server, monkeypatch)
    server.request("GET", "/api/report.json")
    assert calls["n"] == 1
    monkeypatch.setattr(service_api, "_STALE_REPORT_MAX_AGE_S", 0.0)
    _touch_store(server, ".old")

    resp, _ = server.request("GET", "/api/report.json")
    assert calls["n"] == 2  # built while the request waited
    assert resp.getheader("X-Figures-Refreshing") is None


def test_a_named_window_serves_its_last_report_when_its_start_moves_on(server, monkeypatch):
    """A named window's start moves every minute; the report kept for it
    is served (and refreshed behind the scenes) rather than rebuilt while
    the tab waits."""
    calls = _count_builds(server, monkeypatch)
    start = {"since": "2026-09-23T10:00:00Z"}
    monkeypatch.setattr(service_api, "_named_window_since", lambda name, config_dir, now=None, **_kw: (start["since"], ""))

    server.request("GET", "/api/report.json?window=1h")
    assert calls["n"] == 1
    start["since"] = "2026-09-23T10:01:00Z"
    resp, _ = server.request("GET", "/api/report.json?window=1h")
    assert resp.status == 200
    assert resp.getheader("X-Figures-Refreshing") == "1"
    _wait_until_fresh(server, "/api/report.json?window=1h")
    assert calls["n"] == 2

    # An explicit since is its own window: never served another's report.
    server.request("GET", "/api/report.json?since=2026-09-23T10:02:00Z")
    assert calls["n"] == 3


def test_impact_is_served_while_it_refreshes(server):
    resp, _ = server.request("GET", "/api/impact")
    assert resp.status == 200
    assert resp.getheader("X-Figures-Refreshing") is None
    _touch_store(server, ".impact")
    resp, raw = server.request("GET", "/api/impact")
    assert resp.status == 200
    assert resp.getheader("X-Figures-Refreshing") == "1"
    assert json.loads(raw)["ok"] is True
    _wait_until_fresh(server, "/api/impact")


# -- static file serving ------------------------------------------------------


def test_root_serves_placeholder_index_when_static_dir_is_empty(server):
    resp, raw = server.request("GET", "/")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/html"
    assert len(raw) > 0


def test_static_traversal_is_rejected(server):
    resp, raw = server.request("GET", "/static/..%2f..%2fsecrets.txt")
    body = json.loads(raw)
    assert resp.status == 404
    assert body["error"]["code"] == "not_found"


def test_static_missing_file_is_404(server):
    resp, body = server.get_json("/static/does-not-exist.js")
    assert resp.status == 404


def test_static_file_is_served_from_a_real_static_dir(tmp_path, monkeypatch):
    # The package's own static/ is empty at S1-api's own delivery time
    # (a sibling package ships its contents) -- make_handler's
    # static_dir keyword override (an S1-api addition beyond
    # contracts.MakeHandler's bare (store, options), documented on
    # make_handler itself) lets this test point at a tmp_path directory
    # with a real file instead, without writing anything into the
    # source tree.
    corpus = _build_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>hello</html>", encoding="utf-8")
    (static_dir / "app.js").write_text("console.log('hi');", encoding="utf-8")
    (static_dir / "app.css").write_text("body {}", encoding="utf-8")
    (static_dir / "mark.svg").write_text("<svg></svg>", encoding="utf-8")
    (static_dir / "fonts").mkdir()
    (static_dir / "fonts" / "face.woff2").write_bytes(b"wOF2")
    (static_dir / "vendor").mkdir()
    (static_dir / "vendor" / "lib.js").write_text("var lib = 1;", encoding="utf-8")
    # A first-party file whose name merely starts like a pinned folder.
    (static_dir / "vendor.js").write_text("var v = 1;", encoding="utf-8")
    # A tool's own cache folder and a dot-file: never part of the UI.
    (static_dir / ".tool-cache").mkdir()
    (static_dir / ".tool-cache" / "state.json").write_text("{}", encoding="utf-8")
    (static_dir / ".hidden.js").write_text("var h = 1;", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)

    handler_cls = service_api.make_handler(store, options, static_dir=static_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    try:
        resp, raw = handle.request("GET", "/")
        assert resp.status == 200
        assert b"hello" in raw

        resp2, raw2 = handle.request("GET", "/static/app.js")
        assert resp2.status == 200
        # Pinned, not left to mimetypes (which reads the Windows registry):
        # a module script with any other type fails under nosniff.
        assert resp2.getheader("Content-Type") == "text/javascript"
        assert b"console.log" in raw2

        for path, expected in (
            ("/static/app.css", "text/css; charset=utf-8"),
            ("/static/mark.svg", "image/svg+xml"),
            ("/static/fonts/face.woff2", "font/woff2"),
        ):
            resp3, _raw3 = handle.request("GET", path)
            assert resp3.status == 200, path
            assert resp3.getheader("Content-Type") == expected, path

        # The sha256-pinned folders (vendor/, fonts/) may be kept by the
        # browser; everything else, first-party modules included, stays
        # no-store (SECURITY.md, docs/api.md's Security headers).
        for path, expected in (
            ("/static/fonts/face.woff2", "public, max-age=31536000, immutable"),
            ("/static/vendor/lib.js", "public, max-age=31536000, immutable"),
            ("/static/vendor.js", "no-store"),
            ("/static/app.js", "no-store"),
            ("/static/app.css", "no-store"),
            ("/", "no-store"),
        ):
            resp4, _raw4 = handle.request("GET", path)
            assert resp4.status == 200, path
            assert resp4.getheader("Cache-Control") == expected, path
            assert len(resp4.headers.get_all("Cache-Control")) == 1, path

        # HEAD answers with the same header; a miss inside a pinned
        # folder is an ordinary 404, never kept.
        resp5, _raw5 = handle.request("HEAD", "/static/vendor/lib.js")
        assert resp5.status == 200
        assert resp5.getheader("Cache-Control") == "public, max-age=31536000, immutable"
        resp6, _raw6 = handle.request("GET", "/static/vendor/missing.js")
        assert resp6.status == 404
        assert resp6.getheader("Cache-Control") == "no-store"

        # Dot-files and dot-folders are never served: an editor's or a
        # tool's cache in the folder may hold local paths.
        for path in ("/static/.tool-cache/state.json", "/static/.hidden.js", "/static/%2Etool-cache/state.json"):
            resp7, _raw7 = handle.request("GET", path)
            assert resp7.status == 404, path
    finally:
        handle.close()
        store.close()


# -- 500 on unexpected exceptions --------------------------------------------


def test_unexpected_exception_becomes_500_without_traceback(server, monkeypatch):
    # The routing tables are closures inside make_handler, not reachable
    # by name from outside -- exercise the real failure path via a store
    # method monkeypatched to raise, which the health route calls, and
    # confirm the dispatcher's last-resort except clause turns it into a
    # one-line 500 body, never a traceback or the exception's own
    # message text (which could, in principle, embed something private).
    monkeypatch.setattr(server.store, "schema_version", _raise_runtime_error)
    resp, body = server.get_json("/api/health")
    assert resp.status == 500
    assert body["error"]["code"] == "internal_error"
    assert "boom" not in body["error"]["message"]
    assert "Traceback" not in body["error"]["message"]
    assert "\n" not in body["error"]["message"]


def _raise_runtime_error(*args, **kwargs):
    raise RuntimeError("boom: something unexpected happened")


def _raise_import_error(*args, **kwargs):
    # As a lazy import of a module changed on disk would: the message
    # names a local path, which must never reach the response.
    raise ImportError(
        "cannot import name 'build_actions' from 'claudeglass.quick_actions' "
        "(C:\\Users\\someone\\claudeglass\\quick_actions.py)"
    )


@pytest.mark.parametrize("error", [ImportError, ModuleNotFoundError])
def test_import_error_becomes_503_restart_needed(tmp_path, monkeypatch, error):
    """A route that fails to import code changed on disk (the running
    process holding the old modules) says to restart, not 'internal
    error'; the exception's text, which names a path, stays out."""

    def _raise(*_args, **_kwargs):
        if error is ImportError:
            _raise_import_error()
        raise ModuleNotFoundError("No module named 'claudeglass.report_v2' (C:\\Users\\someone)")

    watch = _FakeCodeWatch(_CHANGED)
    handle = _start_server(tmp_path, monkeypatch, code_watch=watch)
    try:
        monkeypatch.setattr(handle.store, "schema_version", _raise)
        resp, body = handle.get_json("/api/health")
        assert resp.status == 503
        assert body["ok"] is False
        assert body["error"]["code"] == "restart_needed"
        message = body["error"]["message"]
        assert "changed on disk since the dashboard started" in message
        assert f"({error.__name__})" in message
        assert "claudeglass install-service" in message
        assert "someone" not in message and "\\" not in message
        # The route asks the watch to look now, not at the next scan.
        assert watch.checks == 1
        assert_privacy(body)
    finally:
        handle.close()
        handle.store.close()


def test_import_error_without_a_seen_change_still_says_restart(server, monkeypatch):
    monkeypatch.setattr(server.store, "schema_version", _raise_import_error)
    resp, body = server.get_json("/api/health")
    assert resp.status == 503
    assert body["error"]["code"] == "restart_needed"
    message = body["error"]["message"]
    assert "couldn't be loaded (ImportError)" in message
    assert "claudeglass install-service" in message
    assert "reinstall ClaudeGlass" in message
    assert "someone" not in message


__all__: list[str] = []


def test_diagnostics_route_returns_the_labelled_table(server):
    resp, body = server.get_json("/api/diagnostics")
    assert resp.status == 200
    table = body["data"]
    assert table["name"] == "data_quality"
    assert [c["key"] for c in table["columns"]] == ["check", "value", "meaning"]
    assert table["value_labels"]["lines"] == "Lines read"
    assert_privacy(body)


# -- Host allowlist (DNS rebinding) -------------------------------------------


@pytest.mark.parametrize("path", ["/api/summary", "/api/report.json", "/", "/static/app.js"])
def test_forged_host_is_refused_on_get(server, path):
    resp, raw = server.request("GET", path, headers={"Host": "attacker.example:8765"})
    assert resp.status == 403
    assert json.loads(raw)["error"]["code"] == "forbidden"


def test_forged_host_is_refused_on_post(server):
    resp, raw = server.request(
        "POST", "/api/profiles", body={"id": "x"}, headers={"Host": "attacker.example"}
    )
    assert resp.status == 403
    assert json.loads(raw)["error"]["code"] == "forbidden"


@pytest.mark.parametrize("host", ["127.0.0.1:1234", "localhost", "LOCALHOST:8765", "[::1]:8765"])
def test_loopback_hosts_are_allowed(server, host):
    resp, _ = server.request("GET", "/api/health", headers={"Host": host})
    assert resp.status == 200


def test_allowed_host_names_add_specific_binds_and_extra_names():
    from claudeglass.service.api import allowed_host_names

    base = ServeOptions(projects_root=Path("p"), config_dir=Path("c"))
    assert "0.0.0.0" not in allowed_host_names(ServeOptions(projects_root=Path("p"), config_dir=Path("c"), bind="0.0.0.0"))
    assert "192.168.1.5" in allowed_host_names(ServeOptions(projects_root=Path("p"), config_dir=Path("c"), bind="192.168.1.5"))
    extra = ServeOptions(projects_root=Path("p"), config_dir=Path("c"), allowed_hosts=("Lens.Local",))
    assert "lens.local" in allowed_host_names(extra)
    assert "lens.local" not in allowed_host_names(base)


# -- readability P6: profile schema, one profile, session explain, from-current --


def test_profile_schema_lists_every_allowlisted_key_in_plain_words(server):
    resp, payload = server.get_json("/api/profile-schema")
    assert resp.status == 200
    data = payload["data"]
    assert {lever["key"] for lever in data["settings"]} == set(profile_schema.SETTINGS_ALLOWLIST)
    assert {lever["key"] for lever in data["agents"]} == set(profile_schema.AGENT_ALLOWLIST)
    effort = next(lever for lever in data["settings"] if lever["key"] == "effortLevel")
    assert effort["label"] == "Effort level"
    assert effort["values"] == ["low", "medium", "high", "xhigh", "max"]
    assert effort["description"]
    assert all(lever["description"] for lever in data["settings"] + data["agents"])
    assert [scope["key"] for scope in data["scopes"]] == ["user", "project-local", "repo"]


def test_profile_route_returns_a_catalogue_profile_and_404s_unknown(server):
    profile_id = profile_catalogue.list_profiles()[0].id
    resp, payload = server.get_json(f"/api/profiles/{profile_id}")
    assert resp.status == 200
    assert payload["data"]["id"] == profile_id
    assert payload["data"]["source"] == "catalogue"
    assert payload["data"]["setting_count"] >= 1
    resp, _payload = server.get_json("/api/profiles/no-such-profile")
    assert resp.status == 404


def test_session_explain_gives_template_sentences(server):
    resp, raw = server.request("GET", f"/api/session/{server.session_id}/explain")
    assert resp.status == 200
    _assert_no_leak(raw)
    data = json.loads(raw)["data"]
    assert data["headline"].startswith("This session cost ")
    assert "3 replies" in data["headline"]
    text = " ".join(data["sentences"])
    assert "No subagents ran" in text
    assert "The cache was rebuilt once" in text
    assert "summarised the conversation once" in text
    parts = {row["part"]: row for row in data["cost_split"]}
    assert set(parts) == {"cache_read", "cache_write", "output", "input"}
    assert round(sum(row["share_pct"] for row in data["cost_split"])) == 100
    resp, _raw = server.request("GET", "/api/session/unknown/explain")
    assert resp.status == 404


def test_session_explain_headline_has_no_bare_dollar_under_a_subscription(server):
    """UX-1: route_session_explain now builds its ``Units`` the same way
    every other report-backed route does (``_report_units(_get_report_model
    (...))``, same idiom ``_compute_impact`` uses) instead of a bare
    ``Units(billing_mode, currency)`` with no elasticity fit -- this
    corpus logs no usage-limit readings, so the fit still falls back to
    "list-price equivalent", but the billing mode itself must still be
    honoured end to end."""
    (server.options.config_dir / "config.toml").write_text('billing = "subscription"\n', encoding="utf-8")
    resp, raw = server.request("GET", f"/api/session/{server.session_id}/explain")
    assert resp.status == 200
    data = json.loads(raw)["data"]
    assert "$" not in data["headline"]
    assert "list-price equivalent" in data["headline"]


def test_profiles_from_current_saves_allowlisted_non_managed_keys(server):
    server.store.upsert_snapshot(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        ts="2026-09-19T12:00:00Z",
        schema_version=2,
        digest_json=json.dumps(
            {
                "effective": {"effortLevel": "high", "model": "opus", "permissions": {"allow": []}},
                "managed_keys": ["model"],
                "effective_agents": {"reviewer": {"effort": "low", "color": "blue"}},
            }
        ),
    )
    # A later apply stamp (active-profile marker) records no settings and
    # must not hide the real snapshot before it.
    server.store.upsert_snapshot(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        ts="2026-09-19T13:00:00Z",
        schema_version=2,
        digest_json=json.dumps({"ts": "2026-09-19T13:00:00Z", "schema_version": 2, "profile_id": "x"}),
    )
    resp, payload = server.post_json("/api/profiles/from-current", {"name": "Mine"})
    assert resp.status == 201, payload
    assert payload["data"]["id"] == "my-current-settings"
    assert payload["data"]["skipped_managed"] == ["model"]
    resp, payload = server.get_json("/api/profiles/my-current-settings")
    assert payload["data"]["settings"] == {"effortLevel": "high"}
    assert payload["data"]["agents"] == {"reviewer": {"effort": "low"}}
    # A second save without replace=1 is a conflict, not an overwrite.
    resp, _payload = server.post_json("/api/profiles/from-current", {})
    assert resp.status == 409


# -- context files, named windows ----------------------------------------


def test_claude_md_route_lists_files_and_404s_an_unknown_id(server):
    (server.options.config_dir.parent / "CLAUDE.md").write_text("# Rules\n\nBe brief.\n", encoding="utf-8")
    resp, payload = server.get_json("/api/claude-md")
    assert resp.status == 200
    data = payload["data"]
    assert data["period"] == "over the last 30 days"
    user = next(item for item in data["files"] if item["level"] == "User")
    resp, payload = server.get_json(f"/api/claude-md/{user['id']}")
    assert resp.status == 200
    assert payload["data"]["section_rows"][0]["heading"] == "Rules"
    resp, _ = server.get_json("/api/claude-md/0123456789abcdef")
    assert resp.status == 404


def test_skills_route_returns_rows_and_fixes(server):
    resp, payload = server.get_json("/api/skills?window=24h")
    assert resp.status == 200
    data = payload["data"]
    assert data["period"] == "in the last 24 hours"
    assert isinstance(data["skills"], list) and "fixes" in data


@pytest.mark.parametrize("name", ["1h", "today", "24h"])
def test_named_windows_resolve_to_since(server, name):
    resp, payload = server.get_json(f"/api/summary?window={name}")
    assert resp.status == 200
    assert "sessions" in payload["data"]
    resp, _raw = server.request("GET", f"/api/report.json?window={name}")
    assert resp.status == 200


def test_all_time_window_has_no_limit(server):
    resp, payload = server.get_json("/api/quick-actions?window=all")
    assert resp.status == 200
    assert payload["data"]["period"] == "over all time"
    assert service_api._window_query({"window": "all"}) == ((None, None, None, "last-reply"), None)
    resp, payload = server.get_json("/api/summary?window=all")
    assert resp.status == 200
    assert "sessions" in payload["data"]


def test_since_last_change_window_needs_a_change(server):
    resp, payload = server.get_json("/api/recommendations?window=change")
    assert resp.status == 400
    assert "No change recorded yet" in payload["error"]["message"]
    resp, payload = server.get_json("/api/summary?window=fortnight")
    assert resp.status == 400


def test_the_change_window_counts_the_sessions_started_since(server, monkeypatch):
    """"Since my last change" counts sessions by their first reply, on
    every store read, so its figures match the "Without this change"
    line; the other windows keep counting sessions by their last."""
    monkeypatch.setattr(
        service_api, "_named_window_since", lambda name, config_dir, now=None, *, latest=None: ("2026-09-18T12:30:00Z", "")
    )
    assert service_api._window_query({"window": "change"}) == ((None, "2026-09-18T12:30:00Z", None, "first-reply"), None)
    assert service_api._window_query({"window": "today"}) == ((None, "2026-09-18T12:30:00Z", None, "last-reply"), None)
    seen = {}
    for name in ("summary", "sessions", "daily_usage", "compactions", "cache_read_tokens_by_model"):
        real = getattr(server.store, name)

        def spy(*args, _name=name, _real=real, **kwargs):
            seen[_name] = kwargs.get("window_by")
            return _real(*args, **kwargs)

        monkeypatch.setattr(server.store, name, spy)
    for route in ("summary", "sessions", "daily-usage", "compactions"):
        resp, payload = server.get_json(f"/api/{route}?window=change")
        assert resp.status == 200, payload
    assert seen == dict.fromkeys(seen, "first-reply") and len(seen) == 5
    server.get_json("/api/summary?window=today")
    assert seen["summary"] == "last-reply"


def test_named_window_since_is_rounded_to_the_minute():
    from datetime import datetime, timezone

    now = datetime(2026, 9, 23, 10, 17, 42, tzinfo=timezone.utc)
    since, reason = service_api._named_window_since("1h", None, now)
    assert since == "2026-09-23T09:17:00Z" and reason == ""
    since, _ = service_api._named_window_since("24h", None, now)
    assert since == "2026-09-22T10:17:00Z"


def test_impact_is_empty_without_changes_and_lists_an_apply(server):
    resp, payload = server.get_json("/api/impact")
    assert resp.status == 200
    assert payload["data"]["changes"] == []
    assert payload["data"]["min_sessions"] >= 1

    from claudeglass.profiles import apply as apply_mod
    from claudeglass.profiles.schema import load_dict

    config_dir = server.options.config_dir
    claude_root = config_dir.parent / "fake-claude"
    claude_root.mkdir()
    plan = apply_mod.plan_apply(
        load_dict({"id": "one-off", "settings": {"effortLevel": "medium"}}),
        scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root,
    )
    apply_mod.execute(plan, config_dir=config_dir)
    resp, payload = server.get_json("/api/impact")
    [change] = payload["data"]["changes"]
    assert change["change"]["keys"] == ["effortLevel"]
    assert change["enough"] is False and "so far" in change["verdict"]
    # P4 leftover: a structured gate alongside the prose verdict, for
    # the dashboard's emptyState() helper.
    from claudeglass import impact as impact_mod

    assert change["gate"] == {"reason": "min_sessions", "have": 0, "need": impact_mod.MIN_SESSIONS}
    # Too few sessions since the change to say what it would have cost without it.
    assert change["without"] is None
    resp, payload = server.get_json("/api/summary?window=change")
    assert resp.status == 200


def test_impact_and_the_last_change_window_see_a_change_only_sessions_show(tmp_path, monkeypatch):
    """A model change no apply or snapshot recorded (EST-P9) reaches the
    impact card and starts the "since my last change" window."""
    from datetime import datetime, timedelta, timezone

    from claudeglass.snapshots import snapshot_project_key

    project_dir = tmp_path / "projects" / "proj-a"
    project_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    for name, days, model in (("s1", 3, "claude-sonnet-5"), ("s2", 1, "claude-opus-5-5")):
        start = now - timedelta(days=days)
        write_jsonl(
            project_dir / f"{name}.jsonl",
            [
                turn_line(timestamp=(start + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), model=model)
                for s in (0, 5)
            ],
        )
    handle = _start_server(tmp_path, monkeypatch, corpus=corpus_mod.load_corpus([project_dir]))
    try:
        resp, payload = handle.get_json("/api/summary?window=change")
        assert resp.status == 200, payload
        resp, payload = handle.get_json("/api/impact")
        [change] = payload["data"]["changes"]
        assert change["change"]["source"] == "transcript"
        assert change["change"]["summary"] == "model: claude-sonnet-5 → claude-opus-5-5"
        assert change["change"]["project"] == snapshot_project_key("proj-a")
        assert change["change"]["project_name"] == "proj-a"
    finally:
        handle.close()
        handle.store.close()


def test_impact_gate_is_null_once_both_sides_have_enough_sessions(server):
    """The other half of the P4-leftover gate: once a change has
    ``min_sessions`` real sessions on each side, ``enough`` is true and
    ``gate`` -- unlike ``verdict``, which always has *some* text -- goes
    back to ``None`` rather than a stale or misleading reason."""
    from claudeglass import impact as impact_mod
    from claudeglass.service import api as service_api

    before = impact_mod.MIN_SESSIONS
    after = impact_mod.MIN_SESSIONS
    assert service_api._min_sessions_gate(before, after, impact_mod.MIN_SESSIONS) is None
    assert service_api._min_sessions_gate(before - 1, after, impact_mod.MIN_SESSIONS) == {
        "reason": "min_sessions",
        "have": before - 1,
        "need": impact_mod.MIN_SESSIONS,
    }
    # The gate reports whichever side is thinner.
    assert service_api._min_sessions_gate(before, 0, impact_mod.MIN_SESSIONS)["have"] == 0


def test_backtest_is_empty_without_predictions(server):
    resp, payload = server.get_json("/api/backtest")
    assert resp.status == 200
    assert payload["data"]["predictions"] == []
    assert set(payload["data"]) >= {"predictions", "judged_just_now", "verdicts"}
    assert "too_little_data" in payload["data"]["verdicts"]


def test_backtest_lists_a_logged_prediction_after_a_matching_apply(server):
    from claudeglass.profiles import apply as apply_mod
    from claudeglass.profiles.schema import load_dict
    from claudeglass.service.watcher import FileWatcher

    resp, payload = server.post_json("/api/whatif", {"settings": {"model": "sonnet"}, "agents": {}, "log": True})
    assert resp.status == 200

    config_dir = server.options.config_dir
    claude_root = config_dir.parent / "fake-claude"
    claude_root.mkdir()
    plan = apply_mod.plan_apply(
        load_dict({"id": "one-off", "settings": {"model": "sonnet"}}),
        scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root,
    )
    apply_mod.execute(plan, config_dir=config_dir)

    # In production a running FileWatcher's own tick ingests
    # prediction-log.jsonl into the store (_scan_predictions); this test
    # server runs no watcher of its own, so run one tick by hand.
    FileWatcher(server.store, server.options).run_once()

    resp, payload = server.get_json("/api/backtest")
    assert resp.status == 200
    [prediction] = payload["data"]["predictions"]
    assert prediction["measure_key"] == "model"
    assert prediction["source"] == "whatif"
    assert prediction["predicted_text"]
    # Judged or not (the seeded corpus may not clear MIN_SESSIONS on
    # both sides), the verdict is always one of the closed set or None.
    assert prediction["verdict"] in (None, *payload["data"]["verdicts"])


def test_backtest_is_cached_between_calls_with_no_new_data(server):
    resp1, payload1 = server.get_json("/api/backtest")
    resp2, payload2 = server.get_json("/api/backtest")
    assert resp1.status == resp2.status == 200
    assert payload1["data"] == payload2["data"]


def test_profile_goals_lists_goals_and_drafts_one(server):
    resp, payload = server.get_json("/api/profile-goals")
    assert resp.status == 200
    ids = [goal["id"] for goal in payload["data"]["goals"]]
    assert "recommendations" in ids and "current" in ids
    resp, payload = server.get_json("/api/profile-goals?goal=cache&window=24h")
    assert resp.status == 200
    data = payload["data"]
    assert data["goal"]["id"] == "cache" and data["period"] == "in the last 24 hours"
    assert set(data) >= {"candidates", "profile", "whatif"}
    resp, payload = server.get_json("/api/profile-goals?goal=nope")
    assert resp.status == 400
    resp, payload = server.get_json("/api/profile-goals?goal=tasks&task=bugfix")
    assert resp.status == 200 and set(payload["data"]) >= {"tasks", "task_labels", "task", "note"}
    resp, payload = server.get_json("/api/profile-goals?goal=tasks&task=not-a-task")
    assert resp.status == 400 and "unknown task" in payload["error"]["message"]


def test_whatif_estimates_and_validates(server):
    resp, payload = server.post_json("/api/whatif", {"settings": {"model": "sonnet"}, "agents": {}})
    assert resp.status == 200
    [row] = payload["data"]["rows"]
    assert row["key"] == "model" and row["effect_text"]
    resp, payload = server.post_json("/api/whatif", {"settings": {"effortLevel": "enormous"}})
    assert resp.status == 400


def test_whatif_task_param_validates_and_scales_the_total(server):
    # No metrics capture in this fixture's corpus, so habits_by_task has
    # no per-task cost to scale by -- PROF-01's scaling degrades to "not
    # estimated" rather than leaving the unscaled (too large) figure in.
    resp, payload = server.post_json("/api/whatif?task=not-a-task", {"settings": {"model": "sonnet"}, "agents": {}})
    assert resp.status == 400 and "unknown task" in payload["error"]["message"]
    resp, payload = server.post_json("/api/whatif?task=bugfix", {"settings": {"model": "sonnet"}, "agents": {}})
    assert resp.status == 200
    data = payload["data"]
    [row] = data["rows"]
    assert row["saving_usd"] is None and "no per-task cost" in row["basis"]
    # Whatever the rows say, the total is exactly their own sum (PROF-01).
    assert data["total_usd"] == sum(r["saving_usd"] for r in data["rows"] if r["saving_usd"] is not None)


def test_whatif_task_param_takes_several_tasks(server):
    # F11: a catalogue profile's normalised tasks arrive comma-separated.
    resp, payload = server.post_json("/api/whatif?task=feature,bugfix", {"settings": {"model": "sonnet"}, "agents": {}})
    assert resp.status == 200
    resp, payload = server.post_json("/api/whatif?task=bugfix,implementation", {"settings": {"model": "sonnet"}})
    assert resp.status == 400 and "'implementation'" in payload["error"]["message"]


def test_a_catalogue_profile_carries_its_for_words_as_tasks(server):
    # F11: the Profiles tab scales a profile's estimate by these; the raw
    # `for` words ("implementation", ...) aren't tasks /api/whatif accepts.
    expected = ["feature", "bugfix", "debug", "refactor", "test", "review"]
    resp, body = server.get_json("/api/profiles/implementation-heavy")
    assert resp.status == 200
    assert body["data"]["tasks"] == expected
    resp, body = server.get_json("/api/profiles")
    entry = next(p for p in body["data"]["profiles"] if p["id"] == "implementation-heavy")
    assert entry["tasks"] == expected


def test_whatif_rejects_cross_site_posts(server):
    resp, _raw = server.request(
        "POST", "/api/whatif", body={"settings": {"model": "sonnet"}}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert resp.status == 403


def test_whatif_without_log_flag_writes_no_prediction(server):
    from claudeglass import config as config_mod

    resp, payload = server.post_json("/api/whatif", {"settings": {"model": "sonnet"}, "agents": {}})
    assert resp.status == 200
    assert config_mod.load_prediction_log(server.options.config_dir) == []


def test_whatif_log_flag_appends_a_prediction_per_estimated_row(server):
    from claudeglass import config as config_mod

    resp, payload = server.post_json(
        "/api/whatif", {"settings": {"model": "sonnet"}, "agents": {}, "log": True}
    )
    assert resp.status == 200
    [row] = payload["data"]["rows"]
    assert row["saving_usd"] is not None
    records = config_mod.load_prediction_log(server.options.config_dir)
    assert len(records) == 1
    assert records[0]["source"] == "whatif"
    assert records[0]["measure_key"] == "model"
    assert records[0]["predicted_usd"] == row["saving_usd"]
    assert records[0]["fidelity"] == row["fidelity"]


def test_whatif_log_flag_skips_rows_that_could_not_be_estimated(server):
    from claudeglass import config as config_mod

    resp, payload = server.post_json(
        "/api/whatif", {"settings": {"effortLevel": "medium"}, "agents": {}, "log": True}
    )
    assert resp.status == 200
    assert payload["data"]["rows"][0]["saving_usd"] is None
    assert config_mod.load_prediction_log(server.options.config_dir) == []


def test_whatif_calibrates_once_three_predictions_for_the_key_are_judged(server):
    from claudeglass import config as config_mod

    for i in range(3):
        pid = f"pred-{i}"
        server.store.upsert_prediction(
            prediction_id=pid, ts="2026-09-20T09:00:00Z", source="whatif", measure_key="model",
            agent=None, predicted_usd=1.0, predicted_pct=None, fidelity="ceiling",
        )
        server.store.judge_prediction(pid, change_ts="2026-09-21T09:00:00Z", verdict="larger", measured_usd=2.0, measured_pct=None)

    resp, payload = server.post_json("/api/whatif", {"settings": {"model": "sonnet"}, "agents": {}, "log": True})
    assert resp.status == 200
    [row] = payload["data"]["rows"]
    assert row["fidelity"] == "calibrated"
    raw = row["uncalibrated_usd"]
    assert raw is not None
    assert row["saving_usd"] == raw * 2.0

    # Logging records the raw, uncalibrated estimate -- not the
    # calibrated one -- so future judging never compounds a correction.
    records = config_mod.load_prediction_log(server.options.config_dir)
    logged = [r for r in records if r["measure_key"] == "model" and r["fidelity"] == "ceiling"]
    assert len(logged) == 1
    assert logged[0]["predicted_usd"] == raw


def test_predictions_seen_marks_a_logged_prediction(server):
    from claudeglass import config as config_mod

    prediction_id = config_mod.append_prediction_log(
        server.options.config_dir,
        source="whatif",
        measure_key="model",
        agent=None,
        predicted_usd=1.0,
        predicted_pct=None,
        fidelity="ceiling",
    )
    server.store.upsert_prediction(
        prediction_id=prediction_id,
        ts="2026-09-20T09:00:00Z",
        source="whatif",
        measure_key="model",
        agent=None,
        predicted_usd=1.0,
        predicted_pct=None,
        fidelity="ceiling",
    )
    resp, payload = server.post_json("/api/predictions/seen", {"id": prediction_id})
    assert resp.status == 200
    assert payload["data"] == {"id": prediction_id, "seen": True}
    [row] = server.store.predictions()
    assert row["seen_at"] is not None


def test_predictions_seen_rejects_a_missing_id(server):
    resp, payload = server.post_json("/api/predictions/seen", {})
    assert resp.status == 400
    resp, payload = server.post_json("/api/predictions/seen", {"id": "does-not-exist"})
    assert resp.status == 200
    assert payload["data"]["seen"] is False


def test_quick_actions_list_and_detail(server):
    resp, payload = server.get_json("/api/quick-actions?window=24h")
    assert resp.status == 200
    checks = payload["data"]["checks"]
    assert [c["id"] for c in checks][:2] == ["models", "effort"]
    assert all(c["status"] in ("act", "ok", "no_data") and c["summary"] for c in checks)
    assert all(isinstance(c["rule_ids"], list) for c in checks)
    for check in checks:
        resp, payload = server.get_json(f"/api/quick-actions/{check['id']}")
        assert resp.status == 200
        assert set(payload["data"]) >= {"question", "table", "fixes", "tips", "rule_ids"}
        assert payload["data"]["rule_ids"] == check["rule_ids"]
    resp, _payload = server.get_json("/api/quick-actions/nope")
    assert resp.status == 404


def test_setup_lists_the_footprint_expectations_and_uninstall(server):
    resp, payload = server.get_json("/api/setup")
    assert resp.status == 200
    data = payload["data"]
    assert {item["key"] for item in data["items"]} >= {"snapshot_hook"}
    assert all(set(item) >= {"title", "status", "token_cost", "undo"} for item in data["items"])
    assert data["expectations"][0]["title"] == "It never uses your Claude tokens"
    assert data["uninstall_command"].endswith("--dry-run")


@pytest.mark.parametrize("registered", [True, False, None])
def test_setup_status_says_what_works_and_names_no_path(tmp_path, monkeypatch, registered):
    handle = _start_server(tmp_path, monkeypatch, service_registered=lambda: registered)
    try:
        resp, raw = handle.request("GET", "/api/setup/status")
        assert resp.status == 200
        _assert_no_leak(raw)
        data = json.loads(raw)["data"]
        items = {item["key"]: item for item in data["items"]}
        assert list(items) == ["billing", "hook", "service", "capture", "skill", "statusline"]
        assert all(set(item) == {"key", "label", "state", "word", "detail", "fix", "essential"} for item in data["items"])
        # Nothing chosen and nothing connected in a fresh config folder.
        assert items["billing"]["state"] == "problem" and items["hook"]["state"] == "problem"
        assert data["done"] is False and data["needs_attention"] >= 2
        assert data["verdict"].endswith("need attention.")
        # This dashboard answering is proof it runs: only the logon task
        # is in question, and an unknown answer is never a problem.
        service = items["service"]
        assert service["state"] == {True: "ok", False: "off", None: "ok"}[registered]
        assert "http://" not in service["detail"]
        if registered is False:
            assert "cleanupPeriodDays" in service["detail"]
            assert service["fix"] == "claudeglass install-service"
    finally:
        handle.close()
        handle.store.close()


# -- metrics capture (/api/health's capture block, /api/capture) -----------


def _config_text(server) -> str:
    path = server.options.config_dir / "config.toml"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def test_health_carries_capture_as_set(server):
    resp, payload = server.get_json("/api/health")
    capture = payload["data"]["capture"]
    assert capture["level"] == "off" and capture["on"] is False and capture["effective"] is False
    assert capture["hooks_ok"] is True
    assert capture["metrics"] == []


def test_capture_lists_every_metric_with_what_why_and_cost(server):
    from claudeglass import capture_catalogue

    resp, raw = server.request("GET", "/api/capture")
    assert resp.status == 200
    _assert_no_leak(raw)
    data = json.loads(raw)["data"]
    assert_privacy(data)
    assert [level["id"] for level in data["levels"]] == [*capture_catalogue.LEVELS, "custom"]
    rows = [row for section in data["sections"] for row in section["metrics"]]
    assert {row["id"] for row in rows} == set(capture_catalogue.METRICS_BY_ID)
    assert all(row["what"] and row["why"] for row in rows)
    assert all(row["on"] and not row["toggle"] for row in rows if row["kind"] == "derived")
    assert data["config"]["on"] is False
    assert data["history"]["sessions"] == 1
    assert data["measured"] is None
    assert data["roi"] is None
    assert data["banner"]["on"] is False
    assert data["banner"]["headline"].startswith("Metrics capture is off.")
    assert "uses your tokens" in data["warning"]


def test_post_capture_saves_the_level_and_names_the_hooks_it_needs(server):
    resp, payload = server.post_json("/api/capture", {"level": "essentials"})
    assert resp.status == 200
    data = payload["data"]
    assert data["changed"] is True
    assert data["config"]["level"] == "essentials" and data["config"]["on"] is True
    assert data["config"]["enabled_at"]
    assert 'level = "essentials"' in _config_text(server)
    assert (server.options.config_dir / "capture-log.jsonl").is_file()
    # settings.json runs no capture hook yet: the page says how to add them.
    assert data["hooks"]["ok"] is False
    assert data["hooks"]["connect_command"] == "claudeglass capture connect"
    task = next(row for s in data["sections"] for row in s["metrics"] if row["id"] == "task")
    assert task["on"] is True and task["needs_hook"] is True
    assert data["measured"]["sessions"] == 0
    assert "no captured sessions yet" in data["banner"]["headline"]
    # Turned on moments ago: too little time has passed to price a
    # weekly cost from, so there's nothing to weigh against either.
    assert data["roi"] is None
    _resp, health = server.get_json("/api/health")
    assert health["data"]["capture"]["level"] == "essentials"
    assert health["data"]["capture"]["hooks_ok"] is False
    # The same change again changes nothing.
    resp, payload = server.post_json("/api/capture", {"level": "essentials"})
    assert payload["data"]["changed"] is False


def test_post_capture_can_hand_the_tags_to_haiku(server):
    resp, payload = server.post_json("/api/capture", {"level": "essentials", "tagger": "haiku"})
    assert resp.status == 200
    data = payload["data"]
    assert data["config"]["tagger"] == "haiku" and "tags by Haiku" in data["config"]["describe"]
    assert 'tagger = "haiku"' in _config_text(server)
    # The page's yes/no says what goes where.
    assert "your own Claude Code login" in data["tagger_text"]["haiku"]
    # The Stop entry that asks Haiku is among the hooks it needs.
    assert "Stop" in data["hooks"]["missing_events"]


def test_post_capture_turning_on_with_no_until_gets_the_default_time_box(server):
    # CAP-8: the dashboard's level picker posts only {"level": ...} when
    # turning capture on (its own "until" control only renders once
    # capture is already on) -- this is exactly the path the default
    # must reach, via config.set_capture's own centralized logic.
    from datetime import datetime, timedelta, timezone

    from claudeglass import capture_catalogue

    resp, payload = server.post_json("/api/capture", {"level": "essentials"})
    assert resp.status == 200
    until = payload["data"]["config"]["until"]
    assert until
    days = (datetime.fromisoformat(until) - datetime.now(timezone.utc)).total_seconds() / 86400
    assert capture_catalogue.DEFAULT_CAPTURE_TIMEBOX_DAYS - 1 < days <= capture_catalogue.DEFAULT_CAPTURE_TIMEBOX_DAYS


def test_post_capture_explicit_empty_until_means_no_limit(server):
    # The API's own way to opt out (until: "") must not be overridden by
    # the default -- only an omitted "until" key gets one.
    resp, payload = server.post_json("/api/capture", {"level": "essentials", "until": ""})
    assert resp.status == 200
    assert payload["data"]["config"]["until"] == ""
    # Bumping the level with "until" left out again keeps that choice.
    resp, payload = server.post_json("/api/capture", {"level": "deep"})
    assert payload["data"]["config"]["until"] == ""


def test_post_capture_picks_metrics_sampling_end_and_feedback(server):
    resp, payload = server.post_json("/api/capture", {"metrics": ["task", "fit"]})
    assert resp.status == 200
    config = payload["data"]["config"]
    # fit rides on result, so result comes with it.
    assert config["level"] == "custom" and config["metrics"] == ["task", "result", "fit"]
    resp, payload = server.post_json(
        "/api/capture", {"sample": 25, "until": "2099-01-01T00:00:00+00:00", "feedback": ["feedback_note"]}
    )
    assert resp.status == 200
    config = payload["data"]["config"]
    assert config["sample"] == 25 and config["until"].startswith("2099-01-01")
    assert config["feedback"] == ["feedback_note"]
    assert payload["data"]["banner"]["feedback_note"].startswith("Finished a piece of work?")
    resp, payload = server.post_json("/api/capture", {"level": "off"})
    assert payload["data"]["config"]["on"] is False and payload["data"]["config"]["until"] == ""


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"colour": "red"},
        {"level": "everything"},
        {"level": "essentials", "metrics": ["task"]},
        {"metrics": ["task", "nope"]},
        {"metrics": "task"},
        {"sample": 33},
        {"sample": True},
        {"until": "2001-01-01"},
        {"until": "next week"},
        {"feedback": ["task"]},
        {"tagger": "gpt"},
    ],
)
def test_post_capture_refuses_bad_bodies(server, body):
    resp, payload = server.post_json("/api/capture", body)
    assert resp.status == 400
    assert payload["error"]["code"] == "bad_request"
    assert _config_text(server) == ""


def test_post_capture_refuses_cross_site_and_other_machines(server, monkeypatch):
    resp, _raw = server.request(
        "POST", "/api/capture", body={"level": "deep"}, headers={"Origin": "https://evil.example"}
    )
    assert resp.status == 403
    monkeypatch.setattr(service_api, "_is_loopback_address", lambda address: False)
    resp, payload = server.post_json("/api/capture", {"level": "deep"})
    assert resp.status == 403
    assert "this machine" in payload["error"]["message"]
    assert _config_text(server) == ""


def test_is_loopback_address():
    for address in ("127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"):
        assert service_api._is_loopback_address(address), address
    for address in ("192.168.1.5", "10.0.0.2", "::ffff:192.168.1.5", "", None, "not-an-ip"):
        assert not service_api._is_loopback_address(address), address


def test_post_capture_that_cannot_be_saved_is_a_conflict_with_the_command(server, monkeypatch):
    def refuse(*args, **kwargs):
        raise ConfigError("config.toml is read-only")

    monkeypatch.setattr(service_api, "set_capture", refuse)
    resp, payload = server.post_json("/api/capture", {"level": "standard"})
    assert resp.status == 409
    assert payload["error"]["code"] == "conflict"
    assert payload["error"]["commands"] == ["claudeglass capture level standard"]


def test_capture_routes_with_a_broken_config_are_conflicts(server):
    (server.options.config_dir / "config.toml").write_text("[capture\nlevel = ", encoding="utf-8")
    resp, raw = server.request("GET", "/api/capture")
    assert resp.status == 409
    _assert_no_leak(raw)
    assert json.loads(raw)["error"]["commands"] == ["claudeglass capture status"]
    resp, payload = server.post_json("/api/capture", {"level": "essentials"})
    assert resp.status == 409
    _resp, health = server.get_json("/api/health")
    assert health["data"]["capture"] is None


def test_a_config_change_refreshes_the_kept_report(server, monkeypatch):
    builds = []
    real = service_api.build_report

    def counting(*args, **kwargs):
        builds.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(service_api, "build_report", counting)
    server.get_json("/api/recommendations")
    server.get_json("/api/recommendations")
    assert len(builds) == 1
    resp, _payload = server.post_json("/api/capture", {"level": "free"})
    assert resp.status == 200
    server.get_json("/api/recommendations")  # served kept, rebuilt behind it
    deadline = time.monotonic() + 10
    while len(builds) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(builds) == 2


def test_report_meta_says_what_amounts_mean(server):
    resp, payload = server.get_json("/api/report.json")
    assert payload["report"]["meta"]["amounts_basis"] == "Amounts are what the tokens cost at list price."


def test_capture_replays_history_again_once_the_first_scan_finishes(tmp_path, monkeypatch):
    """A replay taken mid-scan is short of sessions, so it isn't kept for
    the usual 30 minutes: the first request after the scan replays again."""
    from claudeglass import capture as capture_mod

    calls = []
    real_history = capture_mod.history

    def counting_history(*args, **kwargs):
        calls.append(1)
        return real_history(*args, **kwargs)

    monkeypatch.setattr(capture_mod, "history", counting_history)
    holder = {"state": WatcherState(running=True, scanning=True, scan_started_at="2026-09-23T10:00:00Z")}
    handle = _start_server(tmp_path, monkeypatch, watcher_stats=lambda: None, watcher_state=lambda: holder["state"])
    try:
        handle.get_json("/api/capture")
        handle.get_json("/api/capture")
        assert len(calls) == 1
        holder["state"] = WatcherState(running=True, last_success_at="2026-09-23T10:05:00Z")
        _resp, body = handle.get_json("/api/capture")
        assert len(calls) == 2
        assert body["data"]["history"]["sessions"] == 1
        handle.get_json("/api/capture")
        assert len(calls) == 2
    finally:
        handle.close()
        handle.store.close()


# -- metrics capture feedback: the Sessions tab rating ------------------------


def _feedback_on(server, *ids):
    resp, _payload = server.post_json("/api/capture", {"feedback": list(ids)})
    assert resp.status == 200


def test_session_detail_offers_the_rating_questions_only_while_it_is_on(server):
    resp, payload = server.get_json(f"/api/session/{server.session_id}")
    assert "feedback_questions" not in payload["data"] and payload["data"]["feedback"] is None
    _feedback_on(server, "dashboard_rating")
    resp, payload = server.get_json(f"/api/session/{server.session_id}")
    questions = payload["data"]["feedback_questions"]
    assert [q["key"] for q in questions] == ["outcome", "slow", "worth", "helped"]
    assert questions[1]["multi"] is True and {"word": "none", "label": "Nothing"} in questions[1]["options"]


def test_rating_a_session_saves_words_and_an_empty_rating_clears_it(server):
    url = f"/api/sessions/{server.session_id}/feedback"
    resp, payload = server.post_json(url, {"outcome": "met", "slow": ["tools", "tools"], "worth": "fair", "helped": []})
    assert resp.status == 200
    saved = payload["data"]["feedback"]
    assert saved["outcome"] == "met" and saved["slow"] == ["tools"] and saved["worth"] == "fair"
    resp, payload = server.get_json(f"/api/session/{server.session_id}")
    assert payload["data"]["feedback"]["slow"] == ["tools"]
    resp, payload = server.post_json(url, {"outcome": None, "slow": [], "worth": None, "helped": []})
    assert resp.status == 200 and payload["data"]["feedback"] is None


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"mood": "great"},
        {"outcome": "brilliant"},
        {"outcome": ["met"]},
        {"slow": "tools"},
        {"slow": ["tools", "my boss"]},
        {"worth": 5},
    ],
)
def test_rating_refuses_anything_but_known_words(server, body):
    resp, payload = server.post_json(f"/api/sessions/{server.session_id}/feedback", body)
    assert resp.status == 400 and payload["error"]["code"] == "bad_request"
    assert server.store.feedback(server.session_id) is None


def test_rating_an_unknown_session_is_not_found(server):
    resp, payload = server.post_json("/api/sessions/does-not-exist/feedback", {"outcome": "met"})
    assert resp.status == 404


def test_rating_refuses_cross_site_posts(server):
    resp, _raw = server.request(
        "POST", f"/api/sessions/{server.session_id}/feedback", body={"outcome": "met"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status == 403
    assert server.store.feedback(server.session_id) is None


def test_capture_shows_feedback_counts_and_the_skill_install_note(server):
    import os

    _feedback_on(server, "feedback_skill", "feedback_note", "dashboard_rating")
    server.post_json(f"/api/sessions/{server.session_id}/feedback", {"outcome": "met"})
    resp, payload = server.get_json("/api/capture")
    data = payload["data"]
    rows = {row["id"]: row for section in data["sections"] for row in section["metrics"]}
    assert data["feedback"]["skill"] == "missing" and data["feedback"]["ratings"] == 1
    assert [q["key"] for q in data["feedback"]["questions"]] == ["outcome", "slow", "worth", "helped"]
    skill = rows["feedback_skill"]
    assert skill["needs_install"] is True and skill["install_command"] == "claudeglass capture feedback on"
    assert skill["actual_label"] == "Over the last 14 days"
    assert rows["dashboard_rating"]["answers"] == 1 and rows["dashboard_rating"]["target"] == 10
    assert skill["install_note"] == "The /cl-feedback skill isn't installed"
    assert "The /cl-feedback skill isn't installed: claudeglass capture feedback on" in data["banner"]["notes"]
    # No status line of this tool's in the (fake) settings.json.
    assert rows["feedback_note"]["statusline_note"].startswith("Your status line isn't ClaudeGlass's")

    from claudeglass import footprint

    root = os.environ["CLAUDE_CONFIG_DIR"]
    footprint.write_feedback_skill(root)
    resp, payload = server.get_json("/api/capture")
    rows = {row["id"]: row for section in payload["data"]["sections"] for row in section["metrics"]}
    assert payload["data"]["feedback"]["skill"] == "installed" and rows["feedback_skill"]["needs_install"] is False


# -- project filter (Task Group C) ---------------------------------------


def _build_two_project_corpus(tmp_path: Path) -> corpus_mod.Corpus:
    """Two distinctly-slugged projects (``proj-alpha`` cheaper,
    ``proj-beta`` pricier), for the project-filter tests below."""
    projects_root = tmp_path / "projects"
    alpha_dir = projects_root / "proj-alpha"
    beta_dir = projects_root / "proj-beta"
    alpha_dir.mkdir(parents=True)
    beta_dir.mkdir(parents=True)
    write_jsonl(
        alpha_dir / "session-alpha.jsonl",
        [turn_line(input_tokens=100 + i, output_tokens=20 + i, cache_read_input_tokens=30) for i in range(3)],
    )
    write_jsonl(
        beta_dir / "session-beta.jsonl",
        [turn_line(input_tokens=200 + i, output_tokens=40 + i, cache_read_input_tokens=60) for i in range(3)],
    )
    return corpus_mod.load_corpus([alpha_dir, beta_dir])


def _seed_two_projects(store: Store, corpus: corpus_mod.Corpus) -> dict[str, str]:
    """Seed both of ``corpus``'s sessions under their own slugs -- unlike
    ``_seed_store`` above (which always hardcodes a single ``"proj-a"``
    session regardless of the corpus passed in), this keeps each
    bundle's own ``slug``/``session_id`` so ``resolve_project_slug``/
    project filtering can actually tell the two projects apart.
    ``proj-alpha`` costs 1.0, ``proj-beta`` costs 9.0 -- distinctly
    different so a filtered total can't pass by coincidence. Returns
    ``{slug: session_id}``.
    """
    session_ids: dict[str, str] = {}
    costs = {"proj-alpha": 1.0, "proj-beta": 9.0}
    dropped = {"proj-alpha": 800, "proj-beta": 1600}
    for bundle in corpus.sessions:
        slug = bundle.slug
        cost = costs[slug]
        fake_root = rf"C:\Users\definitely-not-a-real-person\.claude\projects\{slug}"
        store.upsert_session(
            session_id=bundle.session_id,
            project_slug=slug,
            project_root_path=fake_root,
            slug=slug,
            first_ts="2026-09-18T12:00:00Z",
            last_ts="2026-09-18T13:00:00Z",
            span_s=3600.0,
            total_cost=cost,
            total_tokens=450,
        )
        store.upsert_transcript(
            session_id=bundle.session_id,
            path=rf"{fake_root}\{bundle.session_id}.jsonl",
            kind="top-level",
            mtime_ns=123,
            size_bytes=456,
            parser_version=3,
            digest_json=json.dumps({"turns": 3}),
            turns_agg=[
                {
                    "day": "2026-09-18",
                    "model": "claude-sonnet-5",
                    "turns": 3,
                    "input_tokens": 300,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 90,
                    "output_tokens": 63,
                    "thinking_tokens": 0,
                    "cc_5m": 0,
                    "cc_1h": 0,
                    "cost": cost,
                }
            ],
            compactions=[
                {
                    "ts": "2026-09-18T12:30:00Z",
                    "pre_tokens": 1000,
                    "post_tokens": 200,
                    "dropped_tokens": dropped[slug],
                    "trigger": "auto",
                    "join_delta_s": 5.0,
                }
            ],
        )
        session_ids[slug] = bundle.session_id
    return session_ids


def _start_server_with_two_projects(tmp_path, monkeypatch) -> tuple[_ServerHandle, dict[str, str]]:
    """A server backed by two distinct projects (see
    ``_build_two_project_corpus``/``_seed_two_projects``), for the
    project-filter tests below. Bypasses ``_start_server``'s
    ``_seed_store`` (which hardcodes a single ``"proj-a"`` session) so
    both sessions actually land in the store under their own slugs --
    ``resolve_project_slug`` reads ``sessions.slug`` directly, and a
    mismatch here would make every filter assertion meaningless."""
    corpus = _build_two_project_corpus(tmp_path)
    _install_fake_rebuild(monkeypatch, corpus)

    store = Store(tmp_path / "service.db")
    store.open()
    session_ids = _seed_two_projects(store, corpus)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    options = ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir)

    handler_cls = service_api.make_handler(store, options)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    handle = _ServerHandle(httpd, thread, corpus=corpus, store=store, options=options)
    return handle, session_ids


@pytest.fixture
def two_project_server(tmp_path, monkeypatch):
    handle, session_ids = _start_server_with_two_projects(tmp_path, monkeypatch)
    handle.session_ids = session_ids
    try:
        yield handle
    finally:
        handle.close()
        handle.store.close()


def _overview_metric(report: dict, metric: str):
    for section in report["sections"]:
        if section["key"] == "overview":
            for table in section["tables"]:
                if table["name"] == "totals":
                    for row in table["rows"]:
                        if row[0] == metric:
                            return row[1]
    raise KeyError(metric)


def test_summary_filters_by_project(two_project_server):
    resp, body = two_project_server.get_json("/api/summary?project=proj-alpha")
    assert resp.status == 200
    assert body["data"]["sessions"] == 1
    assert body["data"]["total_cost"] == pytest.approx(1.0)

    resp, body = two_project_server.get_json("/api/summary?project=proj-beta")
    assert resp.status == 200
    assert body["data"]["sessions"] == 1
    assert body["data"]["total_cost"] == pytest.approx(9.0)

    resp, body = two_project_server.get_json("/api/summary")
    assert resp.status == 200
    assert body["data"]["sessions"] == 2
    assert_privacy(body)


def test_sessions_filters_by_project(two_project_server):
    resp, body = two_project_server.get_json("/api/sessions?project=proj-alpha")
    assert resp.status == 200
    assert [s["id"] for s in body["data"]] == [two_project_server.session_ids["proj-alpha"]]
    assert body["data"][0]["slug"] == "proj-alpha"
    assert_privacy(body)


def test_daily_usage_filters_by_project(two_project_server):
    resp, body = two_project_server.get_json("/api/daily-usage?project=proj-beta&window=all")
    assert resp.status == 200
    assert sum(row["cost"] for row in body["data"]) == pytest.approx(9.0)

    resp, body = two_project_server.get_json("/api/daily-usage?project=proj-alpha&window=all&split=agent")
    assert resp.status == 200
    assert all(row["agent"] == "main" for row in body["data"])
    assert sum(row["cost"] for row in body["data"]) == pytest.approx(1.0)


def test_compactions_filters_by_project(two_project_server):
    resp, body = two_project_server.get_json("/api/compactions?project=proj-alpha")
    assert resp.status == 200
    assert len(body["data"]) == 1
    assert body["data"][0]["dropped_tokens"] == 800

    resp, body = two_project_server.get_json("/api/compactions?project=proj-beta")
    assert resp.status == 200
    assert len(body["data"]) == 1
    assert body["data"][0]["dropped_tokens"] == 1600


@pytest.mark.parametrize("route", ["/api/summary", "/api/ttl", "/api/sessions", "/api/compactions"])
def test_unknown_project_is_bad_request_without_echoing_it(two_project_server, route):
    needle = "not-a-real-project-xyz"
    resp, body = two_project_server.get_json(f"{route}?project={needle}")
    assert resp.status == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "bad_request"
    assert needle not in body["error"]["message"]


def test_report_json_scoped_by_project(two_project_server):
    resp, body = two_project_server.get_json("/api/report.json?project=proj-alpha")
    assert resp.status == 200
    report = body["report"]
    assert report["meta"]["projects"] == ["proj-alpha"]
    assert _overview_metric(report, "sessions") == 1
    assert_privacy(body)

    resp, body = two_project_server.get_json("/api/report.json?project=proj-beta")
    assert resp.status == 200
    report = body["report"]
    assert report["meta"]["projects"] == ["proj-beta"]
    assert _overview_metric(report, "sessions") == 1


def test_report_meta_projects_sorted_by_cost_descending_at_the_api_level(two_project_server):
    resp, body = two_project_server.get_json("/api/report.json")
    assert resp.status == 200
    # proj-beta (cost 9.0) outspends proj-alpha (cost 1.0) this window --
    # the API's own meta.projects must reflect the same (-cost, slug)
    # order tests/test_report.py already pins at the build_report level.
    assert body["report"]["meta"]["projects"] == ["proj-beta", "proj-alpha"]


def test_report_cache_does_not_leak_across_project_filters(two_project_server):
    """Two different `project` filters (and the unfiltered request) for
    the same window must never share a cached report -- the report
    cache key was widened to include `project` precisely so this
    doesn't happen (see api.py's `_get_report_model`/`_slot_for`)."""
    _resp, alpha = two_project_server.get_json("/api/report.json?project=proj-alpha")
    _resp, beta = two_project_server.get_json("/api/report.json?project=proj-beta")
    _resp, both = two_project_server.get_json("/api/report.json")

    assert alpha["report"]["meta"]["projects"] == ["proj-alpha"]
    assert beta["report"]["meta"]["projects"] == ["proj-beta"]
    assert sorted(both["report"]["meta"]["projects"]) == ["proj-alpha", "proj-beta"]

    # Re-requesting alpha after beta and the combined view must still
    # return alpha's own scoped data, not something left behind by a
    # collided cache slot.
    _resp, alpha_again = two_project_server.get_json("/api/report.json?project=proj-alpha")
    assert alpha_again["report"]["meta"]["projects"] == ["proj-alpha"]
    assert _overview_metric(alpha_again["report"], "sessions") == 1


@pytest.mark.parametrize(
    "route",
    [
        "/api/ttl",
        "/api/carry",
        "/api/compaction-sim",
        "/api/plan-handoff",
        "/api/model-swap",
        "/api/waste",
        "/api/config-diff?auto_keys=1",
        "/api/recommendations",
        "/api/diagnostics",
        "/api/claude-md",
        "/api/skills",
        "/api/profile-goals",
        "/api/quick-actions",
        "/api/report.json",
        "/api/report.md",
        "/api/report.html",
    ],
)
def test_project_filter_does_not_error_on_any_report_backed_route(server, route):
    """A `project=<valid slug>` filter must not error on any
    report-backed route -- `server`'s own single project is `proj-a`
    (see `_seed_store`), so this is a smoke test that the filter is
    wired through every one of them, not a correctness check (the
    dedicated `two_project_server` tests above cover correctness)."""
    sep = "&" if "?" in route else "?"
    resp, _raw = server.request("GET", f"{route}{sep}project=proj-a")
    assert resp.status == 200, f"{route} -> {resp.status}"


def test_project_filter_does_not_error_on_whatif_or_quick_action(server):
    resp, body = server.get_json("/api/quick-actions?project=proj-a")
    assert resp.status == 200
    checks = body["data"]["checks"]
    if checks:
        check_id = checks[0]["id"]
        resp, _body = server.get_json(f"/api/quick-actions/{check_id}?project=proj-a")
        assert resp.status == 200
    resp, _body = server.post_json("/api/whatif?project=proj-a", {"settings": {"model": "sonnet"}, "agents": {}})
    assert resp.status == 200
