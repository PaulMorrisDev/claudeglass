#!/usr/bin/env python3
"""Write a synthetic Claude Code history for the README's screenshots.

Usage::

    python scripts/demo-corpus.py OUT_DIR [--days 30] [--seed 7] [--now ISO8601]

Writes three folders under ``OUT_DIR``, none of which touches your real
``~/.claude``:

- ``projects/``: three made-up projects (``C--work-acme-shop``,
  ``C--work-orbit-api`` and ``C--work-lumen-docs``) with a month of
  sessions. Each project keeps to one main model, except that
  ``orbit-api`` moves from Opus to Sonnet halfway through (the one model
  change the dashboard marks). The sessions launch Explore (Haiku) and
  general-purpose (Sonnet) subagents, rebuild their cache after long
  breaks, and one of them has a conversation summary.
- ``config/``: a ``config.toml`` with ``billing = "subscription"`` and a
  ``usage-log.csv`` whose weekly and 5-hour readings rise with the list
  price of the turns in between, so the dashboard can show amounts as a
  share of plan limits.
- ``claude/``: a Claude Code folder connected the way ``init`` leaves
  it, with the snapshot hook and status line in its ``settings.json``
  and one settings snapshot in ``config/snapshots/``, so the dashboard
  shows a finished setup.

Every prompt, file name and number is made up; apart from the one
snapshot's time and the Python the hook names, the output is the same
for the same ``--seed`` and ``--now``. Point a scratch dashboard at it::

    python -m claudeglass serve --port 8792 \\
        --projects-root OUT_DIR/projects --config-dir OUT_DIR/config \\
        --store OUT_DIR/service.db

Set ``CLAUDE_CONFIG_DIR=OUT_DIR/claude`` where it runs, so the
dashboard reads the demo's Claude Code settings instead of yours, and
``CLAUDEGLASS_COMMAND=python -m claudeglass``, so the commands on the
page don't show your Python's full path.
The README's images are ``#/overview?w=30`` at 1280x800, and the top
card on ``#/actions/recommendations?w=30`` cropped from its title to
the end of its prompt, each with the colour scheme set to dark and then
light.

The transcript lines come from ``tests/helpers.py``, the same builders
the test suite uses, so they have the shape the parser expects.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from helpers import (  # noqa: E402
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class Project:
    slug: str
    cwd: str
    #: Relative weight of this project's share of the sessions.
    weight: float
    prompts: tuple[str, ...]
    files: tuple[str, ...]
    #: The main model, and the one it moves to halfway through, if any.
    model: str = OPUS
    later_model: str | None = None


PROJECTS = (
    Project(
        slug="C--work-acme-shop",
        cwd="C:\\work\\acme-shop",
        weight=0.5,
        prompts=(
            "Add pagination to the orders list",
            "The basket total is wrong when a voucher is applied. Find out why",
            "Write tests for the checkout address form",
            "Rename OrderRow to OrderSummary everywhere",
            "Why does the product page load slowly on first visit?",
            "Move the tax rules into their own module",
        ),
        files=("src/orders/list.tsx", "src/basket/total.ts", "src/checkout/address.tsx", "src/tax/rules.ts"),
    ),
    Project(
        slug="C--work-orbit-api",
        cwd="C:\\work\\orbit-api",
        weight=0.35,
        prompts=(
            "Add rate limiting to the public endpoints",
            "The nightly import job times out. Investigate",
            "Add an index for the bookings query",
            "Review the auth middleware for mistakes",
            "Upgrade the HTTP client and fix what breaks",
        ),
        files=("api/routes/public.py", "jobs/nightly_import.py", "db/migrations/0042_bookings.py", "api/auth.py"),
        later_model=SONNET,
    ),
    Project(
        slug="C--work-lumen-docs",
        cwd="C:\\work\\lumen-docs",
        weight=0.15,
        prompts=(
            "Tidy the getting-started page",
            "Check every link in the guides folder",
            "Write a page on configuring webhooks",
        ),
        files=("guides/getting-started.md", "guides/webhooks.md", "guides/index.md"),
        model=SONNET,
    ),
)


@dataclass
class Clock:
    """The session's current time, moved on by each step."""

    now: datetime

    def iso(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.now.microsecond // 1000:03d}Z"

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Session:
    """One top-level transcript being built, plus its subagents."""

    session_id: str
    project: Project
    model: str
    clock: Clock
    lines: list[dict] = field(default_factory=list)
    #: Tokens already in the cache for the next turn to read.
    context: int = 0
    #: When the cache was last written or read, for spotting expiry.
    cache_touched: datetime | None = None
    subagents: dict[str, tuple[list[dict], dict]] = field(default_factory=dict)
    parent_uuid: str | None = None
    sidechain: bool = False

    def common(self) -> dict:
        return {
            "sessionId": self.session_id,
            "cwd": self.project.cwd,
            "version": "2.4.1",
            "entrypoint": "claude-desktop",
            "gitBranch": "main",
            "isSidechain": self.sidechain,
            "parentUuid": self.parent_uuid,
        }

    def add(self, line: dict) -> None:
        self.parent_uuid = line["uuid"]
        self.lines.append(line)


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _uuid(rng: random.Random) -> str:
    h = _hex(rng, 32)
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:]}"


def _turn(rng: random.Random, s: Session, *, content: list[dict], ttl: str = "1h", model: str | None = None) -> None:
    """One assistant reply. It reads the cached context, writes the new
    tokens since the last reply, and rebuilds the whole cache when the
    cache has expired (after an hour for the main thread, five minutes for
    subagents)."""
    new_tokens = rng.randint(1_500, 9_000)
    expiry = timedelta(hours=1) if ttl == "1h" else timedelta(minutes=5)
    expired = s.cache_touched is not None and s.clock.now - s.cache_touched > expiry
    if s.context == 0 or expired:
        cache_read, cache_write = 0, s.context + new_tokens + (18_000 if s.context == 0 else 0)
    else:
        cache_read, cache_write = s.context, new_tokens
    usage = {
        "input_tokens": rng.randint(2, 12),
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
        "output_tokens": rng.randint(150, 2_400),
        "ephemeral_5m_input_tokens": cache_write if ttl == "5m" else 0,
        "ephemeral_1h_input_tokens": cache_write if ttl == "1h" else 0,
    }
    line = turn_line(
        model=model or s.model,
        content=content,
        timestamp=s.clock.iso(),
        uuid=_uuid(rng),
        message_id=f"msg_{_hex(rng, 24)}",
        request_id=f"req_{_hex(rng, 24)}",
        **usage,
        **s.common(),
    )
    s.add(line)
    s.context = cache_read + cache_write
    s.cache_touched = s.clock.now


def _subagent(rng: random.Random, s: Session, agent_type: str, model: str, description: str) -> None:
    """Launch a subagent from the main thread: the Agent tool call, the
    subagent's own transcript and ``.meta.json``, then the result."""
    tool_use_id = f"toolu_{_hex(rng, 24)}"
    _turn(
        rng,
        s,
        content=[
            {"type": "text", "text": "I'll send a subagent to look."},
            tool_use_block(
                "Agent",
                tool_use_id,
                {"subagent_type": agent_type, "description": description, "prompt": description},
            ),
        ],
    )
    agent_id = f"agent-{_hex(rng, 16)}"
    sub = Session(
        session_id=s.session_id,
        project=s.project,
        model=model,
        clock=Clock(s.clock.now + timedelta(seconds=2)),
        sidechain=True,
    )
    sub.add(user_str_line(description, uuid=_uuid(rng), timestamp=sub.clock.iso(), **sub.common()))
    for _ in range(rng.randint(4, 12)):
        sub.clock.advance(rng.uniform(3, 20))
        tid = f"toolu_{_hex(rng, 24)}"
        tool = rng.choice(("Grep", "Read", "Glob", "Read"))
        _turn(rng, sub, content=[tool_use_block(tool, tid, {"pattern": "order"})], ttl="5m")
        sub.clock.advance(rng.uniform(0.5, 3))
        sub.add(
            user_block_line(
                [tool_result_block(tid, "x" * rng.randint(400, 6_000))],
                uuid=_uuid(rng),
                timestamp=sub.clock.iso(),
                **sub.common(),
            )
        )
    sub.clock.advance(rng.uniform(3, 15))
    _turn(rng, sub, content=[{"type": "text", "text": "Found it."}], ttl="5m")
    meta = {
        "agentType": agent_type,
        "model": model,
        "toolUseId": tool_use_id,
        "description": description,
    }
    s.subagents[agent_id] = (sub.lines, meta)
    s.clock.now = sub.clock.now + timedelta(seconds=2)
    s.add(
        user_block_line(
            [tool_result_block(tool_use_id, "Summary of what the subagent found.")],
            uuid=_uuid(rng),
            timestamp=s.clock.iso(),
            **s.common(),
        )
    )


def _compact(rng: random.Random, s: Session) -> None:
    """An automatic conversation summary: Claude Code swaps the long
    history for a short summary and carries on."""
    pre = s.context
    s.add(
        system_line(
            "compact_boundary",
            uuid=_uuid(rng),
            timestamp=s.clock.iso(),
            compactMetadata={
                "trigger": "auto",
                "preTokens": pre,
                "postTokens": 24_000,
                "cumulativeDroppedTokens": pre - 24_000,
                "durationMs": 41_000,
            },
            **s.common(),
        )
    )
    s.clock.advance(41)
    s.add(
        user_str_line(
            "This session is being continued from a previous conversation. Summary: ...",
            uuid=_uuid(rng),
            timestamp=s.clock.iso(),
            isCompactSummary=True,
            **s.common(),
        )
    )
    s.context = 0


def _session(rng: random.Random, project: Project, start: datetime, *, long: bool, model: str) -> Session:
    s = Session(session_id=_uuid(rng), project=project, model=model, clock=Clock(start))
    prompts = rng.sample(project.prompts, k=min(len(project.prompts), rng.randint(2, 4)))
    for prompt in prompts:
        s.add(user_str_line(prompt, uuid=_uuid(rng), timestamp=s.clock.iso(), **s.common()))
        steps = rng.randint(30, 60) if long else rng.randint(6, 22)
        for step in range(steps):
            s.clock.advance(rng.uniform(4, 40))
            if step == 1 and rng.random() < 0.45:
                _subagent(rng, s, "Explore", HAIKU, f"Find where {rng.choice(project.files)} is used")
                continue
            if step == 4 and rng.random() < 0.2:
                _subagent(rng, s, "general-purpose", SONNET, "Check the change against the style guide")
                continue
            tid = f"toolu_{_hex(rng, 24)}"
            tool = rng.choice(("Read", "Read", "Edit", "Bash", "Grep", "Edit"))
            _turn(rng, s, content=[tool_use_block(tool, tid, {"file_path": rng.choice(project.files)})])
            s.clock.advance(rng.uniform(0.5, 20))
            size = rng.randint(20_000, 60_000) if tool == "Bash" and rng.random() < 0.15 else rng.randint(300, 5_000)
            s.add(
                user_block_line(
                    [tool_result_block(tid, "x" * size)],
                    uuid=_uuid(rng),
                    timestamp=s.clock.iso(),
                    **s.common(),
                )
            )
            if long and s.context > 700_000:
                _compact(rng, s)
        s.clock.advance(rng.uniform(4, 30))
        _turn(rng, s, content=[{"type": "text", "text": "Done. Here's what changed."}])
        # A break before the next prompt: usually minutes, sometimes a
        # meeting or lunch long enough for the cache to expire.
        s.clock.advance(rng.choice((120, 480, 900, 1_500, 2_400, 3_000, 4_500)))
    return s


def _write_session(root: Path, s: Session) -> None:
    project_dir = root / s.project.slug
    project_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(project_dir / f"{s.session_id}.jsonl", s.lines)
    if not s.subagents:
        return
    agent_dir = project_dir / s.session_id / "subagents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for agent_id, (lines, meta) in s.subagents.items():
        write_jsonl(agent_dir / f"{agent_id}.jsonl", lines)
        (agent_dir / f"{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


def build_projects(root: Path, *, days: int, now: datetime, rng: random.Random) -> list[Session]:
    """Two or three sessions on most working days, fewer at weekends."""
    sessions: list[Session] = []
    first_day = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    long_done = False
    for d in range(days):
        day = first_day + timedelta(days=d)
        weekend = day.weekday() >= 5
        count = rng.choice((0, 0, 1)) if weekend else rng.choice((1, 2, 2, 3, 3))
        hour = 8.5
        for _ in range(count):
            hour += rng.uniform(0.2, 2.5)
            start = day + timedelta(hours=hour)
            if start > now - timedelta(minutes=30):
                break
            project = rng.choices(PROJECTS, weights=[p.weight for p in PROJECTS])[0]
            long = not long_done and d > days // 2 and project is PROJECTS[0]
            long_done = long_done or long
            later = project.later_model is not None and d >= days // 2
            s = _session(rng, project, start, long=long, model=project.later_model if later else project.model)
            if s.clock.now > now:
                break
            sessions.append(s)
            hour = (s.clock.now - day).total_seconds() / 3600
    for s in sessions:
        _write_session(root, s)
    return sessions


def build_usage_log(config_dir: Path, projects_root: Path, *, now: datetime, rng: random.Random) -> int:
    """Usage-limit readings like the status line records: a weekly
    reading and a 5-hour reading after each prompt's reply, each rising
    with the list price of the turns since its window began.

    Prices come from the dashboard's own parser and rate card, so the
    readings line up with what it will fit against. Returns the number
    of rows written."""
    from claudeglass import elasticity, pricing
    from claudeglass.corpus import load_corpus
    from claudeglass.tools.log_usage import CSV_FIELDS

    rates = pricing.load_pricing(config_dir=config_dir)
    loaded = load_corpus(sorted(p for p in projects_root.iterdir() if p.is_dir()))
    results = [r for b in loaded.sessions for r in ([b.top] if b.top else []) + b.subs]
    index = elasticity._build_turn_index(results, rates)

    # Weekly limit resets on Thursdays at 09:00 UTC; the 5-hour window
    # starts with the first reply after the last one ended.
    def week_start(t: datetime) -> datetime:
        base = t.replace(hour=9, minute=0, second=0, microsecond=0)
        base -= timedelta(days=(base.weekday() - 3) % 7)
        return base if base <= t else base - timedelta(days=7)

    reading_times = [dt for dt in index.dts if rng.random() < 0.08]
    weekly_spend = max(
        index.sums_in(week_start(t), week_start(t) + timedelta(days=7))[2] for t in index.dts
    )
    weekly_slope = 72.0 / weekly_spend
    five_hour_slope = weekly_slope * 6

    rows = []
    #: (window, resets_at) -> the last reading's time and percentage.
    last: dict[tuple[str, str], tuple[datetime, float]] = {}
    five_start: datetime | None = None
    for t in reading_times:
        if five_start is None or t >= five_start + timedelta(hours=5):
            five_start = t - timedelta(minutes=rng.uniform(5, 60))
        for window, start, end, slope in (
            ("seven_day", week_start(t), week_start(t) + timedelta(days=7), weekly_slope),
            ("five_hour", five_start, five_start + timedelta(hours=5), five_hour_slope),
        ):
            resets_at = end.strftime("%Y-%m-%dT%H:%M:%SZ")
            since, pct = last.get((window, resets_at), (start, 0.0))
            spent = index.sums_in(since, t)[2]
            pct = round(min(99.0, pct + spent * slope * rng.uniform(0.9, 1.1)), 1)
            last[(window, resets_at)] = (t, pct)
            rows.append(
                {
                    "logged_at": t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "session_id": "",
                    "window": window,
                    "used_percentage": pct,
                    "resets_at": resets_at,
                    "source": "statusline",
                }
            )
    with open(config_dir / "usage-log.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def connect_claude(claude_root: Path, config_dir: Path) -> None:
    """Connect ``claude_root`` the way ``init`` does, so the dashboard
    shows a finished setup rather than a "Setup isn't finished" warning:
    the snapshot hook and status line in its ``settings.json``, and one
    settings snapshot, taken by the hook itself as a session starts. The
    hook runs with only the variables Claude Code would give it, so none
    of this machine's own settings or variable names get in."""
    from claudeglass import cli, hook_health, statusline

    hook = cli._load_snapshot_hook_module()
    script = hook.install_hook(config_dir)
    extra_args = f' --config-dir "{config_dir.resolve()}"'
    plan = hook_health.plan_connect(
        config_dir,
        hook_command=hook.hook_command(script=script, extra_args=extra_args),
        statusline_command=statusline.install_command(extra_args=extra_args),
        claude_root=claude_root,
    )
    hook_health.connect(plan)
    cwd = str(Path("C:/work/acme-shop"))
    env = {"CLAUDE_CONFIG_DIR": str(claude_root), "CLAUDE_PROJECT_DIR": cwd}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    payload = {"hook_event_name": "SessionStart", "session_id": "demo-connect", "cwd": cwd, "source": "startup"}
    subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--config-dir", str(config_dir)],
        input=json.dumps(payload),
        text=True,
        env=env,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="folder to write projects/ and config/ into")
    parser.add_argument("--days", type=int, default=30, help="days of history (default: 30)")
    parser.add_argument("--seed", type=int, default=7, help="random seed (default: 7)")
    parser.add_argument("--now", default=None, help="end of the history, ISO 8601 (default: now)")
    args = parser.parse_args(argv)

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rng = random.Random(args.seed)

    projects_root = args.out / "projects"
    config_dir = args.out / "config"
    claude_root = args.out / "claude"
    for folder in (projects_root, config_dir, claude_root):
        if folder.exists() and any(folder.iterdir()):
            parser.error(f"{folder} isn't empty; pick a new OUT_DIR")
        folder.mkdir(parents=True, exist_ok=True)

    sessions = build_projects(projects_root, days=args.days, now=now, rng=rng)
    (config_dir / "config.toml").write_text('billing = "subscription"\n', encoding="utf-8")
    rows = build_usage_log(config_dir, projects_root, now=now, rng=rng)
    connect_claude(claude_root, config_dir)
    print(f"{len(sessions)} sessions in {projects_root}")
    print(f"{rows} usage-limit readings in {config_dir / 'usage-log.csv'}")
    print(f"Claude Code settings, connected, in {claude_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
