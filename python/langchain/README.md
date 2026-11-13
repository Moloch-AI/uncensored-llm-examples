# LangChain (Python)

Use LangChain's `ChatOpenAI` with MolochAI: a plain call, streaming, tool calling with `bind_tools`,
and structured output that works without a JSON mode.

## What it does

One file, four demos, chosen by the first argument:

- `invoke`: one reply. Reasoning goes to stderr, the answer to stdout.
- `stream`: the same, token by token, with in-stream errors reported as such.
- `tools`: a tool loop with `bind_tools` and a `@tool` unit converter (no file, network or shell
  access). Bad tool arguments go back to the model as the tool result; `--max-steps` bounds the loop.
- `structured`: `with_structured_output(Review, method="function_calling", include_raw=True)`
  returns a validated Pydantic object. `--method json_schema` (LangChain's default) shows why the
  default does not work here.

The client is `ChatMolochAI`, a `ChatOpenAI` subclass of about 40 lines, built with
`base_url` and `api_key` from `MOLOCHAI_BASE_URL` and `MOLOCHAI_API_KEY`,
`use_responses_api=False` and `max_retries=0`. The sections below explain each choice.

Everything under "verified" was observed against the mock server with the versions in
[../requirements.txt](../requirements.txt) (`langchain-openai` 1.7.4, `langchain-core` 1.7.7,
`openai` 3.50.0), by reading the requests and responses on the wire. What the mock cannot show is
marked as applying to the real API only.

### Structured output: use `method="function_calling"`

`ChatOpenAI.with_structured_output()` defaults to `method="json_schema"` in langchain-openai 1.7.4.
(The base class `BaseChatOpenAI` defaults to `function_calling`; `ChatOpenAI` overrides it.) That
method asks for the shape only through `response_format`:

```json
"response_format": {"type": "json_schema", "json_schema": {"name": "Review", "strict": true, "schema": {...}}}
```

MolochAI has no JSON mode and drops `response_format` without an error. The schema is not in the
prompt either, so the model never sees it: it answers in free text and parsing fails. Verified:

- Against the mock, the default method gets a plain-text reply. With a Pydantic class,
  langchain-openai parses through the OpenAI SDK's `parse()` path, so the failure is a
  `pydantic.ValidationError` (`Invalid JSON`) raised from `invoke()`, even with `include_raw=True`.
- Asking for JSON in the prompt does not fix it. The reply is JSON, but not the schema's
  (`sentiment: Field required`).
- `method="json_mode"` sends `response_format: {"type": "json_object"}`, which is dropped as well.

`method="function_calling"` turns the schema into a tool and forces it with
`tool_choice: {"type": "function", "function": {"name": "Review"}}` (plus
`parallel_tool_calls: false`). MolochAI supports tools, so the arguments come back as JSON, and
Pydantic validates them. Verified against the mock, which always calls the first tool with
placeholder arguments built from its schema.

Applies to the real API only: MolochAI forwards `tool_choice` to the serving server unchanged, and
whether a forced choice is honored depends on that server. If the model answers in text instead,
`parsed` and `parsing_error` are both `None`; the example reports that case. For how the API treats
`parallel_tool_calls`, see the
[compatibility matrix](https://molochai.dev/docs/openai-compatibility?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples).

### Reasoning: `ChatOpenAI` drops `reasoning_content`

Verified: with plain `ChatOpenAI`, a reply that carries `message.reasoning_content` yields an
`AIMessage` whose `additional_kwargs` hold only `refusal`, and the streamed
`delta.reasoning_content` chunks arrive as empty chunks. LangChain does not send reasoning back on
assistant turns either. The library documents this: `ChatOpenAI` "targets official OpenAI API
specifications only. Non-standard response fields added by third-party providers (e.g.
`reasoning_content`, `reasoning_details`) are not extracted or preserved."

`ChatMolochAI` in [main.py](main.py) overrides three methods so that reasoning lands in
`message.additional_kwargs["reasoning_content"]` (on `invoke` and on every stream chunk, merged
like any other string when chunks are added), and is sent back on assistant messages. That keeps
the reasoning across a tool round trip; the request after a tool call carries it (verified on the
wire). The three methods are private hooks of langchain-openai. They work with the pinned
versions; check them again after an upgrade.

A non-streamed reply can also carry reasoning inline as `<think>...</think>` at the start of
`content` (the gateway strips it only while streaming). LangChain leaves it in `content` (verified
with the mock's `[mock:inline-think]`); the `invoke` demo splits it off.

### Retries: `max_retries=0`, then `.with_retry()` with a policy

- `ChatOpenAI(max_retries=...)` is handed to the OpenAI SDK client; unset, the SDK's default of 2
  applies. The SDK retries 408, 409, 429, every 5xx and connection errors, unless the response
  carries `X-Should-Retry: false`, which it checks first. MolochAI sends that header on quota and
  session 429s, 402 and 413. Verified: with the default, one `invoke()` that hit a quota 429
  reached the mock once before `OpenAIRateLimitError` was raised (3 times with a mock that does
  not send the header, such as uncensored-ai-api v0.1.0). 503 `model_unavailable` carries no such
  header and is retried twice.
- LangChain adds no retries unless you call `.with_retry()`, and that stacks on the SDK's: with
  `.with_retry(stop_after_attempt=3)` on a client with default `max_retries`, one `invoke()`
  against a 503 sent 9 requests (verified).
- This example sets `max_retries=0` and wraps calls in
  `.with_retry(retry_if_exception_type=should_retry, stop_after_attempt=3)`. In langchain-core 1.7.7,
  `retry_if_exception_type` also accepts a function, so the policy can read the status and the
  error `type`. It retries connection errors, 502, 504, 524 and 503 `server_busy`. It does not
  retry 401, 402, 403, 413, 422, quota 429s (`resets_at`) or 503 `model_unavailable`.
- Rate-limit 429s (`Too Many Attempts.`, over 60 requests per minute) carry `Retry-After`, which
  `with_retry` cannot read; it waits with its own exponential backoff. The policy leaves them alone
  rather than retrying too early. If you need them retried, do it in your own loop or keep the
  SDK's retries for that client, accepting that they also retry 503 `model_unavailable`.
- `.with_retry()` covers `invoke()` and `batch()` (and their async forms), not `stream()`. Verified:
  a stream whose first request failed with 502 was not retried. A stream that fails after text was
  printed should not be silently restarted anyway.

### Other behavior

- Responses API (from the langchain-openai source and docstring): `ChatOpenAI` can switch to
  OpenAI's `/responses` endpoint based on the model name or on options such as `reasoning`,
  `include` or `output_version="responses/v1"`. MolochAI only has `/chat/completions`, so the
  example sets `use_responses_api=False`. With today's model ids the switch stays off either way
  (verified for `glm-5.3-flash`).

The rest of this list was verified against the mock:

- The usage chunk with `choices: []` is handled: `usage_metadata` is filled on the merged message,
  without sending `stream_options`.
- `: ka` keep-alives and queue comments are ignored by the OpenAI SDK's SSE parser; queue progress
  is not visible through LangChain.
- The cap notice is passed through in `content`; the demos remove it and report the output cap
  from `finish_reason: "length"`.

## Dependencies

`langchain-openai`, `langchain-core`, `pydantic` and `openai`, pinned in
[../requirements.txt](../requirements.txt). Python 3.10 or newer. langchain-core 1.8 needs
Python 3.11, so the pin stays on 1.7.7, the newest release that installs on 3.10.

## Install

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r python/requirements.txt
```

## Configuration

| Variable / flag | Required | Default | Meaning |
|---|---|---|---|
| `MOLOCHAI_API_KEY` | yes | | API key from the [dashboard](https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples), or `mock-key` for the mock server |
| `MOLOCHAI_BASE_URL` | no | `https://api.molochai.dev/api/v1` | API base URL |
| `MOLOCHAI_MODEL` / `--model` | no | `glm-5.3-flash` | Model id; `--model` wins |
| demo (first positional) | yes | | `invoke`, `stream`, `tools` or `structured` |
| prompt (second positional) | no | one per demo | The user message (for `structured`, the review text) |
| `--method` | no | `function_calling` | `structured` only: `function_calling`, `json_schema` or `json_mode` |
| `--max-steps` | no | 4 | `tools` only: model calls allowed |

## Run

```bash
python python/langchain/main.py invoke
python python/langchain/main.py stream "Explain TCP slow start in one paragraph."
python python/langchain/main.py tools
python python/langchain/main.py structured
python python/langchain/main.py structured --method json_schema
```

Without a key, against the mock server (see [../../local-development](../../local-development)):

```bash
python local-development/devenv.py --mock -- python python/langchain/main.py structured
```

## Expected output

From the mock server, which returns canned text, not model output: tool and schema arguments are
placeholders built from the schema (numbers `1`, strings `"example"`, the first enum value). With
the real API the answers, arguments and token counts are the model's own.

```text
$ python python/langchain/main.py invoke
[reasoning] Mock reasoning for glm-5.3-flash.
[usage] input=11 output=27 total=38
Mock reply from glm-5.3-flash: In two sentences, what is a context window?

$ python python/langchain/main.py tools
[step 1] convert_units({"value": 1, "from_unit": "km", "to_unit": "km"}) -> {"value": 1.0, "from_unit": "km", "to_unit": "km", "result": 1.0}
[done] 2 model call(s)
Mock reply from glm-5.3-flash after tool convert_units: {"value": 1.0, "from_unit": "km", "to_unit": "km", "result": 1.0}

$ python python/langchain/main.py structured
{
  "sentiment": "positive",
  "rating": 1,
  "summary": "example"
}

$ python python/langchain/main.py structured --method json_schema
error: with_structured_output(method="json_schema") got a reply that is not a Review.
  This method asks for the shape through `response_format`; MolochAI has no JSON mode and drops it without an error, so the model never saw the schema. Use method="function_calling".
  parsing error: reply: Invalid JSON: expected value at line 1 column 1
```

Lines starting with `[` and `error:` are on stderr; answers and JSON are on stdout.

## Error handling

- Every error exits with status 1 and one message on stderr, built by the shared `describe_error`
  helper from the status code and the error `type`/`code`, never from the message text.
- LangChain re-raises the OpenAI SDK's errors as its own subclasses (for example
  `OpenAIRateLimitError`, a subclass of `openai.RateLimitError`) and keeps `status_code` and `body`,
  so the helper works unchanged. Verified against the mock:

  | MolochAI response | Raised by `ChatOpenAI` |
  |---|---|
  | 402 `no_active_plan` | `openai.APIStatusError` |
  | 403 `email_unverified` | `OpenAIPermissionDeniedError` |
  | 413 `context_length_exceeded` | `openai.APIStatusError`, not LangChain's `ContextOverflowError` (that check only looks at 400 responses) |
  | 422 validation error | `openai.UnprocessableEntityError` |
  | 429 quota, session quota or rate limit | `OpenAIRateLimitError` |
  | 502, 503, 524 | `OpenAIAPIError` (a subclass of `openai.InternalServerError`) |
  | error event inside a stream | `openai.APIError` with no status; `body` is the event's error object |

- An error event inside a stream (`upstream_timeout`, `preempted`) is reported with a note that the
  printed reply is incomplete.
- The tool loop sends bad arguments back to the model as `{"error": ...}`; arguments that are not
  valid JSON end up in `invalid_tool_calls` and are printed.
