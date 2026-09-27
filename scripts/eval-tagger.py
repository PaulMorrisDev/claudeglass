#!/usr/bin/env python3
"""How well do the capture tags come out? Measures Claude Haiku as the
tagger (``[capture] tagger = "haiku"``) against known right answers:
with and without thinking, against a stronger model, against Claude's
own tags, and across repeat runs.

Three steps, each spending no more than it says:

    python scripts/eval-tagger.py record [--only ID ...] [--model sonnet]
        Runs every scenario in scripts/tagger-eval/scenarios.json as a real
        Claude Code session (claude -p) in a fresh copy of
        scripts/tagger-eval/project/, with the capture note asking Claude
        for its own tags, and keeps a trimmed copy of each transcript in
        scripts/tagger-eval/sessions/. Spends Claude tokens (a few dollars
        with Sonnet). The recorded sessions are committed, so this is only
        needed to change the scenarios.

    python scripts/eval-tagger.py judge [--configs haiku,haiku-think,sonnet] [--repeats 3] [--jobs 6] [--hook PATH]
        Builds the hook's excerpt of each scenario's last turn (with
        Claude's own tag taken out of the reply) and asks each judge
        configuration for the tag, --repeats times, the way the hook does
        (capture-hook.py's ask_haiku). Writes the raw answers to
        scripts/tagger-eval/results/<time>.json. Spends a little: about
        $0.0015 a Haiku call.

    python scripts/eval-tagger.py score [RESULTS] [--out FILE]
        The report, from the newest results file by default: accuracy per
        key against the known answers (as the hook stores them, and before
        its plan and skill corrections), how often repeat runs agree, cost
        and time per call, Claude's own tags scored the same way, and how
        often Haiku agrees with Claude. Spends nothing.

It loads the real hook script, so it measures the excerpt, instructions,
word filter and corrections the hook uses today. Run it after changing
any of them. It spends real tokens, so it never runs in CI.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "scripts" / "tagger-eval"
PROJECT = HERE / "project"
SESSIONS = HERE / "sessions"
RESULTS = HERE / "results"
HOOK_PATH = ROOT / "src" / "claudeglass" / "hooks" / "capture-hook.py"


def _load_hook(path: Path = HOOK_PATH):
    spec = importlib.util.spec_from_file_location("_capture_hook_eval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = _load_hook()
CATALOGUE = HOOK.load_catalogue()


def _use_hook(path: Path) -> None:
    """Judge with another copy of the hook script (and the catalogue
    beside it), to compare a change against what it replaces."""
    global HOOK, CATALOGUE
    HOOK = _load_hook(path)
    CATALOGUE = HOOK.load_catalogue(path.with_name(HOOK.CATALOGUE_FILE))
#: Every key a main-session tag can carry: the Deep level's.
KEYS = [m["id"] for m in CATALOGUE["metrics"] if m["main_line"]]

#: Judge configurations: the model ``claude --model`` takes, and the
#: thinking budget (``MAX_THINKING_TOKENS``; 0 is none).
CONFIGS = {
    "haiku": {"model": "haiku", "thinking_tokens": 0},
    "haiku-think": {"model": "haiku", "thinking_tokens": 4000},
    "sonnet": {"model": "sonnet", "thinking_tokens": 0},
    "sonnet-think": {"model": "sonnet", "thinking_tokens": 4000},
}
DEFAULT_CONFIGS = ("haiku", "haiku-think", "sonnet")

#: What recorded sessions may run without asking (``--allowedTools``);
#: anything else is refused, which the excerpt then counts as an error.
ALLOWED_TOOLS = (
    "Read", "Edit", "Write", "Glob", "Grep", "Skill", "TodoWrite",
    "Bash(python:*)", "Bash(python3:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(grep:*)", "Bash(mkdir:*)",
)

#: Tool-input fields the excerpt reads; everything else is dropped from a
#: recorded transcript, as is every tool result's content.
_INPUT_KEYS = ("file_path", "notebook_path", "command", "skill")
_TOP_KEYS = (
    "type", "uuid", "parentUuid", "isSidechain", "isMeta", "isCompactSummary", "timestamp", "requestId",
    "permissionMode", "promptId",
)
_TAG_RE = re.compile(r"`?\[(?:tl|result):[^\[\]\n]{0,400}\]`?")


def _scenarios(only=()) -> list[dict]:
    data = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))["scenarios"]
    return [s for s in data if not only or s["id"] in only]


# -- record -----------------------------------------------------------------------


def _claude_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) if base else Path.home() / ".claude"


def _trim(record: dict) -> dict | None:
    """What the excerpt and the scoring need from one transcript line."""
    if record.get("type") not in ("user", "assistant"):
        return None
    out = {key: record[key] for key in _TOP_KEYS if key in record}
    if "toolUseResult" in record:
        out["toolUseResult"] = True
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    kept = {key: message[key] for key in ("role", "id", "model") if key in message}
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    if usage:
        kept["usage"] = {
            key: usage[key]
            for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            if key in usage
        }
    content = message.get("content")
    if isinstance(content, list):
        blocks = []
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text":
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif kind == "tool_use":
                given = block.get("input") if isinstance(block.get("input"), dict) else {}
                blocks.append({
                    "type": "tool_use", "id": block.get("id"), "name": block.get("name"),
                    "input": {key: value for key, value in given.items() if key in _INPUT_KEYS},
                })
            elif kind == "tool_result":
                blocks.append({
                    "type": "tool_result", "tool_use_id": block.get("tool_use_id"),
                    "is_error": bool(block.get("is_error")), "content": "",
                })
            elif kind == "image":
                blocks.append({"type": "image"})
        content = blocks
    kept["content"] = content
    out["message"] = kept
    return out


def _record_one(scenario: dict, model: str, python: str) -> str:
    sid = str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="tagger-eval-") as tmp:
        tmp_path = Path(tmp)
        work = tmp_path / "work"
        shutil.copytree(PROJECT, work)
        config_dir = tmp_path / "cg"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            '[capture]\nlevel = "custom"\nmetrics = [' + ", ".join(f'"{k}"' for k in KEYS) + ']\n'
            'tagger = "claude"\n',
            encoding="utf-8",
        )
        hook = f'"{python}" "{HOOK_PATH}" --config-dir "{config_dir}"'
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"hooks": {"SessionStart": [
            {"matcher": "startup|clear|compact", "hooks": [{"type": "command", "command": hook, "timeout": 10}]},
        ]}}), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != CATALOGUE["judge"]["env"]}
        for number, prompt in enumerate(scenario["turns"]):
            mode = scenario.get("mode", "acceptEdits") if number == len(scenario["turns"]) - 1 else "acceptEdits"
            args = [
                "claude", "-p", prompt, "--model", model, "--settings", str(settings), "--permission-mode", mode,
                "--allowedTools", *ALLOWED_TOOLS,
            ]
            args += ["--session-id", sid] if number == 0 else ["--resume", sid]
            done = subprocess.run(args, cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=600)
            if done.returncode != 0:
                raise RuntimeError(f"{scenario['id']} turn {number + 1}: {done.stderr.decode(errors='replace')[-300:]}")
    found = list(_claude_dir().glob(f"projects/*/{sid}.jsonl"))
    if not found:
        raise RuntimeError(f"{scenario['id']}: no transcript for session {sid}")
    source = found[0]
    lines = []
    for raw in source.read_text(encoding="utf-8").splitlines():
        trimmed = _trim(json.loads(raw)) if raw.strip() else None
        if trimmed is not None:
            lines.append(json.dumps(trimmed, ensure_ascii=False))
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / f"{scenario['id']}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The recorded session isn't one of yours: out of your transcripts.
    source.unlink()
    shutil.rmtree(source.with_suffix(""), ignore_errors=True)
    try:
        source.parent.rmdir()
    except OSError:
        pass
    return scenario["id"]


def cmd_record(args) -> int:
    plans = _claude_dir() / "plans"
    before = set(plans.glob("*.md")) if plans.is_dir() else set()
    scenarios = _scenarios(args.only)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(_record_one, s, args.model, sys.executable): s["id"] for s in scenarios}
        for future in concurrent.futures.as_completed(futures):
            try:
                print(f"recorded {future.result()}", flush=True)
            except Exception as exc:  # noqa: BLE001 - report and go on
                print(f"FAILED {futures[future]}: {exc}", flush=True)
    # Plan mode writes its plan under <claude>/plans: drop the ones made here.
    for path in (set(plans.glob("*.md")) if plans.is_dir() else set()) - before:
        path.unlink()
    return 0


# -- judge ------------------------------------------------------------------------


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _final_text(records: list[dict]) -> str:
    for record in reversed(records):
        if record.get("type") != "assistant" or record.get("isSidechain"):
            continue
        texts = [b["text"] for b in record["message"].get("content") or [] if b.get("type") == "text" and b["text"].strip()]
        if texts:
            return texts[-1]
    return ""


def _untagged(records: list[dict]) -> list[dict]:
    """The transcript as Haiku mode leaves it: no tag at the end of a reply."""
    out = []
    for record in records:
        record = json.loads(json.dumps(record))
        if record.get("type") == "assistant":
            for block in record["message"].get("content") or []:
                if block.get("type") == "text":
                    block["text"] = _TAG_RE.sub("", block["text"]).rstrip()
        out.append(record)
    return out


def _job(scenario_id: str) -> tuple[dict, dict]:
    """The hook's job for a scenario's last turn, and what the scoring
    needs from the session: Claude's own tag and whether a skill ran."""
    records = _records(SESSIONS / f"{scenario_id}.jsonl")
    clean = _untagged(records)
    mode = next((r.get("permissionMode") for r in reversed(records) if r.get("permissionMode")), "default")
    payload = {"hook_event_name": "Stop", "session_id": scenario_id, "cwd": "/work",
               "last_assistant_message": _final_text(clean), "permission_mode": mode}
    facts: dict = {}
    excerpt, reply = HOOK.judge_excerpt(clean, payload, CATALOGUE, True, facts)
    job = {"ts": "", "reply": reply, "keys": KEYS, "system": HOOK.build_judge_prompt(CATALOGUE, KEYS),
           "excerpt": excerpt, "facts": facts}
    last_human = max(i for i, r in enumerate(records) if HOOK._typed(r, CATALOGUE["coaching"]["interrupt_prefix"]))
    skill_ran = any(
        b.get("type") == "tool_use" and b.get("name") == "Skill"
        for r in records[last_human:] if r.get("type") == "assistant" for b in r["message"].get("content") or []
    )
    own = HOOK.judge_tag(_final_text(records), KEYS, CATALOGUE["judge"])
    return job, {"claude": own, "skill_ran": skill_ran}


def _ask(job: dict, config: dict) -> dict:
    judge = {**CATALOGUE["judge"], "model": config["model"], "thinking_tokens": config["thinking_tokens"]}
    started = time.monotonic()
    try:
        answer = HOOK.ask_haiku(job, judge)
    except Exception as exc:  # noqa: BLE001 - a failed call is a result too
        return {"error": type(exc).__name__, "seconds": round(time.monotonic() - started, 2)}
    tag = HOOK.judge_tag(answer.get("result"), job["keys"], CATALOGUE["judge"])
    usage = answer.get("usage") or {}
    return {
        "result": answer.get("result"),
        "raw": tag,
        "tag": HOOK.grounded(tag, job["facts"]),
        "usd": answer.get("total_cost_usd"),
        "seconds": round(time.monotonic() - started, 2),
        "tokens_in": sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
        "tokens_out": usage.get("output_tokens"),
        "thinking": (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0),
    }


def cmd_judge(args) -> int:
    if args.hook:
        _use_hook(Path(args.hook).resolve())
    configs = [c for c in args.configs.split(",") if c]
    unknown = [c for c in configs if c not in CONFIGS]
    if unknown:
        print(f"unknown config {', '.join(unknown)}; known: {', '.join(CONFIGS)}")
        return 2
    scenarios = [s for s in _scenarios(args.only) if (SESSIONS / f"{s['id']}.jsonl").is_file()]
    if not scenarios:
        print("no recorded sessions: run 'record' first")
        return 2
    out = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"), "configs": {c: CONFIGS[c] for c in configs},
           "repeats": args.repeats, "hook": args.hook or "current", "scenarios": {}}
    tasks = []
    for scenario in scenarios:
        job, facts = _job(scenario["id"])
        out["scenarios"][scenario["id"]] = {**facts, "excerpt": job["excerpt"], "facts": job["facts"],
                                            "runs": {c: [None] * args.repeats for c in configs}}
        tasks += [(scenario["id"], job, c, n) for c in configs for n in range(args.repeats)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(_ask, job, CONFIGS[c]): (sid, c, n) for sid, job, c, n in tasks}
        for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            sid, c, n = futures[future]
            out["scenarios"][sid]["runs"][c][n] = future.result()
            if count % 20 == 0 or count == len(tasks):
                print(f"{count}/{len(tasks)} calls", flush=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")
    return 0


# -- score ------------------------------------------------------------------------


def words(tag: str | None) -> dict[str, str]:
    return dict(word.split("=", 1) for word in (tag or "").split())


def accepts(expected, got: str | None) -> bool:
    """Whether ``got`` (a key's word, ``None`` when left out) is one of
    ``expected``: a value, ``None`` for "left out", or for the ``missing``
    list ``~a|b``: not ``none`` and naming at least one of a, b."""
    for option in expected:
        if option is None and got is None:
            return True
        if isinstance(option, str) and option.startswith("~"):
            listed = set((got or "").split(",")) - {""}
            if listed and "none" not in listed and listed & set(option[1:].split("|")):
                return True
        elif option is not None and got is not None and option == got:
            return True
    return False


def expected_for(scenario: dict, facts: dict) -> dict:
    expect = dict(scenario["expect"])
    if expect.get("skill") == "auto":
        expect["skill"] = ["helped", "unneeded"] if facts["skill_ran"] else ["none", "would-help", None]
    return expect


def _pct(n: float, d: float) -> str:
    return f"{100 * n / d:.0f}%" if d else "–"


def _majority(values: list) -> str | None:
    return Counter(values).most_common(1)[0][0] if values else None


def score(results: dict, scenarios: dict) -> str:
    configs = list(results["configs"])
    rows = results["scenarios"]
    lines = [f"# Tagger eval, {results['when']} (hook: {results.get('hook', 'current')})", "",
             f"{len(rows)} scenarios, {results['repeats']} runs each per judge. Accuracy is against the known "
             "answers in scenarios.json, over every scored key of every run.", ""]

    def tally(pick) -> tuple[Counter, Counter]:
        right, total = Counter(), Counter()
        for sid, row in rows.items():
            expect = expected_for(scenarios[sid], row)
            for tag in pick(row):
                got = words(tag)
                for key, options in expect.items():
                    total[key] += 1
                    right[key] += accepts(options, got.get(key))
        return right, total

    table = ["| Judge | Accuracy | Before corrections | Same answer in every run | $ a call | Seconds a call | Thinking tokens | Failed calls |",
             "|---|---|---|---|---|---|---|---|"]
    per_key: dict[str, tuple[Counter, Counter]] = {}
    for config in configs:
        runs = [r for row in rows.values() for r in row["runs"][config] if r]
        good = [r for r in runs if "error" not in r]
        right, total = tally(lambda row, c=config: [r["tag"] for r in row["runs"][c] if r and "error" not in r])
        raw_right, raw_total = tally(lambda row, c=config: [r["raw"] for r in row["runs"][c] if r and "error" not in r])
        per_key[config] = (right, total)
        same = pairs = 0
        for row in rows.values():
            tags = [words(r["tag"]) for r in row["runs"][config] if r and "error" not in r]
            if len(tags) < 2:
                continue
            for key in KEYS:
                pairs += 1
                same += len({t.get(key) for t in tags}) == 1
        usd = sum(r.get("usd") or 0 for r in good) / len(good) if good else 0
        secs = sum(r["seconds"] for r in good) / len(good) if good else 0
        thinking = sum(r.get("thinking") or 0 for r in good) / len(good) if good else 0
        table.append(
            f"| {config} | {_pct(sum(right.values()), sum(total.values()))} | "
            f"{_pct(sum(raw_right.values()), sum(raw_total.values()))} | {_pct(same, pairs)} | ${usd:.4f} | "
            f"{secs:.1f} | {thinking:.0f} | {len(runs) - len(good)} |"
        )
    right, total = tally(lambda row: [row["claude"]])
    per_key["claude (own tags)"] = (right, total)
    table.append(f"| Claude's own tags (the session's model, one run) | {_pct(sum(right.values()), sum(total.values()))} | – | – | – | – | – | – |")
    lines += ["## Overall", "", *table, ""]

    keys = [k for k in KEYS if any(k in s["expect"] for s in scenarios.values())]
    lines += ["## Accuracy by key", "", "| Key | " + " | ".join(per_key) + " |", "|---" * (len(per_key) + 1) + "|"]
    for key in keys:
        cells = [_pct(r[key], t[key]) + f" ({t[key] // max(1, results['repeats']) if name in configs else t[key]})"
                 for name, (r, t) in per_key.items()]
        lines.append(f"| {key} | " + " | ".join(cells) + " |")
    lines += ["", "(n) is the number of scenarios scoring that key.", ""]

    lines += ["## Agreement with Claude's own tags", "",
              "How often the judge's usual answer (the most common of its runs) matches the tag Claude wrote in "
              "the session, key by key over all scenarios, left-out keys included.", "",
              "| Judge | Agrees with Claude |", "|---|---|"]
    for config in configs:
        agree = count = 0
        for row in rows.values():
            tags = [words(r["tag"]) for r in row["runs"][config] if r and "error" not in r]
            if not tags:
                continue
            own = words(row["claude"])
            for key in KEYS:
                count += 1
                agree += _majority([t.get(key) for t in tags]) == own.get(key)
        lines.append(f"| {config} | {_pct(agree, count)} |")
    lines.append("")

    lines += ["## Misses", "", "Each scored key a judge got wrong in most of its runs: what it said, and what was right.", ""]
    for config in [*configs, "claude"]:
        misses = []
        for sid, row in rows.items():
            expect = expected_for(scenarios[sid], row)
            tags = [row["claude"]] if config == "claude" else [r["tag"] for r in row["runs"][config] if r and "error" not in r]
            for key, options in expect.items():
                answers = [words(t).get(key) for t in tags]
                wrong = [a for a in answers if not accepts(options, a)]
                if answers and len(wrong) * 2 > len(answers):
                    shown = "/".join(str(o) if o is not None else "(left out)" for o in options)
                    misses.append(f"{sid}.{key}: said {_majority(wrong) or '(left out)'}, right is {shown}")
        lines.append(f"**{config}** ({len(misses)}): " + ("; ".join(misses) if misses else "none"))
        lines.append("")
    return "\n".join(lines)


def cmd_score(args) -> int:
    path = Path(args.results) if args.results else max(RESULTS.glob("*.json"), default=None)
    if path is None:
        print("no results: run 'judge' first")
        return 2
    results = json.loads(path.read_text(encoding="utf-8"))
    report = score(results, {s["id"]: s for s in _scenarios()})
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
    print(report)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record", help="record the scenario sessions (spends Claude tokens)")
    record.add_argument("--only", nargs="*", default=())
    record.add_argument("--model", default="sonnet")
    record.add_argument("--jobs", type=int, default=4)
    judge = sub.add_parser("judge", help="ask each judge for each scenario's tag (spends a little)")
    judge.add_argument("--configs", default=",".join(DEFAULT_CONFIGS))
    judge.add_argument("--repeats", type=int, default=3)
    judge.add_argument("--jobs", type=int, default=6)
    judge.add_argument("--only", nargs="*", default=())
    judge.add_argument("--hook", help="judge with this copy of capture-hook.py (its catalogue beside it)")
    scored = sub.add_parser("score", help="the report (spends nothing)")
    scored.add_argument("results", nargs="?")
    scored.add_argument("--out")
    args = parser.parse_args(argv)
    return {"record": cmd_record, "judge": cmd_judge, "score": cmd_score}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
