# LiteLLM (Python)

Call MolochAI through LiteLLM's `completion` and `acompletion`, with streaming, LiteLLM's
exception classes traced back to MolochAI's error types, and LiteLLM's network access at import
turned off.

## What it does

- Passes `model="openai/<model id>"`, `api_base` and `api_key`. The `openai/` prefix selects
  LiteLLM's OpenAI-compatible provider; LiteLLM strips it and sends `"model": "glm-5.3-flash"`
  (verified on the wire).
- Streams by default. `--no-stream` asks for one plain reply, `--async` uses `acompletion` (with
  `async for` when streaming).
- Prints reasoning to stderr and the answer to stdout, removes the cap notice, and prints usage.
- Turns LiteLLM's retries off and applies a small policy of its own (see Retries).
- Reports every error with the LiteLLM class that was raised and what the API actually said, for
  example `[litellm.APIError] HTTP 413 context_length_exceeded ... (context_cap=65536, bound_by=plan)`.

Everything under "verified" was observed against the mock server with `litellm` 1.109.1 and
`openai` 3.50.0, by reading the requests and responses on the wire.

### Network access when LiteLLM is imported

- `import litellm` downloads `model_prices_and_context_window.json` from
  `raw.githubusercontent.com` (verified with a socket audit hook). When that fails, LiteLLM logs
  warnings, falls back to the copy bundled in the package and keeps retrying in a background thread
  (verified with the network blocked).
- `LITELLM_LOCAL_MODEL_COST_MAP=True`, set before the import, makes it use the bundled copy without
  any request. `LITELLM_OFFLINE_MODE=true` does the same and, per the 1.109.1 source, also covers
  the other files LiteLLM fetches on its own. MolochAI's model ids are not in that price list, so
  the download gains nothing here. The example sets the first variable with
  `os.environ.setdefault` before importing LiteLLM, so you can still override it.
- With that set, the example contacted nothing but `MOLOCHAI_BASE_URL`: every demo ran with all
  non-loopback DNS lookups blocked.
- Without `litellm.suppress_debug_info = True`, LiteLLM prints a "Give Feedback / Get Help" banner
  to stdout every time it raises. The example turns it off.

### Windows only: the bundled tokenizer is re-downloaded

LiteLLM counts tokens at the end of every stream, with tiktoken's `cl100k_base`, which it bundles.
In the `litellm-1.109.1-cp310-abi3-win_amd64` wheel the bundled tiktoken files have CRLF line
endings, so they fail tiktoken's hash check. The `manylinux_2_28_x86_64` and `macosx_11_0_arm64`
wheels ship LF files that pass it. On Windows, the first streamed call therefore deletes the bundled
`cl100k_base` file and downloads it from `openaipublic.blob.core.windows.net` into the package
folder. Verified:

- Online, this happens once, silently, and later runs are fine.
- Offline, the download fails. The stream still completes with the right answer and usage, but
  LiteLLM writes `LiteLLM:ERROR ... stream_chunk_builder() - Exception occurred` with tracebacks
  to stderr, and the bundled file is gone, so this repeats on every stream.

Workaround: run one streamed request while online after installing, or reinstall LiteLLM on a
machine that can reach that host once. CI runs on Linux and is not affected. This is a packaging
problem in that wheel, unrelated to MolochAI.

### Reasoning and usage

- `message.reasoning_content` (plain replies) and `delta.reasoning_content` (streams) are passed
  through (verified).
- For a non-streamed reply with `<think>...</think>` at the start of `content`, LiteLLM moves the
  block into `reasoning_content` and leaves the rest in `content` (verified with
  `[mock:inline-think]`).
- LiteLLM drops the final stream chunk with `choices: []` and `usage` unless the request has
  `stream_options={"include_usage": True}` (verified both ways). MolochAI ignores that parameter
  and always sends usage; the example sets it so that LiteLLM passes the usage on.
- LiteLLM adds `completion_tokens_details.reasoning_tokens` and `text_tokens` to streamed usage,
  computed locally with a tokenizer. MolochAI does not send that breakdown, so treat it as an
  estimate. `total_tokens` from the API is what counts against quota.

### Retries

- `max_retries` (default 2) is handed to the OpenAI SDK client, which retries every 429, every 5xx
  and connection errors. Verified: one quota 429 reached the mock 3 times with the default.
- `num_retries=N` replaces `max_retries` with `N` for the SDK client and then repeats the whole
  call up to `N` more times on any `openai.APIError`, which includes 402, 403, 413, 422 and quota
  429s. Verified: `num_retries=2` sent 5 requests for one quota error (also with `max_retries=0`)
  and 3 requests for a 413. Per the 1.109.1 source, the default strategy does not wait between
  those outer attempts.
- This example passes `max_retries=0`, does not use `num_retries`, and retries in
  `call_with_retries()`: connection errors, 502, 504, 524 and 503 `server_busy` with exponential
  backoff and jitter, and the 429 rate limit after `Retry-After`. It never retries 401, 402, 403,
  413, 422, quota 429s (`resets_at`) or 503 `model_unavailable`. A stream is retried only when it
  fails before it opens; once text was printed, the error is reported instead.

## Dependencies

`litellm` and `openai`, pinned in [../requirements.txt](../requirements.txt). Python 3.10 or newer.
LiteLLM pulls in many packages (tokenizers, aiohttp, boto3 and more), so the install is large.

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
| `MOLOCHAI_BASE_URL` | no | `https://api.molochai.dev/api/v1` | API base URL, passed as `api_base` |
| `MOLOCHAI_MODEL` / `--model` | no | `glm-5.3-flash` | Model id without the `openai/` prefix |
| `--no-stream` | no | off | One plain reply instead of a stream |
| `--async` | no | off | Use `acompletion` |
| `LITELLM_LOCAL_MODEL_COST_MAP` | no | `True` (set by the example) | `False` lets LiteLLM download its price list at import |
| prompt (positional) | no | a TCP question | The user message |

## Run

```bash
python python/litellm/main.py
python python/litellm/main.py --no-stream "Name three uses for a hash map."
python python/litellm/main.py --async "Explain TCP slow start in one paragraph."
```

Without a key, against the mock server (see [../../local-development](../../local-development)):

```bash
python local-development/devenv.py --mock -- python python/litellm/main.py
```

## Expected output

From the mock server, which returns canned text, not model output. The last three runs use the
mock's markers to trigger an in-stream error, a quota error and a 413.

```text
$ python python/litellm/main.py
[reasoning] Mock reasoning for glm-5.3-flash.
Mock reply from glm-5.3-flash: Explain TCP slow start to a new engineer in one paragraph.
[usage] prompt=15 completion=31 total=46

$ python python/litellm/main.py --no-stream "Name three uses for a hash map."
[reasoning] Mock reasoning for glm-5.3-flash.
[usage] prompt=8 completion=24 total=32
Mock reply from glm-5.3-flash: Name three uses for a hash map.

$ python python/litellm/main.py --async "[mock:timeout] Explain TCP slow start."
[reasoning] Mock reasoning for glm-5.3-flash.
Mock reply
error: [litellm.MidStreamFallbackError] stream error upstream_timeout: Server busy or request took too long.
  the reply above is incomplete; retry with backoff

$ python python/litellm/main.py --no-stream "[mock:quota] hi"
error: [litellm.RateLimitError] HTTP 429 quota_exceeded: Monthly token cap reached for your plan. Add account credit to keep going, or wait until the cap resets. (resets_at=2026-11-13T18:04:17+00:00)
  hint: quota used up; retrying before resets_at will not help

$ python python/litellm/main.py --no-stream "[mock:context] hi"
error: [litellm.APIError] HTTP 413 context_length_exceeded: This request is larger than your plan allows. Trim the conversation history, or upgrade your plan for a larger context window. (estimated_tokens=70000, context_cap=65536, bound_by=plan)
  hint: the prompt does not fit this model's context; shorten it
```

Lines starting with `[` and `error:` are on stderr; the answer is on stdout.

## Error handling

LiteLLM wraps the OpenAI SDK's exceptions in its own classes. All of them subclass the OpenAI SDK's
error types, and all of them keep the original OpenAI SDK exception, with the real status and body,
in their chain (`__cause__` or `original_exception`). `unwrap()` returns that original exception and
the shared `describe_error` helper reads it. Read the status and `type` from there, not from
LiteLLM's message (`litellm.RateLimitError: RateLimitError: OpenAIException - ...`), which carries
neither `resets_at` nor `context_cap`.

What LiteLLM 1.109.1 raised for each MolochAI response (verified against the mock):

| MolochAI response | LiteLLM raises | `status_code` | Note |
|---|---|---|---|
| 402 `no_active_plan` | `litellm.APIError` | 402 | |
| 403 `email_unverified` | `litellm.APIError` | 403 | not `PermissionDeniedError` |
| 413 `context_length_exceeded` | `litellm.APIError` | 413 | not `ContextWindowExceededError`, so LiteLLM's context-window fallbacks, which look for that class, do not apply |
| 422 validation error | `litellm.BadRequestError` | 422 | |
| 429 quota, session quota, rate limit | `litellm.RateLimitError` | 429 | same class for all three; only the rate limit has `Retry-After`, only quota errors have `resets_at` |
| 502 `upstream_error`, `upstream_unreachable` | `litellm.BadGatewayError` | 502 | |
| 503 `server_busy`, `model_unavailable` | `litellm.ServiceUnavailableError` | 503 | same class; read `type` before retrying |
| 504 `upstream_timeout` | `litellm.Timeout` | 504 | also a subclass of `openai.APITimeoutError`, like a client-side timeout (status 408) |
| 524 with an HTML body | `litellm.APIError` | 524 | |
| error event inside a stream | `litellm.MidStreamFallbackError` | 500 | the HTTP status was 200; `generated_content` holds the text received before the error |
| connection refused | `litellm.InternalServerError` | 500 | no response at all; the placeholder response's URL is `https://api.openai.com/v1`, though no request went there |

Every error exits with status 1 and one message on stderr. An error event inside a stream
(`upstream_timeout`, `preempted`) is reported with a note that the printed reply is incomplete.
