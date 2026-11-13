"""Code explainer: stream an explanation of a source file passed on the command line.

    python python/code-explainer/main.py path/to/file.py
    python python/code-explainer/main.py server.go --question "How are timeouts handled?"

The file is sent whole. If it does not fit the model's context the gateway
answers 413 `context_length_exceeded` with `estimated_tokens`, `context_cap`
and `bound_by` (plan or server), which this example prints. For files that
are too long, see ../document-summarizer for chunking.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import openai
from openai import OpenAI

DEFAULT_BASE_URL = "https://api.molochai.dev/api/v1"
DEFAULT_MODEL = "glm-5.3-flash"
CAP_NOTICE = "\n\n[Response is too large for your current plan. Try a smaller request.]"
MAX_FILE_BYTES = 400_000  # a local sanity limit; the server enforces the real one

LANGUAGES = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".sh": "bash", ".sql": "sql", ".swift": "swift", ".lua": "lua",
}  # fmt: skip

SYSTEM_PROMPT = (
    "You explain source code to experienced engineers who are new to this file. Start with "
    "a two-sentence summary of what the file does. Then walk through the main parts in the "
    "order they run, naming functions and types as they appear in the code. End with any "
    "bugs, risks or unclear spots you notice. Be specific; do not restate the code line by line."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Explain a source file, streaming the answer.")
    parser.add_argument("file", type=Path, help="source file to explain")
    parser.add_argument("--question", help="a specific question about the file")
    parser.add_argument(
        "--model",
        default=os.environ.get("MOLOCHAI_MODEL") or DEFAULT_MODEL,
        help=f"model id (default: $MOLOCHAI_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument("--show-reasoning", action="store_true", help="print reasoning to stderr")
    args = parser.parse_args()

    try:
        size = args.file.stat().st_size
        if size > MAX_FILE_BYTES:
            return fail(f"{args.file} is {size} bytes; this example stops at {MAX_FILE_BYTES}")
        source = args.file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return fail(f"{args.file} is not UTF-8 text")
    except OSError as err:
        return fail(f"could not read {args.file}: {err}")

    language = LANGUAGES.get(args.file.suffix.lower(), "")
    question = args.question or "Explain this file."
    user_message = f"File: {args.file.name}\n\n```{language}\n{source}\n```\n\n{question}"

    client = make_client(timeout=60.0, max_retries=2)
    held = ""  # the cap notice can only come last, so hold back that many characters
    try:
        stream = client.chat.completions.create(
            model=args.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning and args.show_reasoning:
                sys.stderr.write(reasoning)
                sys.stderr.flush()
            if delta.content:
                held += delta.content
                ready, held = held[: -len(CAP_NOTICE)], held[-len(CAP_NOTICE) :]
                sys.stdout.write(ready)
                sys.stdout.flush()
    except openai.OpenAIError as err:
        print(held, flush=True)  # whatever arrived before an in-stream error
        return fail(describe_error(err))

    if held.endswith(CAP_NOTICE):
        held = held.removesuffix(CAP_NOTICE)
        print("[note] the explanation stopped at the plan's output cap", file=sys.stderr)
    print(held, flush=True)
    return 0


# ---------------------------------------------------------------------------
# Boilerplate shared (as a copy) by every Python example in this repository, so
# that each example stays a single file you can copy out.
# ---------------------------------------------------------------------------


def make_client(**options: Any) -> OpenAI:
    """Build an OpenAI SDK client from MOLOCHAI_API_KEY and MOLOCHAI_BASE_URL."""
    api_key = os.environ.get("MOLOCHAI_API_KEY", "").strip()
    if not api_key:
        sys.exit(
            "error: MOLOCHAI_API_KEY is not set. Create a key at "
            "https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples"
            ", or run against the mock server (see local-development/README.md)."
        )
    base_url = os.environ.get("MOLOCHAI_BASE_URL", "").strip() or DEFAULT_BASE_URL
    return OpenAI(api_key=api_key, base_url=base_url, **options)


# What to tell the user, keyed by error `type`/`code` or by HTTP status.
HINTS = {
    "401": "check MOLOCHAI_API_KEY (missing, mistyped or revoked)",
    "no_active_plan": "plans and credit: "
    "https://molochai.dev/pricing?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples",
    "email_unverified": "confirm the account's email address, then retry",
    "context_length_exceeded": "the prompt does not fit this model's context; shorten it",
    "422": "the request was rejected as invalid; check the model id against GET /models",
    "429": "over 60 requests per minute; wait for the Retry-After header",
    "quota_exceeded": "quota used up; retrying before resets_at will not help",
    "session_quota_exceeded": "5-hour session cap reached; retrying before resets_at won't help",
    "model_unavailable": "no server is serving this model right now; try another with --model",
    "server_busy": "no free slot right now; try again shortly",
    "524": "the edge timed out a long non-streamed reply; use streaming",
}
DETAIL_FIELDS = ("resets_at", "estimated_tokens", "context_cap", "bound_by", "model", "status")


def describe_error(err: openai.OpenAIError) -> str:
    """Turn any SDK error into a readable message. Reads fields, never message text."""
    if isinstance(err, openai.APIStatusError):
        status = err.status_code
        # The SDK stores the inner "error" object for gateway errors, the whole
        # body for validation (422) and bare-message errors, or raw text.
        body = err.body
        if not isinstance(body, dict):
            hint = HINTS.get(str(status))
            return f"HTTP {status} with a non-JSON body" + (f"\n  hint: {hint}" if hint else "")
        kind = body.get("type") or body.get("code")
        text = f"HTTP {status}" + (f" {kind}" if kind else "")
        if body.get("message"):
            text += f": {body['message']}"
        details = [f"{key}={body[key]}" for key in DETAIL_FIELDS if key in body]
        if isinstance(body.get("errors"), dict):
            details += [f"{key}: {'; '.join(map(str, v))}" for key, v in body["errors"].items()]
        if details:
            text += f" ({', '.join(details)})"
        hint = HINTS.get(str(kind)) or HINTS.get(str(status))
        return text + (f"\n  hint: {hint}" if hint else "")
    if isinstance(err, openai.APIConnectionError):
        return f"could not reach the API ({err.message}); check MOLOCHAI_BASE_URL and the network"
    if isinstance(err, openai.APIError):  # an error event inside a stream
        kind = err.body.get("type") if isinstance(err.body, dict) else None
        return f"stream error {kind or ''}: {err.message}"
    return str(err)


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
