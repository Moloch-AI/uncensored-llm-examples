"""Streaming: print a reply as it is generated, with the MolochAI stream details handled.

    python python/streaming/main.py "Write a short explanation of TCP slow start."

- answer text goes to stdout as it arrives; reasoning goes to stderr
- usage is read from the final chunk, which has an empty `choices` list
- an error event inside the stream (HTTP status already 200) is reported, exit 1
- the cap notice the gateway appends at the output cap is removed before printing
- finish_reason "length" is reported on stderr
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import openai
from openai import OpenAI

DEFAULT_BASE_URL = "https://api.molochai.dev/api/v1"
DEFAULT_MODEL = "glm-5.3-flash"

# Appended by the gateway to `content` when a reply stops at the plan's output
# cap without a tool call. It is for people reading a chat, not for programs.
CAP_NOTICE = "\n\n[Response is too large for your current plan. Try a smaller request.]"


class CapNoticeFilter:
    """Pass text through, but hold back any tail that could be the start of CAP_NOTICE.

    The notice arrives as ordinary content deltas and can be split across chunks,
    so a plain `str.replace` on each chunk would miss it.
    """

    def __init__(self) -> None:
        self._held = ""
        self.found = False

    def feed(self, text: str) -> str:
        self._held += text
        keep = 0
        for size in range(min(len(self._held), len(CAP_NOTICE)), 0, -1):
            if CAP_NOTICE.startswith(self._held[-size:]):
                keep = size
                break
        split = len(self._held) - keep
        ready, self._held = self._held[:split], self._held[split:]
        return ready

    def finish(self) -> str:
        """Call once the stream has ended. Returns any held text that was not the notice."""
        held, self._held = self._held, ""
        if held == CAP_NOTICE:
            self.found = True
            return ""
        return held


def main() -> int:
    parser = argparse.ArgumentParser(description="Stream a chat completion to the terminal.")
    parser.add_argument(
        "prompt", nargs="?", default="Explain TCP slow start to a new engineer in one paragraph."
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("MOLOCHAI_MODEL") or DEFAULT_MODEL,
        help=f"model id (default: $MOLOCHAI_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument("--hide-reasoning", action="store_true", help="do not print reasoning")
    args = parser.parse_args()

    # While streaming, the read timeout is the longest silence allowed between
    # bytes. The gateway sends keep-alive comments about once a second, so a
    # stalled connection shows up well inside 60 s.
    client = make_client(timeout=60.0, max_retries=2)

    try:
        stream = client.chat.completions.create(
            model=args.model,
            messages=[{"role": "user", "content": args.prompt}],
            stream=True,
            # MolochAI always sends usage at the end of a stream and drops this
            # parameter; other OpenAI-compatible servers need it to send usage.
            stream_options={"include_usage": True},
        )
    except openai.OpenAIError as err:  # status errors happen here, before any chunk
        return fail(describe_error(err))

    cap_filter = CapNoticeFilter()
    finish_reason = None
    usage = None
    in_reasoning = False
    try:
        for chunk in stream:
            if chunk.usage is not None:
                usage = chunk.usage
            if not chunk.choices:  # the usage chunk has `choices: []`
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            reasoning = getattr(delta, "reasoning_content", None)  # MolochAI extension
            if reasoning and not args.hide_reasoning:
                if not in_reasoning:
                    sys.stderr.write("[reasoning] ")
                    in_reasoning = True
                sys.stderr.write(reasoning)
                sys.stderr.flush()
            if delta.content:
                if in_reasoning:
                    sys.stderr.write("\n[/reasoning]\n")
                    in_reasoning = False
                sys.stdout.write(cap_filter.feed(delta.content))
                sys.stdout.flush()
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    except openai.APIError as err:
        # The HTTP status was already 200, so an upstream failure arrives as a
        # data event: {"error": {"type": "upstream_timeout", ...}}. The SDK
        # raises it as APIError with `body` set to that inner object.
        sys.stdout.write(cap_filter.finish())
        print(flush=True)
        kind = err.body.get("type") if isinstance(err.body, dict) else None
        advice = (
            "the server was reclaimed mid-reply; retry in about a minute"
            if kind == "preempted"
            else "retry the request, with backoff"
        )
        return fail(f"{describe_error(err)}\n  the reply above is incomplete; {advice}")

    sys.stdout.write(cap_filter.finish())
    print(flush=True)
    if in_reasoning:
        sys.stderr.write("\n[/reasoning]\n")

    if finish_reason == "length" or cap_filter.found:
        print(
            "[note] the reply stopped at the plan's output cap (cap notice removed); "
            "max_tokens has no effect on MolochAI, so ask for less or split the task",
            file=sys.stderr,
        )
    if usage is not None:
        print(
            f"[usage] prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
            f"total={usage.total_tokens}",
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
