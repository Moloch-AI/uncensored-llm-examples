"""JSON output without a JSON mode: schema in the prompt, extract, validate, repair once.

    python python/json-output/main.py
    python python/json-output/main.py --input review.txt --schema my-schema.json

MolochAI has no JSON mode: `response_format` is accepted and silently dropped.
So the schema goes into the prompt, the reply is parsed leniently (models
sometimes wrap JSON in prose or code fences), and the result is validated with
`jsonschema`. If validation fails, the validation errors are sent back once and
the model gets one chance to correct its reply. Valid JSON goes to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import openai
from jsonschema import Draft202012Validator, SchemaError
from openai import OpenAI

DEFAULT_BASE_URL = "https://api.molochai.dev/api/v1"
DEFAULT_MODEL = "glm-5.3-flash"
CAP_NOTICE = "\n\n[Response is too large for your current plan. Try a smaller request.]"

DEFAULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "product": {"type": "string", "minLength": 1},
        "sentiment": {"type": "string", "enum": ["positive", "mixed", "negative"]},
        "rating": {"type": "integer", "minimum": 1, "maximum": 5},
        "pros": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "cons": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
    "required": ["product", "sentiment", "rating", "pros", "cons"],
    "additionalProperties": False,
}

DEFAULT_TEXT = (
    "Bought the Trailhead 2 headlamp for night runs. The beam is bright and even, and the "
    "strap stays put. Battery life is shorter than advertised (about 3 hours on high), and "
    "the USB-C port cover feels flimsy. Would still buy it again, maybe 4 out of 5."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract schema-valid JSON from text.")
    parser.add_argument("--input", type=Path, help="text file to extract from (default: a sample)")
    parser.add_argument("--schema", type=Path, help="JSON Schema file (default: a review schema)")
    parser.add_argument(
        "--model",
        default=os.environ.get("MOLOCHAI_MODEL") or DEFAULT_MODEL,
        help=f"model id (default: $MOLOCHAI_MODEL or {DEFAULT_MODEL})",
    )
    args = parser.parse_args()

    try:
        text = args.input.read_text(encoding="utf-8") if args.input else DEFAULT_TEXT
        schema = json.loads(args.schema.read_text(encoding="utf-8")) if args.schema else None
    except (OSError, json.JSONDecodeError) as err:
        return fail(f"could not read input: {err}")
    schema = schema or DEFAULT_SCHEMA
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as err:
        return fail(f"--schema is not a valid JSON Schema: {err.message}")
    validator = Draft202012Validator(schema)

    client = make_client(timeout=60.0, max_retries=2)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You extract data from text. Reply with one JSON object that matches this "
                "JSON Schema, and nothing else: no prose, no code fences.\n\n"
                f"```json\n{json.dumps(schema, indent=2)}\n```"
            ),
        },
        {"role": "user", "content": f"Text:\n<<<\n{text}\n>>>"},
    ]

    for attempt in (1, 2):
        try:
            reply = complete(client, args.model, messages)
        except openai.OpenAIError as err:
            return fail(describe_error(err))

        problems: list[str]
        data = extract_json(reply)
        if data is None:
            problems = ["the reply did not contain a JSON object"]
        else:
            problems = [
                f"at {error.json_path}: {error.message}"
                for error in sorted(validator.iter_errors(data), key=lambda e: e.json_path)
            ]
        if not problems:
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return 0

        print(f"[attempt {attempt}] invalid reply:", *problems, sep="\n  ", file=sys.stderr)
        if attempt == 1:
            # One repair round: show the model its reply and exactly what is wrong.
            messages += [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": "That reply does not match the schema:\n- "
                    + "\n- ".join(problems)
                    + "\nReply again with only the corrected JSON object.",
                },
            ]

    return fail("the reply was still invalid after one repair attempt")


def complete(client: OpenAI, model: str, messages: list[dict[str, Any]]) -> str:
    """Stream a reply and return the whole answer text.

    Streaming here is not about showing progress: a reasoning model can think
    for a long time before answering, and a plain request that runs past about
    100 seconds is cut off at the edge.
    """
    stream = client.chat.completions.create(model=model, messages=messages, stream=True)
    parts: list[str] = []
    finish_reason = None
    for chunk in stream:
        if not chunk.choices:  # the final usage chunk
            continue
        parts.append(chunk.choices[0].delta.content or "")
        finish_reason = chunk.choices[0].finish_reason or finish_reason
    reply = "".join(parts)
    if finish_reason == "length":
        print("[note] the reply stopped at the plan's output cap", file=sys.stderr)
    return reply.removesuffix(CAP_NOTICE)


def extract_json(reply: str) -> Any | None:
    """Find the first JSON object in a reply that may also contain prose or code fences."""
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
    candidates = [reply]
    candidates += re.findall(r"```(?:json)?\s*\n(.*?)```", reply, flags=re.DOTALL)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", reply):  # last resort: first decodable object
        try:
            value, _ = decoder.raw_decode(reply, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


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
