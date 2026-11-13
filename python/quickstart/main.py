"""Quickstart: one chat completion against the MolochAI API with the official OpenAI SDK.

    python python/quickstart/main.py "In two sentences, what is a context window?"

Prints the answer to stdout. Reasoning (when the model returns any) and token
usage go to stderr, so `> answer.txt` captures only the answer.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any

import openai
from openai import OpenAI

DEFAULT_BASE_URL = "https://api.molochai.dev/api/v1"
DEFAULT_MODEL = "glm-5.3-flash"

# Some serving servers leave reasoning inline as <think>...</think> at the start
# of `content`. The gateway strips those only while streaming, so a plain
# (non-streamed) reply can still contain one.
INLINE_THINK = re.compile(r"^\s*<think>(.*?)</think>\s*", re.DOTALL)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one chat completion and print the reply.")
    parser.add_argument("prompt", nargs="?", default="In two sentences, what is a context window?")
    parser.add_argument(
        "--model",
        default=os.environ.get("MOLOCHAI_MODEL") or DEFAULT_MODEL,
        help=f"model id (default: $MOLOCHAI_MODEL or {DEFAULT_MODEL})",
    )
    args = parser.parse_args()

    # A plain request has to finish before the edge gives up (about 100 s), so
    # keep the timeout near that. Anything that can run long should stream:
    # see ../streaming. The SDK retries connection errors, 429 and 5xx twice by
    # default; that includes quota errors that cannot pass until `resets_at`.
    client = make_client(timeout=120.0, max_retries=2)

    try:
        completion = client.chat.completions.create(
            model=args.model,
            messages=[{"role": "user", "content": args.prompt}],
        )
    except openai.OpenAIError as err:
        return fail(describe_error(err))

    choice = completion.choices[0]
    content = choice.message.content or ""
    # `reasoning_content` is a MolochAI extension, not a typed SDK field. The SDK
    # keeps unknown fields, so getattr reads it when present.
    reasoning = getattr(choice.message, "reasoning_content", None)
    if not reasoning and (match := INLINE_THINK.match(content)):
        reasoning, content = match.group(1).strip(), content[match.end() :]

    if reasoning:
        print(f"[reasoning]\n{reasoning.strip()}\n[/reasoning]", file=sys.stderr)
    print(content.strip())

    if choice.finish_reason == "length":
        print(
            "[note] the reply stopped at the plan's output cap; max_tokens has no effect "
            "on MolochAI, so ask for a shorter answer or split the task",
            file=sys.stderr,
        )
    if completion.usage:
        u = completion.usage
        print(
            f"[usage] model={completion.model} prompt={u.prompt_tokens} "
            f"completion={u.completion_tokens} total={u.total_tokens}",
            file=sys.stderr,
        )
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
