"""Run any command against the mock server, or with variables from a .env file.

    python local-development/devenv.py --mock -- python python/quickstart/main.py "hi"
    python local-development/devenv.py --mock --mock-args="--unavailable glm-5.3-flash" -- \
        python python/model-selection/main.py
    python local-development/devenv.py --env-file .env -- python python/streaming/main.py "hi"

--mock starts the MolochAI mock server (a test double with canned replies, from
https://github.com/Moloch-AI/uncensored-ai-api) on a free local port, points
MOLOCHAI_BASE_URL at it, sets MOLOCHAI_API_KEY=mock-key, runs the command and
stops the server. Nothing reaches the real API and no quota is used.

--env-file loads KEY=VALUE lines (blank lines and # comments allowed, optional
quotes around the value, no inline comments or interpolation). Variables that
are already set in the environment win; --mock values win over both.

The mock server script is found through MOLOCHAI_MOCK_SERVER, default
../uncensored-ai-api/mock-server/server.mjs; relative paths are resolved from the
repository root. It needs Node.js 20 or newer. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MOCK_SERVER = "../uncensored-ai-api/mock-server/server.mjs"
MOCK_KEY = "mock-key"
# Talk to the local mock directly, even when HTTP(S)_PROXY is set.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def mock_server_path() -> Path:
    raw = os.environ.get("MOLOCHAI_MOCK_SERVER") or DEFAULT_MOCK_SERVER
    path = Path(raw)
    return (path if path.is_absolute() else REPO_ROOT / path).resolve()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockServer:
    """Start the mock on a free port; use as a context manager."""

    def __init__(self, extra_args: Sequence[str] = (), script: Path | None = None) -> None:
        self.script = script or mock_server_path()
        self.extra_args = list(extra_args)
        self.port = 0
        self._process: subprocess.Popen[bytes] | None = None
        # The mock's stderr, read only if it fails to start. Closed in stop().
        self._log = tempfile.TemporaryFile()  # noqa: SIM115

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    def env(self) -> dict[str, str]:
        return {"MOLOCHAI_BASE_URL": self.base_url, "MOLOCHAI_API_KEY": MOCK_KEY}

    def __enter__(self) -> MockServer:
        if not self.script.is_file():
            raise RuntimeError(
                f"mock server not found at {self.script}. Clone "
                "https://github.com/Moloch-AI/uncensored-ai-api next to this repository "
                "or set MOLOCHAI_MOCK_SERVER to its mock-server/server.mjs."
            )
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("node is not on PATH; the mock server needs Node.js 20 or newer")
        self.port = free_port()
        command = [node, str(self.script), "--port", str(self.port), "--quiet", *self.extra_args]
        self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=self._log)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._log.seek(0)
                output = self._log.read().decode("utf-8", "replace").strip()
                raise RuntimeError(
                    f"mock server exited with code {self._process.returncode}: {output}"
                )
            try:
                with DIRECT.open(f"http://127.0.0.1:{self.port}/health", timeout=1):
                    return self
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("mock server did not answer GET /health within 15 s")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._log.close()


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.isidentifier():
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def run(command: Sequence[str], extra_env: Mapping[str, str]) -> int:
    executable = shutil.which(command[0]) or command[0]
    return subprocess.call([executable, *command[1:]], env={**os.environ, **extra_env})


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a command against the mock server or with a .env file.",
        usage="%(prog)s [--mock [--mock-args ARGS]] [--env-file FILE] -- COMMAND [ARG ...]",
    )
    parser.add_argument("--mock", action="store_true", help="start the mock server for the command")
    parser.add_argument("--mock-args", default="", help='extra mock flags, e.g. "--delay-ms 0"')
    parser.add_argument("--env-file", type=Path, help="load KEY=VALUE pairs from this file")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the command, after --")
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given (put it after --)")

    env: dict[str, str] = {}
    if args.env_file:
        try:
            loaded = read_env_file(args.env_file)
        except (OSError, ValueError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        env.update({k: v for k, v in loaded.items() if k not in os.environ})

    if not args.mock:
        return run(command, env)
    try:
        with MockServer(shlex.split(args.mock_args)) as mock:
            print(f"[devenv] mock server at {mock.base_url}", file=sys.stderr)
            return run(command, {**env, **mock.env()})
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
