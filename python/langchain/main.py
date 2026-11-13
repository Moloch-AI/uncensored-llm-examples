"""LangChain: ChatOpenAI pointed at MolochAI, with invoke, streaming, tools and structured output.

    python python/langchain/main.py invoke "In two sentences, what is a context window?"
    python python/langchain/main.py stream "Explain TCP slow start in one paragraph."
    python python/langchain/main.py tools "How many miles is a 21.1 km half marathon?"
    python python/langchain/main.py structured "Battery lasts two days, screen is dim. 3/5."
    python python/langchain/main.py structured --method json_schema "..."   # fails on MolochAI

What it shows (verified against the mock with langchain-openai 1.7.4):
- `with_structured_output` defaults to method="json_schema", which asks for the shape
  through `response_format`. MolochAI has no JSON mode and drops `response_format`
  without an error, so the model never sees the schema. method="function_calling"
  sends the schema as a tool instead, which MolochAI does support.
- ChatOpenAI drops `reasoning_content` from replies and never sends it back.
  ChatMolochAI below keeps it.
- ChatOpenAI's `max_retries` is the OpenAI SDK's retry count, and that SDK retries
  every 429, including quota errors that cannot pass before `resets_at`. This file
  turns it off and retries with `.with_retry()` and a policy that reads the error.

Answers go to stdout; reasoning, tool steps, usage and errors go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Literal

import openai
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, ValidationError

DEFAULT_BASE_URL = "https://api.molochai.dev/api/v1"
DEFAULT_MODEL = "glm-5.3-flash"
CAP_NOTICE = "\n\n[Response is too large for your current plan. Try a smaller request.]"
INLINE_THINK = re.compile(r"^\s*<think>(.*?)</think>\s*", re.DOTALL)


# --- a ChatOpenAI that keeps reasoning --------------------------------------


class ChatMolochAI(ChatOpenAI):
    """ChatOpenAI that keeps MolochAI's `reasoning_content`.

    ChatOpenAI targets the official OpenAI API and drops fields it does not know,
    so `reasoning_content` never reaches you and is never sent back. These three
    overrides put it in `message.additional_kwargs["reasoning_content"]` (merged
    across stream chunks like any other string) and send it back on assistant
    turns. They hook private methods of langchain-openai 1.7.4: check them again
    when you upgrade.
    """

    def _create_chat_result(self, response: Any, generation_info: dict | None = None) -> Any:
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump(warnings=False)
        for generation, choice in zip(result.generations, data.get("choices") or [], strict=False):
            reasoning = (choice.get("message") or {}).get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ) -> Any:
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        choices = chunk.get("choices") or []  # the usage chunk has `choices: []`
        reasoning = (choices[0].get("delta") or {}).get("reasoning_content") if choices else None
        if generation is not None and isinstance(reasoning, str) and reasoning:
            generation.message.additional_kwargs["reasoning_content"] = reasoning
        return generation

    def _get_request_payload(
        self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        for message, sent in zip(messages, payload.get("messages") or [], strict=False):
            reasoning = message.additional_kwargs.get("reasoning_content")
            if isinstance(message, AIMessage) and isinstance(reasoning, str) and reasoning:
                sent["reasoning_content"] = reasoning
        return payload


def make_llm(model: str) -> ChatMolochAI:
    api_key = os.environ.get("MOLOCHAI_API_KEY", "").strip()
    if not api_key:
        sys.exit(
            "error: MOLOCHAI_API_KEY is not set. Create a key at "
            "https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples"
            ", or run against the mock server (see local-development/README.md)."
        )
    return ChatMolochAI(
        model=model,
        base_url=os.environ.get("MOLOCHAI_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        api_key=api_key,
        # ChatOpenAI can switch to OpenAI's Responses API (/responses) based on the
        # model name or some options. MolochAI only has /chat/completions.
        use_responses_api=False,
        # No hidden retries in the OpenAI SDK; with_retry() below decides instead.
        max_retries=0,
        # The longest silence allowed between bytes. Plain requests must also
        # finish before the edge gives up (about 100 s); stream anything long.
        timeout=60,
    )


# --- retries ------------------------------------------------------------------


def should_retry(err: BaseException) -> bool:
    """Retry policy for `.with_retry()`: transient failures only. Reads status and type.

    Not retried: 401/402/403/413/422, 429 quota errors (they carry `resets_at`),
    503 `model_unavailable` (pick another model), and 429 rate limits, because
    with_retry waits with its own backoff and cannot read `Retry-After`.
    """
    if isinstance(err, openai.APIConnectionError):  # includes timeouts
        retry = True
    elif isinstance(err, openai.APIStatusError):
        kind = err.body.get("type") if isinstance(err.body, dict) else None
        retry = err.status_code in (502, 504, 524) or (
            err.status_code == 503 and kind == "server_busy"
        )
    else:
        retry = False
    if retry:  # tenacity also asks after the last attempt, so this is not "retrying now"
        print(f"[retryable] {describe_error(err).splitlines()[0]}", file=sys.stderr)
    return retry


def with_retries(runnable: Runnable) -> Runnable:
    # with_retry() wraps invoke() and batch(), not stream(): a stream is not retried.
    # Three attempts in all, with exponential backoff and jitter between them.
    return runnable.with_retry(
        retry_if_exception_type=should_retry,
        stop_after_attempt=3,
        exponential_jitter_params={"initial": 1, "max": 10},
    )


# --- invoke -------------------------------------------------------------------


def run_invoke(llm: ChatMolochAI, prompt: str) -> int:
    reply = with_retries(llm).invoke([HumanMessage(prompt)])
    content = reply.content if isinstance(reply.content, str) else str(reply.content)
    reasoning = reply.additional_kwargs.get("reasoning_content")
    # A non-streamed reply can carry reasoning inline as <think>...</think> instead
    # (the gateway strips it only while streaming). LangChain leaves it in content.
    if not reasoning and (match := INLINE_THINK.match(content)):
        reasoning, content = match.group(1).strip(), content[match.end() :]
    if reasoning:
        print(f"[reasoning] {reasoning.strip()}", file=sys.stderr)
    finish_reason = reply.response_metadata.get("finish_reason")
    print(content.removesuffix(CAP_NOTICE).strip())
    report(finish_reason, content.endswith(CAP_NOTICE), reply.usage_metadata)
    return 0


# --- stream -------------------------------------------------------------------


def run_stream(llm: ChatMolochAI, prompt: str) -> int:
    cap_filter = CapNoticeFilter()
    full: Any = None
    failure: openai.APIError | None = None
    in_reasoning = saw_text = False
    try:
        for chunk in llm.stream([HumanMessage(prompt)]):
            full = chunk if full is None else full + chunk
            reasoning = chunk.additional_kwargs.get("reasoning_content")
            if reasoning:
                if not in_reasoning:
                    sys.stderr.write("[reasoning] ")
                    in_reasoning = True
                sys.stderr.write(reasoning)
                sys.stderr.flush()
            if isinstance(chunk.content, str) and chunk.content:
                if in_reasoning:
                    sys.stderr.write("\n")
                    in_reasoning = False
                saw_text = True
                sys.stdout.write(cap_filter.feed(chunk.content))
                sys.stdout.flush()
    except openai.APIError as err:
        # HTTP errors before the first chunk are APIStatusError. An error event
        # inside the open stream (HTTP 200) is a plain APIError whose `body` is
        # the event's inner object; LangChain passes it through unchanged.
        failure = err
    if saw_text:
        sys.stdout.write(cap_filter.finish())
        print(flush=True)
    if in_reasoning:
        sys.stderr.write("\n")
    if failure is not None:
        message = describe_error(failure)
        if not isinstance(failure, (openai.APIStatusError, openai.APIConnectionError)):
            kind = failure.body.get("type") if isinstance(failure.body, dict) else None
            wait = "in about a minute" if kind == "preempted" else "with backoff"
            message += f"\n  the reply above is incomplete; retry {wait}"
        return fail(message)
    if full is not None:
        report(full.response_metadata.get("finish_reason"), cap_filter.found, full.usage_metadata)
    return 0


# --- tools --------------------------------------------------------------------

UNITS = {
    "km": ("length", 1000.0),
    "mi": ("length", 1609.344),
    "kg": ("mass", 1.0),
    "lb": ("mass", 0.45359237),
}
Unit = Literal["km", "mi", "kg", "lb"]


@tool
def convert_units(value: float, from_unit: Unit, to_unit: Unit) -> dict[str, Any]:
    """Convert a distance (km, mi) or a mass (kg, lb) to another unit."""
    if UNITS[from_unit][0] != UNITS[to_unit][0]:
        raise ValueError(f"cannot convert {from_unit} to {to_unit}")
    result = value * UNITS[from_unit][1] / UNITS[to_unit][1]
    return {"value": value, "from_unit": from_unit, "to_unit": to_unit, "result": round(result, 4)}


def run_tool_call(call: dict[str, Any]) -> ToolMessage:
    """Run one tool call. Bad arguments go back to the model as the tool result."""
    try:
        return convert_units.invoke(call)  # validates `args` against the tool's schema
    except (ValidationError, ValueError) as err:
        error = json.dumps({"error": str(err).splitlines()[0]})
        return ToolMessage(error, tool_call_id=call["id"], name=call["name"])


def run_tools(llm: ChatMolochAI, prompt: str, max_steps: int) -> int:
    model = with_retries(llm.bind_tools([convert_units]))
    messages: list[BaseMessage] = [
        SystemMessage("Use the convert_units tool for every conversion. Answer briefly."),
        HumanMessage(prompt),
    ]
    for step in range(1, max_steps + 1):
        reply = model.invoke(messages)
        messages.append(reply)  # reasoning_content travels back with it (ChatMolochAI)
        if not reply.tool_calls:
            print(str(reply.content).strip())
            print(f"[done] {step} model call(s)", file=sys.stderr)
            return 0
        for problem in reply.invalid_tool_calls:  # arguments that were not valid JSON
            print(f"[step {step}] invalid tool call: {problem}", file=sys.stderr)
        for call in reply.tool_calls:
            result = run_tool_call(call)
            args = json.dumps(call["args"])
            print(f"[step {step}] {call['name']}({args}) -> {result.content}", file=sys.stderr)
            messages.append(result)
    return fail(f"no final answer after {max_steps} model calls (raise --max-steps?)")


# --- structured output --------------------------------------------------------


class Review(BaseModel):
    """A product review reduced to what a dashboard needs."""

    sentiment: Literal["positive", "neutral", "negative"]
    rating: int = Field(ge=1, le=5, description="Stars, 1 to 5")
    summary: str = Field(description="One sentence")


def run_structured(llm: ChatMolochAI, text: str, method: str) -> int:
    # include_raw=True returns parsing problems as `parsing_error` instead of raising.
    structured = with_retries(llm.with_structured_output(Review, method=method, include_raw=True))
    messages = [SystemMessage("Extract the review from the user's text."), HumanMessage(text)]
    try:
        result = structured.invoke(messages)
    except ValidationError as err:
        # method="json_schema" with a Pydantic class parses the reply inside the
        # OpenAI SDK while reading the response, so a mismatch is raised here even
        # with include_raw=True.
        result = {"parsing_error": err, "parsed": None}
    error, review = result["parsing_error"], result["parsed"]
    if error is None and review is not None:
        print(review.model_dump_json(indent=2))
        return 0
    if isinstance(error, ValidationError):
        problem = error.errors()[0]
        first_line = f"{'.'.join(map(str, problem['loc'])) or 'reply'}: {problem['msg']}"
    else:
        first_line = str(error).splitlines()[0] if error else "no output"
    if method != "function_calling":
        return fail(
            f'with_structured_output(method="{method}") got a reply that is not a Review.\n'
            "  This method asks for the shape through `response_format`; MolochAI has no JSON mode "
            "and drops it without an error, so the model never saw the schema. "
            'Use method="function_calling".\n'
            f"  parsing error: {first_line}"
        )
    if error is not None:
        return fail(f"the tool arguments did not match the schema: {first_line}")
    return fail("the model answered in text instead of calling the Review tool; ask again")


# --- shared helpers -----------------------------------------------------------


def report(finish_reason: str | None, cap_notice: bool, usage: dict[str, Any] | None) -> None:
    if finish_reason == "length" or cap_notice:
        print(
            "[note] the reply stopped at the plan's output cap (cap notice removed); "
            "max_tokens has no effect on MolochAI, so ask for less or split the task",
            file=sys.stderr,
        )
    if usage:
        print(
            f"[usage] input={usage['input_tokens']} output={usage['output_tokens']} "
            f"total={usage['total_tokens']}",
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


def main() -> int:
    parser = argparse.ArgumentParser(description="LangChain's ChatOpenAI against MolochAI.")
    parser.add_argument("demo", choices=["invoke", "stream", "tools", "structured"])
    parser.add_argument("prompt", nargs="?", help="user message (each demo has a default)")
    parser.add_argument(
        "--model",
        default=os.environ.get("MOLOCHAI_MODEL") or DEFAULT_MODEL,
        help=f"model id (default: $MOLOCHAI_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--method",
        choices=["function_calling", "json_schema", "json_mode"],
        default="function_calling",
        help="with_structured_output method for the structured demo (default function_calling)",
    )
    parser.add_argument("--max-steps", type=int, default=4, help="model calls allowed (tools)")
    args = parser.parse_intermixed_args()  # flags may come after the prompt

    llm = make_llm(args.model)
    try:
        if args.demo == "invoke":
            return run_invoke(llm, args.prompt or "In two sentences, what is a context window?")
        if args.demo == "stream":
            return run_stream(llm, args.prompt or "Explain TCP slow start in one paragraph.")
        if args.demo == "tools":
            prompt = args.prompt or "How many miles is a 21.1 km half marathon?"
            return run_tools(llm, prompt, args.max_steps)
        text = args.prompt or "Battery easily lasts two days, but the screen is dim outdoors. 3/5."
        return run_structured(llm, text, args.method)
    except openai.OpenAIError as err:
        return fail(describe_error(err))


# ---------------------------------------------------------------------------
# Boilerplate shared (as a copy) by every Python example in this repository, so
# that each example stays a single file you can copy out. LangChain's own error
# classes (OpenAIRateLimitError and the rest) subclass the OpenAI SDK's errors
# and keep `status_code` and `body`, so this works on them unchanged.
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
