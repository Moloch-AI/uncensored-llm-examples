"""LiteLLM: MolochAI through litellm.completion and acompletion, with streaming and error mapping.

    python python/litellm/main.py "Explain TCP slow start in one paragraph."
    python python/litellm/main.py --no-stream "Name three uses for a hash map."
    python python/litellm/main.py --async "Explain TCP slow start in one paragraph."

The model goes in as "openai/<model id>" together with `api_base`: the prefix
tells LiteLLM to speak the OpenAI chat completions protocol to that base URL.

What it shows (verified against the mock with litellm 1.109.1):
- LiteLLM wraps the OpenAI SDK's errors in its own classes, and some of them
  hide what happened: 413 becomes a generic APIError, an error event inside a
  stream becomes MidStreamFallbackError with status_code 500 although the HTTP
  status was 200, and a refused connection becomes InternalServerError with
  status_code 500. The original OpenAI SDK error, with the real status and
  body, is in the exception chain; `unwrap()` finds it.
- Its retries are turned off (`max_retries=0`, no `num_retries`) and replaced by
  a small policy that reads the status and the error type.

Answers go to stdout; reasoning, usage, retries and errors go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from typing import Any

# LiteLLM downloads its model price list from GitHub when it is imported, unless
# this is set first. MolochAI's model ids are not in that list, so the download
# gains nothing here, and offline (CI) it only adds warnings and retries.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm
import openai

# Without this, LiteLLM prints a "Give Feedback / Get Help" banner to stdout
# every time it raises an exception.
litellm.suppress_debug_info = True

DEFAULT_BASE_URL = "https://api.molochai.dev/api/v1"
DEFAULT_MODEL = "glm-5.3-flash"
CAP_NOTICE = "\n\n[Response is too large for your current plan. Try a smaller request.]"
ATTEMPTS = 3


def request(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.environ.get("MOLOCHAI_API_KEY", "").strip()
    if not api_key:
        sys.exit(
            "error: MOLOCHAI_API_KEY is not set. Create a key at "
            "https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples"
            ", or run against the mock server (see local-development/README.md)."
        )
    kwargs: dict[str, Any] = {
        "model": f"openai/{args.model}",
        "api_base": os.environ.get("MOLOCHAI_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        "api_key": api_key,
        "messages": [{"role": "user", "content": args.prompt}],
        # The OpenAI SDK under LiteLLM retries every 429 and 5xx twice by default,
        # except responses marked `X-Should-Retry: false` (MolochAI's quota errors).
        "max_retries": 0,
        "timeout": 60,
    }
    if args.stream:
        # MolochAI always sends usage in a last chunk with `choices: []` and drops
        # this parameter, but LiteLLM only passes that chunk on when it is set.
        kwargs |= {"stream": True, "stream_options": {"include_usage": True}}
    return kwargs


# --- errors and retries -------------------------------------------------------


def unwrap(err: BaseException) -> BaseException:
    """Return the OpenAI SDK exception inside a LiteLLM exception, or `err` itself."""
    seen: set[int] = set()
    current: BaseException | None = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, openai.APIError) and type(current).__module__.startswith("openai"):
            return current
        nested = getattr(current, "original_exception", None)
        current = nested or current.__cause__ or current.__context__
    return err


def describe(err: BaseException) -> str:
    """'[litellm.Class] <what the API said>', so you can see LiteLLM's mapping."""
    inner = unwrap(err)
    text = f"[{type(err).__module__.split('.')[0]}.{type(err).__name__}] {describe_error(inner)}"
    is_stream_event = isinstance(inner, openai.APIError) and not isinstance(
        inner, (openai.APIStatusError, openai.APIConnectionError)
    )
    if is_stream_event:
        kind = inner.body.get("type") if isinstance(inner.body, dict) else None
        wait = "in about a minute" if kind == "preempted" else "with backoff"
        text += f"\n  the reply above is incomplete; retry {wait}"
    return text


def retry_delay(err: BaseException, attempt: int) -> float | None:
    """Seconds to wait before the next attempt, or None to give up. Reads status and type."""
    if attempt >= ATTEMPTS:
        return None
    inner = unwrap(err)
    backoff = min(2.0**attempt, 10.0) * random.uniform(0.5, 1.0)
    if isinstance(inner, openai.APIConnectionError):  # includes timeouts
        return backoff
    if not isinstance(inner, openai.APIStatusError):
        return None  # an error event inside a stream: output was already printed
    kind = inner.body.get("type") if isinstance(inner.body, dict) else None
    if inner.status_code == 429:
        if isinstance(inner.body, dict) and "resets_at" in inner.body:
            return None  # quota: nothing changes before resets_at
        retry_after = inner.response.headers.get("retry-after", "")
        return min(float(retry_after), 30.0) if retry_after.isdigit() else backoff
    if inner.status_code in (502, 504, 524) or (inner.status_code == 503 and kind == "server_busy"):
        return backoff
    return None  # 401/402/403/413/422 and 503 model_unavailable


def call_with_retries(kwargs: dict[str, Any]) -> Any:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return litellm.completion(**kwargs)
        except openai.OpenAIError as err:
            delay = retry_delay(err, attempt)
            if delay is None:
                raise
            print(f"[retry] {describe(err).splitlines()[0]}; in {delay:.1f} s", file=sys.stderr)
            time.sleep(delay)
    raise AssertionError("unreachable")


async def acall_with_retries(kwargs: dict[str, Any]) -> Any:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return await litellm.acompletion(**kwargs)
        except openai.OpenAIError as err:
            delay = retry_delay(err, attempt)
            if delay is None:
                raise
            print(f"[retry] {describe(err).splitlines()[0]}; in {delay:.1f} s", file=sys.stderr)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


# --- output -------------------------------------------------------------------


class Printer:
    """Answer to stdout, reasoning and usage to stderr, for whole replies or chunks."""

    def __init__(self) -> None:
        self.cap_filter = CapNoticeFilter()
        self.finish_reason: str | None = None
        self.usage: Any = None
        self.in_reasoning = False

    def reply(self, response: Any) -> None:
        choice = response.choices[0]
        # LiteLLM fills `reasoning_content` from the API's field, and also moves a
        # <think>...</think> block at the start of a non-streamed reply into it.
        reasoning = getattr(choice.message, "reasoning_content", None)
        if reasoning:
            print(f"[reasoning] {reasoning.strip()}", file=sys.stderr)
        self.finish_reason, self.usage = choice.finish_reason, response.usage
        content = choice.message.content or ""
        self.cap_filter.found = content.endswith(CAP_NOTICE)
        print(content.removesuffix(CAP_NOTICE).strip())

    def chunk(self, chunk: Any) -> None:
        if getattr(chunk, "usage", None):
            self.usage = chunk.usage
        if not chunk.choices:  # the usage chunk
            return
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            sys.stderr.write(reasoning if self.in_reasoning else f"[reasoning] {reasoning}")
            sys.stderr.flush()
            self.in_reasoning = True
        if delta.content:
            self.end_reasoning()
            sys.stdout.write(self.cap_filter.feed(delta.content))
            sys.stdout.flush()
        self.finish_reason = chunk.choices[0].finish_reason or self.finish_reason

    def end_reasoning(self) -> None:
        if self.in_reasoning:
            sys.stderr.write("\n")
            self.in_reasoning = False

    def done(self, streamed: bool) -> None:
        if streamed:
            sys.stdout.write(self.cap_filter.finish())
            print(flush=True)
            self.end_reasoning()
        if self.finish_reason == "length" or self.cap_filter.found:
            print(
                "[note] the reply stopped at the plan's output cap (cap notice removed); "
                "max_tokens has no effect on MolochAI, so ask for less or split the task",
                file=sys.stderr,
            )
        if self.usage:
            u = self.usage
            print(
                f"[usage] prompt={u.prompt_tokens} completion={u.completion_tokens} "
                f"total={u.total_tokens}",
                file=sys.stderr,
            )


class CapNoticeFilter:
    """Pass text through, holding back any tail that could be the start of CAP_NOTICE."""

    def __init__(self) -> None:
        self._held = ""
        self.found = False

    def feed(self, text: str) -> str:
        self._held += text
        sizes = range(min(len(self._held), len(CAP_NOTICE)), 0, -1)
        keep = next((n for n in sizes if CAP_NOTICE.startswith(self._held[-n:])), 0)
        split = len(self._held) - keep
        ready, self._held = self._held[:split], self._held[split:]
        return ready

    def finish(self) -> str:
        held, self._held = self._held, ""
        self.found = held == CAP_NOTICE
        return "" if self.found else held


# --- sync and async -----------------------------------------------------------


def run_sync(kwargs: dict[str, Any], printer: Printer) -> None:
    response = call_with_retries(kwargs)  # HTTP errors are raised here, before any chunk
    if not kwargs.get("stream"):
        printer.reply(response)
        return
    try:
        for chunk in response:
            printer.chunk(chunk)
    finally:
        printer.done(streamed=True)


async def run_async(kwargs: dict[str, Any], printer: Printer) -> None:
    response = await acall_with_retries(kwargs)
    if not kwargs.get("stream"):
        printer.reply(response)
        return
    try:
        async for chunk in response:
            printer.chunk(chunk)
    finally:
        printer.done(streamed=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Call MolochAI through LiteLLM.")
    parser.add_argument(
        "prompt", nargs="?", default="Explain TCP slow start to a new engineer in one paragraph."
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("MOLOCHAI_MODEL") or DEFAULT_MODEL,
        help=f"model id, without the openai/ prefix (default: $MOLOCHAI_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="one plain reply")
    parser.add_argument("--async", dest="use_async", action="store_true", help="use acompletion")
    args = parser.parse_intermixed_args()

    kwargs = request(args)
    printer = Printer()
    try:
        if args.use_async:
            asyncio.run(run_async(kwargs, printer))
        else:
            run_sync(kwargs, printer)
    except openai.OpenAIError as err:  # every LiteLLM exception subclasses one of these
        return fail(describe(err))
    if not args.stream:
        printer.done(streamed=False)
    return 0


# ---------------------------------------------------------------------------
# Boilerplate shared (as a copy) by every Python example in this repository, so
# that each example stays a single file you can copy out. It reads the OpenAI
# SDK's errors; `unwrap()` above digs them out of LiteLLM's.
# ---------------------------------------------------------------------------

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


def describe_error(err: BaseException) -> str:
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
