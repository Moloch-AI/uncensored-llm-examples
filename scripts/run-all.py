"""Smoke-test every example against the MolochAI mock server.

    python scripts/run-all.py              # run everything
    python scripts/run-all.py -k streaming # only cases whose name contains "streaming"
    python scripts/run-all.py --list       # show the cases
    python scripts/run-all.py -v           # print each case's output

Starts the mock server (MOLOCHAI_MOCK_SERVER, default
../uncensored-ai-api/mock-server/server.mjs, relative to the repository root),
runs each example with sample inputs and fails if a case exits with a code other
than the expected one, prints nothing (stdout for successes, stderr for expected
failures), or misses an expected piece of output. Cases that exercise error
handling expect a non-zero exit and a specific message.

Needs: Python dependencies from python/requirements.txt in the running
interpreter, `npm ci` done in javascript/ and typescript/, Node.js 22+.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "local-development"))
from devenv import MockServer, mock_server_path  # noqa: E402

PY = sys.executable
TIMEOUT_S = 120


@dataclass
class Case:
    name: str
    command: list[str]
    cwd: str = "."
    stdin: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    mock_args: tuple[str, ...] = ()
    use_mock: bool = True
    exit_code: int = 0
    stdout_has: tuple[str, ...] = ()
    stdout_lacks: tuple[str, ...] = ()
    stderr_has: tuple[str, ...] = ()


@dataclass
class Result:
    case: Case
    ok: bool
    seconds: float
    problems: list[str]
    stdout: str
    stderr: str


def cases(fixtures: Path, node: str) -> list[Case]:
    tsx = [node, "--import", "tsx"]
    # fmt: off
    return [
        # --- python/quickstart
        Case("python/quickstart", [PY, "python/quickstart/main.py", "Say hello"],
             stdout_has=("Mock reply from glm-5.3-flash: Say hello",),
             stderr_has=("[reasoning]", "[usage]")),
        Case("python/quickstart --model (no reasoning)",
             [PY, "python/quickstart/main.py", "--model", "llama-3.3-70b-instruct-abliterated", "hi"],
             stdout_has=("Mock reply from llama-3.3-70b-instruct-abliterated",)),
        Case("python/quickstart inline <think>",
             [PY, "python/quickstart/main.py", "[mock:inline-think] hi"],
             stdout_lacks=("<think>",), stderr_has=("Mock inline reasoning.",)),
        Case("python/quickstart 402 no plan", [PY, "python/quickstart/main.py", "hi"],
             env={"MOLOCHAI_API_KEY": "mock-no-plan"}, exit_code=1,
             stderr_has=("HTTP 402 no_active_plan", "molochai.dev/pricing")),
        Case("python/quickstart 422 unknown model",
             [PY, "python/quickstart/main.py", "--model", "not-a-model", "hi"],
             exit_code=1, stderr_has=("HTTP 422",)),
        Case("python/quickstart 413 context", [PY, "python/quickstart/main.py", "[mock:context] x"],
             exit_code=1, stderr_has=("context_length_exceeded", "bound_by=plan")),
        Case("python/quickstart 524 non-JSON", [PY, "python/quickstart/main.py", "[mock:cloudflare-524]"],
             exit_code=1, stderr_has=("HTTP 524 with a non-JSON body",)),
        Case("python/quickstart missing key", [PY, "python/quickstart/main.py", "hi"],
             env={"MOLOCHAI_API_KEY": ""}, exit_code=1,
             stderr_has=("MOLOCHAI_API_KEY is not set",)),
        # --- python/streaming
        Case("python/streaming", [PY, "python/streaming/main.py", "Stream this please"],
             stdout_has=("Mock reply from glm-5.3-flash: Stream this please",),
             stderr_has=("[reasoning] Mock reasoning", "[usage] prompt=")),
        Case("python/streaming cap notice", [PY, "python/streaming/main.py", "[mock:cap] long"],
             stdout_has=("Mock reply",), stdout_lacks=("[Response is too large",),
             stderr_has=("output cap",)),
        Case("python/streaming in-stream timeout",
             [PY, "python/streaming/main.py", "[mock:timeout] go"],
             exit_code=1, stderr_has=("stream error upstream_timeout", "incomplete")),
        Case("python/streaming preempted", [PY, "python/streaming/main.py", "[mock:preempted] go"],
             exit_code=1, stderr_has=("stream error preempted", "about a minute")),
        Case("python/streaming queue", [PY, "python/streaming/main.py", "[mock:queue] go"],
             stdout_has=("Mock reply",)),
        # --- python/model-selection
        Case("python/model-selection", [PY, "python/model-selection/main.py"],
             stdout_has=("Mock reply",), stderr_has=("[answered by] glm-5.3-flash", "available")),
        Case("python/model-selection skips unavailable", [PY, "python/model-selection/main.py"],
             mock_args=("--unavailable", "glm-5.3-flash"),
             stdout_has=("Mock reply from glm-4.7-flash-uncensored",),
             stderr_has=("[skip] glm-5.3-flash: listed as not available",)),
        Case("python/model-selection 503 on every model",
             [PY, "python/model-selection/main.py", "[mock:unavailable] x"], exit_code=1,
             stderr_has=("[fallback] glm-5.3-flash: 503", "no candidate model")),
        # --- python/json-output
        Case("python/json-output", [PY, "python/json-output/main.py"],
             stdout_has=('"sentiment": "positive"', '"rating": 1')),
        Case("python/json-output repair then give up",
             [PY, "python/json-output/main.py", "--schema", str(fixtures / "strict-schema.json")],
             exit_code=1, stderr_has=("[attempt 2]", "after one repair attempt")),
        # --- python/tool-calling
        Case("python/tool-calling", [PY, "python/tool-calling/main.py"],
             stdout_has=("after tool convert_units",),
             stderr_has=("[step 1] convert_units(", "[done] 2 model call(s)")),
        Case("python/tool-calling max steps", [PY, "python/tool-calling/main.py", "--max-steps", "1"],
             exit_code=1, stderr_has=("no final answer after 1 model calls",)),
        # --- python/document-summarizer
        Case("python/document-summarizer",
             [PY, "python/document-summarizer/main.py", "python/document-summarizer/sample.md"],
             stdout_has=("Mock reply",),
             stderr_has=("context limit for glm-5.3-flash: 65536 tokens",)),
        Case("python/document-summarizer map-reduce",
             [PY, "python/document-summarizer/main.py", "python/document-summarizer/sample.md",
              "--chunk-tokens", "300"],
             stdout_has=("Mock reply",), stderr_has=("map 1/", "reduce (final)")),
        # --- python/code-explainer
        Case("python/code-explainer",
             [PY, "python/code-explainer/main.py", "python/code-explainer/sample_backoff.py"],
             stdout_has=("File: sample_backoff.py",)),
        Case("python/code-explainer missing file",
             [PY, "python/code-explainer/main.py", "does-not-exist.py"], exit_code=1,
             stderr_has=("could not read",)),
        # --- javascript/streaming-fetch
        Case("javascript/streaming-fetch", [node, "streaming-fetch/main.mjs", "Fetch this"],
             cwd="javascript", stdout_has=("Mock reply from glm-5.3-flash: Fetch this",),
             stderr_has=("[usage]",)),
        Case("javascript/streaming-fetch queue notices",
             [node, "streaming-fetch/main.mjs", "[mock:queue] go"], cwd="javascript",
             stdout_has=("Mock reply",), stderr_has=("[queue] queued",)),
        Case("javascript/streaming-fetch cap notice",
             [node, "streaming-fetch/main.mjs", "[mock:cap] go"], cwd="javascript",
             stdout_has=("Mock reply",), stdout_lacks=("[Response is too large",),
             stderr_has=("output cap",)),
        Case("javascript/streaming-fetch in-stream error",
             [node, "streaming-fetch/main.mjs", "[mock:timeout] go"], cwd="javascript",
             exit_code=1, stderr_has=("stream error upstream_timeout",)),
        Case("javascript/streaming-fetch 403", [node, "streaming-fetch/main.mjs", "hi"],
             cwd="javascript", env={"MOLOCHAI_API_KEY": "mock-unverified"}, exit_code=1,
             stderr_has=("HTTP 403 email_unverified",)),
        # --- javascript/text-processing
        Case("javascript/text-processing rewrite",
             [node, "text-processing/main.mjs", "text-processing/sample.txt"], cwd="javascript",
             stdout_has=('"line":8', '"output":'), stderr_has=("done: 8 ok, 0 failed",)),
        Case("javascript/text-processing labels",
             [node, "text-processing/main.mjs", "text-processing/sample.txt",
              "--labels", "bug,feature,question,praise"], cwd="javascript",
             stdout_has=('"label":',), stderr_has=("done: 8 ok",)),
        Case("javascript/text-processing Retry-After",
             [node, "text-processing/main.mjs", str(fixtures / "rate-limited.txt")], cwd="javascript",
             exit_code=1, stdout_has=('"line":1',),
             stderr_has=("(Retry-After)", "giving up after 4 attempt(s)")),
        Case("javascript/text-processing quota stops the batch",
             [node, "text-processing/main.mjs", str(fixtures / "quota.txt"), "--concurrency", "1"],
             cwd="javascript", exit_code=1,
             stderr_has=("stopped the batch", "quota_exceeded", "2 not processed")),
        Case("javascript/text-processing backoff on 502",
             [node, "text-processing/main.mjs", str(fixtures / "flaky.txt")], cwd="javascript",
             stdout_has=('"line":2',), stderr_has=("upstream_unreachable; retrying",)),
        # --- typescript/terminal-chat
        Case("typescript/terminal-chat", [*tsx, "terminal-chat/main.ts"], cwd="typescript",
             stdin="Hello there\n/model\n/model llama-3.3-70b-instruct-abliterated\n"
             "Second question\n/reset\n[mock:cap] capped\n/exit\n",
             stdout_has=("Mock reply from glm-5.3-flash: Hello there",
                         "Mock reply from llama-3.3-70b-instruct-abliterated: Second question"),
             stdout_lacks=("[Response is too large",),
             stderr_has=("conversation cleared", "output cap")),
        Case("typescript/terminal-chat failed turn", [*tsx, "terminal-chat/main.ts"],
             cwd="typescript", stdin="[mock:busy] hi\nafter\n", exit_code=1,
             stdout_has=("Mock reply from glm-5.3-flash: after",), stderr_has=("server_busy",)),
        Case("typescript/terminal-chat 402 ends the session", [*tsx, "terminal-chat/main.ts"],
             cwd="typescript", stdin="hi\nagain\n", env={"MOLOCHAI_API_KEY": "mock-no-plan"},
             exit_code=1, stderr_has=("HTTP 402 no_active_plan",)),
        # --- typescript/structured-extraction
        Case("typescript/structured-extraction",
             [*tsx, "structured-extraction/main.ts", "structured-extraction/sample-notes.txt"],
             cwd="typescript", stdout_has=('"action_items"',), stderr_has=("[extracted]",)),
        # --- local-development
        Case("local-development/devenv.py --mock",
             [PY, "local-development/devenv.py", "--mock", "--", PY, "python/quickstart/main.py",
              "via devenv"],
             use_mock=False, stdout_has=("Mock reply from glm-5.3-flash: via devenv",)),
        Case("local-development/check_secrets.py flags a key",
             [PY, "local-development/check_secrets.py", str(fixtures / "leaked.txt")],
             use_mock=False, exit_code=1, stderr_has=("literal value",)),
    ]
    # fmt: on


def write_fixtures(directory: Path) -> None:
    strict = {
        "type": "object",
        "properties": {"summary": {"type": "string", "minLength": 20}},
        "required": ["summary"],
    }
    (directory / "strict-schema.json").write_text(json.dumps(strict), encoding="utf-8")
    (directory / "rate-limited.txt").write_text(
        "first line\n[mock:rate-limit] the mock rate-limits this line every time\n",
        encoding="utf-8",
    )
    (directory / "quota.txt").write_text(
        "first line\n[mock:quota] quota is used up\nthird line\n", encoding="utf-8"
    )
    (directory / "flaky.txt").write_text(
        f"first line\n[mock:flaky=2] fails twice, run {time.time_ns()}\n", encoding="utf-8"
    )
    # Built at runtime so this file itself does not contain a literal key.
    leaked = "=".join(["MOLOCHAI_API_KEY", "a1b2c3d4" * 4])
    (directory / "leaked.txt").write_text(leaked + "\n", encoding="utf-8")


def base_env() -> dict[str, str]:
    """The parent environment without any real API configuration."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MOLOCHAI_", "OPENAI_"))}
    env["MOLOCHAI_MOCK_SERVER"] = str(mock_server_path())
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    return env


def run_case(index: int, case: Case, mocks: dict[tuple[str, ...], MockServer]) -> Result:
    env = base_env()
    if case.use_mock:
        env.update(mocks[case.mock_args].env())
        # The mock rate-limits per token; a token per case keeps parallel cases apart.
        env["MOLOCHAI_API_KEY"] = f"mock-key-{index}"
    env.update(case.env)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            case.command,
            cwd=ROOT / case.cwd,
            env=env,
            input=case.stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Result(case, False, TIMEOUT_S, [f"timed out after {TIMEOUT_S} s"], "", "")
    seconds = time.monotonic() - started
    out, err = completed.stdout, completed.stderr

    problems = []
    if completed.returncode != case.exit_code:
        problems.append(f"exit code {completed.returncode}, expected {case.exit_code}")
    if case.exit_code == 0 and not out.strip():
        problems.append("printed nothing on stdout")
    if case.exit_code != 0 and not err.strip():
        problems.append("failed without a message on stderr")
    problems += [f"stdout lacks {s!r}" for s in case.stdout_has if s not in out]
    problems += [f"stdout contains {s!r}" for s in case.stdout_lacks if s in out]
    problems += [f"stderr lacks {s!r}" for s in case.stderr_has if s not in err]
    return Result(case, not problems, seconds, problems, out, err)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test every example against the mock.")
    parser.add_argument("-k", dest="filter", default="", help="only cases whose name contains this")
    parser.add_argument(
        "-j", "--jobs", type=int, default=4, help="cases run in parallel (default 4)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print every case's output")
    parser.add_argument("--list", action="store_true", help="list the cases and exit")
    args = parser.parse_args()

    node = shutil.which("node")
    if node is None:
        print("error: node is not on PATH", file=sys.stderr)
        return 2
    for folder in ("javascript", "typescript"):
        if not (ROOT / folder / "node_modules").is_dir():
            print(f"error: run `npm ci` in {folder}/ first", file=sys.stderr)
            return 2

    with tempfile.TemporaryDirectory() as tmp:
        fixtures = Path(tmp)
        write_fixtures(fixtures)
        selected = [c for c in cases(fixtures, node) if args.filter in c.name]
        if args.list:
            print("\n".join(c.name for c in selected))
            return 0
        if not selected:
            print(f"error: no case matches {args.filter!r}", file=sys.stderr)
            return 2

        # One mock per distinct set of mock flags, shared by the cases that use it.
        mocks: dict[tuple[str, ...], MockServer] = {}
        try:
            with ExitStack() as stack:
                for mock_args in dict.fromkeys(c.mock_args for c in selected if c.use_mock):
                    mock = stack.enter_context(MockServer(mock_args))
                    mocks[mock_args] = mock
                    print(f"mock server {' '.join(mock_args) or '(default)'} at {mock.base_url}")
                with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
                    results = list(pool.map(lambda ic: run_case(*ic, mocks), enumerate(selected)))
        except RuntimeError as err:
            print(f"error: {err}", file=sys.stderr)
            return 2

    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"{status}  {result.case.name}  ({result.seconds:.1f} s)")
        for problem in result.problems:
            print(f"      - {problem}")
        if args.verbose or not result.ok:
            for label, text in (("stdout", result.stdout), ("stderr", result.stderr)):
                if text.strip():
                    print(f"      {label}:")
                    print("\n".join(f"        {line}" for line in text.strip().splitlines()[-15:]))
    failed = sum(not r.ok for r in results)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
