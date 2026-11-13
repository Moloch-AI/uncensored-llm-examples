"""Model selection: list the catalog, pick a model that is being served, fall back on 503.

    python python/model-selection/main.py
    python python/model-selection/main.py --model qwen3.8-27b-uncensored --fallbacks glm-5.3-flash

GET /models returns every public model id with two MolochAI extensions: `name`
(display name) and `available` (a server is serving it right now). The listing
is a snapshot, so a model can still go away before the request lands: that
returns 503 `model_unavailable`, and the gateway never reroutes to another
model on its own. This example tries each candidate once, in order.
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Pick a served model and fall back on 503.")
    parser.add_argument("prompt", nargs="?", default="Reply with one short sentence about tides.")
    parser.add_argument(
        "--model",
        default=os.environ.get("MOLOCHAI_MODEL") or DEFAULT_MODEL,
        help=f"preferred model id (default: $MOLOCHAI_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--fallbacks",
        default="",
        help="comma-separated ids to try next (default: every other available model)",
    )
    args = parser.parse_args()

    # max_retries=0: the SDK would otherwise retry the 503 model_unavailable
    # twice on the same model before this code could move on.
    client = make_client(timeout=120.0, max_retries=0)

    try:
        listing = client.models.list()
    except openai.OpenAIError as err:
        return fail(describe_error(err))

    catalog: dict[str, bool] = {}
    print(f"{'id':<38} {'name':<42} available", file=sys.stderr)
    for model in listing.data:
        available = bool(getattr(model, "available", True))  # extension; assume yes elsewhere
        catalog[model.id] = available
        name = getattr(model, "name", "") or ""
        print(f"{model.id:<38} {name:<42} {'yes' if available else 'no'}", file=sys.stderr)

    wanted = [args.model, *(m.strip() for m in args.fallbacks.split(",") if m.strip())]
    if not args.fallbacks:
        wanted += [model_id for model_id, available in catalog.items() if available]

    candidates: list[str] = []
    for model_id in dict.fromkeys(wanted):  # de-duplicate, keep order
        if model_id not in catalog:
            print(f"[skip] {model_id}: not in the catalog (would be a 422)", file=sys.stderr)
        elif not catalog[model_id]:
            print(f"[skip] {model_id}: listed as not available", file=sys.stderr)
        else:
            candidates.append(model_id)

    for model_id in candidates:
        try:
            completion = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": args.prompt}],
            )
        except openai.APIStatusError as err:
            body = err.body if isinstance(err.body, dict) else {}
            if err.status_code == 503 and body.get("type") == "model_unavailable":
                print(
                    f"[fallback] {model_id}: 503 model_unavailable, trying the next one",
                    file=sys.stderr,
                )
                continue
            return fail(describe_error(err))
        except openai.OpenAIError as err:
            return fail(describe_error(err))

        # `model` in the response is always the public id you asked for.
        print(f"[answered by] {completion.model}", file=sys.stderr)
        print((completion.choices[0].message.content or "").strip())
        return 0

    return fail(
        "no candidate model could serve the request (model_unavailable); check "
        "https://molochai.dev/models?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples"
        " or try again later"
    )


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
