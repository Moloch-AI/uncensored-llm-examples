"""Document summarizer: chunk a long text file to fit the model's context, then map-reduce.

    python python/document-summarizer/main.py notes.txt
    python python/document-summarizer/main.py report.md --chunk-tokens 3000

1. Read the context limit for the chosen model from GET /api/auth/me (a
   MolochAI endpoint outside /api/v1). On any other server, or if that call
   fails, fall back to a conservative default.
2. Split the text on paragraph boundaries into chunks that leave room for the
   reply (reasoning tokens count against the context too).
3. Map: summarize each chunk. Reduce: combine the partial summaries, in more
   than one round if they do not fit together. The final summary streams to
   stdout; progress goes to stderr.

Token counts here are estimates (characters / 3, deliberately high for
English); if a chunk is still too large the gateway answers 413
`context_length_exceeded` and that chunk is split in two once.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import openai
from openai import OpenAI

DEFAULT_BASE_URL = "https://api.molochai.dev/api/v1"
DEFAULT_MODEL = "glm-5.3-flash"
CAP_NOTICE = "\n\n[Response is too large for your current plan. Try a smaller request.]"

FALLBACK_CONTEXT_TOKENS = 8192  # used when the server cannot tell us its limit
REPLY_RESERVE_TOKENS = 10_000  # output cap per reply (9,000 on most plans) plus prompt overhead
MAX_CHUNK_TOKENS = 16_000  # bigger chunks are slower and summarize more coarsely
CHARS_PER_TOKEN = 3
MAX_REDUCE_ROUNDS = 4

MAP_PROMPT = (
    "Summarize this part of a longer document in at most 8 bullet points. Keep names of "
    "systems, numbers and decisions. Do not add anything that is not in the text."
)
SINGLE_PROMPT = (
    "Summarize this document: a one-sentence overview, then at most 10 bullet points. Keep "
    "names of systems, numbers and decisions. Do not add anything that is not in the text."
)
REDUCE_PROMPT = (
    "These are summaries of consecutive parts of one document. Merge them into a single "
    "summary: a one-sentence overview, then at most 10 bullet points. Remove repetition."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize a long text file with map-reduce.")
    parser.add_argument("file", type=Path, help="UTF-8 text file to summarize")
    parser.add_argument(
        "--model",
        default=os.environ.get("MOLOCHAI_MODEL") or DEFAULT_MODEL,
        help=f"model id (default: $MOLOCHAI_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument("--chunk-tokens", type=int, help="override the computed chunk size")
    args = parser.parse_args()

    try:
        text = args.file.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        return fail(f"could not read {args.file}: {err}")
    if not text.strip():
        return fail(f"{args.file} is empty")

    client = make_client(timeout=60.0, max_retries=2)

    if args.chunk_tokens:
        chunk_tokens = args.chunk_tokens
    else:
        context = context_limit(client, args.model)
        if context is None:
            context = FALLBACK_CONTEXT_TOKENS
            log(f"context limit unknown, assuming {context} tokens")
        else:
            log(f"context limit for {args.model}: {context} tokens")
        reserve = min(REPLY_RESERVE_TOKENS, context // 2)
        chunk_tokens = min(MAX_CHUNK_TOKENS, context - reserve)
    chunk_chars = max(chunk_tokens * CHARS_PER_TOKEN, 200)

    try:
        chunks = split_text(text, chunk_chars)
        log(f"{len(text)} characters -> {len(chunks)} chunk(s) of up to ~{chunk_tokens} tokens")
        if len(chunks) == 1:
            stream_summary(client, args.model, SINGLE_PROMPT, chunks[0])
            return 0

        summaries = []
        for number, chunk in enumerate(chunks, 1):
            log(f"map {number}/{len(chunks)}")
            summaries.extend(summarize(client, args.model, MAP_PROMPT, chunk))

        for round_number in range(1, MAX_REDUCE_ROUNDS + 1):
            groups = split_text("\n\n".join(summaries), chunk_chars)
            if len(groups) == 1:
                log("reduce (final)")
                stream_summary(client, args.model, REDUCE_PROMPT, groups[0])
                return 0
            log(f"reduce round {round_number}: {len(summaries)} summaries -> {len(groups)}")
            summaries = [s for g in groups for s in summarize(client, args.model, REDUCE_PROMPT, g)]
        return fail("summaries did not converge; try a larger --chunk-tokens")
    except openai.OpenAIError as err:
        return fail(describe_error(err))


def context_limit(client: OpenAI, model: str) -> int | None:
    """Per-model context limit from GET /api/auth/me, or None if unavailable.

    The endpoint lives outside /api/v1: replace a trailing /v1 in the base URL
    with /auth/me. Per-model limits are the smaller of the plan's and the
    serving server's limit; the top-level value is the plan's.
    """
    base = str(client.base_url).rstrip("/")
    if not base.endswith("/v1"):
        return None
    request = urllib.request.Request(
        base[: -len("/v1")] + "/auth/me",
        headers={"Authorization": f"Bearer {client.api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            me = json.load(response)
    except (OSError, ValueError) as err:  # HTTPError and URLError are OSErrors
        log(f"GET /api/auth/me failed ({err}); not a MolochAI server?")
        return None
    if not isinstance(me, dict):
        return None
    for entry in me.get("models") or []:
        if entry.get("id") == model and isinstance(entry.get("context_limit"), int):
            return entry["context_limit"]
    limit = me.get("context_limit")
    return limit if isinstance(limit, int) else None


def split_text(text: str, max_chars: int) -> list[str]:
    """Pack paragraphs into chunks of at most max_chars, splitting long paragraphs."""
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        rest = paragraph.strip()
        while len(rest) > max_chars:  # cut at the last sentence end or space
            cut = max(rest.rfind(". ", 0, max_chars) + 1, rest.rfind(" ", 0, max_chars))
            cut = cut if cut > max_chars // 2 else max_chars
            pieces.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        if rest:
            pieces.append(rest)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def summarize(client: OpenAI, model: str, instruction: str, text: str) -> list[str]:
    """Summarize one chunk. On 413, split the chunk in two once and summarize both halves."""
    try:
        return [collect(client, model, instruction, text)]
    except openai.APIStatusError as err:
        body = err.body if isinstance(err.body, dict) else {}
        if err.status_code != 413 or body.get("type") != "context_length_exceeded":
            raise
        log(f"chunk too large for the server ({body.get('estimated_tokens')} tokens), splitting")
        halves = split_text(text, max(len(text) // 2, 200))
        return [collect(client, model, instruction, half) for half in halves]


def request(client: OpenAI, model: str, instruction: str, text: str) -> Any:
    return client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": instruction},
            {"role": "user", "content": text},
        ],
        stream=True,  # long inputs mean long thinking; plain requests die at ~100 s
    )


def collect(client: OpenAI, model: str, instruction: str, text: str) -> str:
    parts = [
        chunk.choices[0].delta.content or ""
        for chunk in request(client, model, instruction, text)
        if chunk.choices
    ]
    return "".join(parts).removesuffix(CAP_NOTICE).strip()


def stream_summary(client: OpenAI, model: str, instruction: str, text: str) -> None:
    """Final step: print the summary to stdout as it arrives."""
    held = ""  # the cap notice can only be at the very end, so hold back its length
    for chunk in request(client, model, instruction, text):
        if chunk.choices and chunk.choices[0].delta.content:
            held += chunk.choices[0].delta.content
            ready, held = held[: -len(CAP_NOTICE)], held[-len(CAP_NOTICE) :]
            sys.stdout.write(ready)
            sys.stdout.flush()
    if held.endswith(CAP_NOTICE):
        held = held.removesuffix(CAP_NOTICE)
        log("the summary stopped at the plan's output cap")
    print(held, flush=True)


def log(message: str) -> None:
    print(f"[summarizer] {message}", file=sys.stderr)


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
