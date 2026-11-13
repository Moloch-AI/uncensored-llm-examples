"""Tool calling: a complete tool loop with two safe local tools and a step limit.

    python python/tool-calling/main.py
    python python/tool-calling/main.py "How many pounds is 12.5 kg, times 3?"

Loop: send the conversation with `tools`; when the reply ends with
finish_reason "tool_calls", validate each call's JSON arguments against the
tool's own schema, run the tool locally, append the results as `role: "tool"`
messages and ask again. Stop when the model answers in text, or fail after
--max-steps model calls. Tool errors (bad arguments, division by zero) go back
to the model as the tool result so it can correct itself; they do not crash.

The tools only do arithmetic and unit conversion: no file, network or shell
access, so nothing a model asks for can reach outside this process.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import operator
import os
import sys
from collections.abc import Callable
from typing import Any

import openai
from jsonschema import Draft202012Validator
from openai import OpenAI

DEFAULT_BASE_URL = "https://api.molochai.dev/api/v1"
DEFAULT_MODEL = "glm-5.3-flash"

# --- the local tools --------------------------------------------------------

UNITS = {  # unit: (dimension, factor to the base unit)
    "km": ("length", 1000.0),
    "m": ("length", 1.0),
    "mi": ("length", 1609.344),
    "ft": ("length", 0.3048),
    "kg": ("mass", 1.0),
    "lb": ("mass", 0.45359237),
}


def convert_units(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    from_dim, from_factor = UNITS[from_unit]
    to_dim, to_factor = UNITS[to_unit]
    if from_dim != to_dim:
        raise ValueError(f"cannot convert {from_dim} ({from_unit}) to {to_dim} ({to_unit})")
    result = value * from_factor / to_factor
    return {"value": value, "from_unit": from_unit, "to_unit": to_unit, "result": round(result, 6)}


BINARY_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
UNARY_OPS: dict[type, Callable[[Any], Any]] = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def calculate(expression: str) -> dict[str, Any]:
    """Evaluate arithmetic by walking the syntax tree. Never calls eval()."""

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in BINARY_OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 64:
                raise ValueError("exponent too large (limit 64)")
            value = BINARY_OPS[type(node.op)](left, right)
            if isinstance(value, complex):
                raise ValueError("result is a complex number")
            if isinstance(value, int) and value.bit_length() > 1024:
                raise ValueError("number too large")
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in UNARY_OPS:
            return UNARY_OPS[type(node.op)](walk(node.operand))
        raise ValueError(f"unsupported syntax: {type(node).__name__}")

    result = walk(ast.parse(expression, mode="eval").body)
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError("result is not a finite number")
    return {"expression": expression, "result": result}


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "convert_units",
            "description": "Convert a length (km, m, mi, ft) or a mass (kg, lb) to another unit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "number"},
                    "from_unit": {"type": "string", "enum": list(UNITS)},
                    "to_unit": {"type": "string", "enum": list(UNITS)},
                },
                "required": ["value", "from_unit", "to_unit"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate arithmetic: numbers, + - * / // % ** and parentheses.",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "maxLength": 200}},
                "required": ["expression"],
                "additionalProperties": False,
            },
        },
    },
]
IMPLEMENTATIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "convert_units": convert_units,
    "calculate": calculate,
}
VALIDATORS = {
    tool["function"]["name"]: Draft202012Validator(tool["function"]["parameters"]) for tool in TOOLS
}


def run_tool(name: str, raw_arguments: str) -> str:
    """Validate and run one call. Always returns a JSON string for the tool message."""
    if name not in IMPLEMENTATIONS:
        return json.dumps({"error": f"unknown tool {name!r}"})
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as err:
        return json.dumps({"error": f"arguments are not valid JSON: {err}"})
    problems = [e.message for e in VALIDATORS[name].iter_errors(arguments)]
    if problems:
        return json.dumps({"error": "invalid arguments: " + "; ".join(problems)})
    try:
        return json.dumps(IMPLEMENTATIONS[name](**arguments))
    except (ValueError, ArithmeticError, SyntaxError) as err:
        return json.dumps({"error": str(err)})


# --- the loop -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a tool-calling loop with local tools.")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="A trail is 13.1 miles long. How many kilometers is that, and how many hours "
        "does it take at 4.5 km/h?",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("MOLOCHAI_MODEL") or DEFAULT_MODEL,
        help=f"model id (default: $MOLOCHAI_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument("--max-steps", type=int, default=6, help="model calls allowed (default 6)")
    args = parser.parse_args()

    client = make_client(timeout=60.0, max_retries=2)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "Use the tools for every conversion and calculation. Answer briefly.",
        },
        {"role": "user", "content": args.prompt},
    ]

    for step in range(1, args.max_steps + 1):
        try:
            content, reasoning, tool_calls = stream_turn(client, args.model, messages)
        except openai.OpenAIError as err:
            return fail(describe_error(err))

        if not tool_calls:
            print(content.strip())
            print(f"[done] {step} model call(s)", file=sys.stderr)
            return 0

        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": content or None,
            "tool_calls": tool_calls,
        }
        if reasoning:
            # MolochAI accepts reasoning_content on assistant messages; sending it
            # back keeps the model's reasoning across the tool round trip.
            assistant["reasoning_content"] = reasoning
        messages.append(assistant)

        for call in tool_calls:
            name, raw = call["function"]["name"], call["function"]["arguments"]
            result = run_tool(name, raw)
            print(f"[step {step}] {name}({raw}) -> {result}", file=sys.stderr)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    return fail(f"no final answer after {args.max_steps} model calls (raise --max-steps?)")


def stream_turn(
    client: OpenAI, model: str, messages: list[dict[str, Any]]
) -> tuple[str, str, list[dict[str, Any]]]:
    """Stream one reply and return (content, reasoning, tool_calls).

    MolochAI sends each tool call's arguments whole in one chunk, but other
    OpenAI-compatible servers split them, so fragments are joined by `index`
    the same way the SDK's own accumulator does.
    """
    stream = client.chat.completions.create(
        model=model, messages=messages, tools=TOOLS, stream=True
    )
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    for chunk in stream:
        if not chunk.choices:  # the final usage chunk
            continue
        delta = chunk.choices[0].delta
        content.append(delta.content or "")
        reasoning.append(getattr(delta, "reasoning_content", None) or "")
        for fragment in delta.tool_calls or []:
            slot = calls.setdefault(
                fragment.index,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if fragment.id:
                slot["id"] = fragment.id
            if fragment.function and fragment.function.name:
                slot["function"]["name"] += fragment.function.name
            if fragment.function and fragment.function.arguments:
                slot["function"]["arguments"] += fragment.function.arguments
    return "".join(content), "".join(reasoning), [calls[i] for i in sorted(calls)]


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
