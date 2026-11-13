"""Refuse to commit .env files or files that look like they contain an API key.

    python local-development/check_secrets.py              # staged files (use as a pre-commit hook)
    python local-development/check_secrets.py --all        # every tracked file (CI runs this)
    python local-development/check_secrets.py FILE [...]   # the given files

What it flags:
- any file named .env or .env.<something>, except .env.example
- MOLOCHAI_API_KEY / OPENAI_API_KEY assigned a literal value of 12 or more
  characters that is not a known placeholder (your-api-key, mock-key, ...)
- "Authorization: Bearer <literal token>" with a long literal token

It is a guard against the common mistake, not a full scanner: it does not know
every key format. Pair it with a dedicated tool such as gitleaks if you need
more. Put "secret-check: allow" on a line to skip it deliberately.
Standard library only; exits 1 when something is found.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

PLACEHOLDERS = {"your-api-key", "mock-key", "mock-no-plan", "mock-unverified"}
MIN_KEY_LENGTH = 12  # shorter values are names (MOCK_KEY) or test strings, not keys
KEY_ASSIGNMENT = re.compile(
    r"\b(?:MOLOCHAI|OPENAI)_API_KEY\b[\"']?\s*[:=]\s*[\"']?(?P<value>[^\s\"'#,;)]*)"
)
BEARER = re.compile(r"Bearer\s+(?P<value>[A-Za-z0-9._~+/=-]{20,})")
ALLOW_MARKER = "secret-check: allow"


def is_reference(value: str) -> bool:
    """Values like $VAR, ${{ secrets.X }}, <your key> or {key} are not literals."""
    return value.startswith(("$", "<", "{", "%", "process.env", "os.environ"))


def env_file(path: str) -> bool:
    name = Path(path).name
    return (name == ".env" or name.startswith(".env.")) and name != ".env.example"


def findings(path: str, text: str) -> Iterator[str]:
    if env_file(path):
        yield f"{path}: .env files hold real keys and must not be committed"
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for match in KEY_ASSIGNMENT.finditer(line):
            value = match.group("value")
            if (
                len(value) >= MIN_KEY_LENGTH
                and value not in PLACEHOLDERS
                and not is_reference(value)
            ):
                yield f"{path}:{number}: API key variable set to a literal value"
        for match in BEARER.finditer(line):
            if not is_reference(match.group("value")):
                yield f"{path}:{number}: literal bearer token"


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="Check files for committed secrets.")
    parser.add_argument("files", nargs="*", help="files to check (default: staged files)")
    parser.add_argument("--all", action="store_true", help="check every tracked file")
    args = parser.parse_args()

    # (path, content) pairs; staged files are read from the index, which is
    # what will be committed, not from the working tree.
    items: list[tuple[str, bytes]] = []
    try:
        if args.files:
            items = [(f, Path(f).read_bytes()) for f in args.files]
        elif args.all:
            paths = git("ls-files", "-z").decode().split("\0")
            items = [(p, Path(p).read_bytes()) for p in paths if p and Path(p).is_file()]
        else:
            staged = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
            items = [(p, git("show", f":{p}")) for p in staged.decode().split("\0") if p]
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"check_secrets: {err}", file=sys.stderr)
        return 2

    problems = []
    for path, content in items:
        if b"\0" in content[:8000]:  # binary file
            problems.extend(f for f in findings(path, "") if ".env" in f)
            continue
        problems.extend(findings(path, content.decode("utf-8", "replace")))

    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(
            "check_secrets: possible secrets found; move keys to .env (git ignores it) "
            "or mark a false positive with 'secret-check: allow'",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
