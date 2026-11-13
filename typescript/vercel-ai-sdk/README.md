# Vercel AI SDK (TypeScript)

Use the [Vercel AI SDK](https://ai-sdk.dev) with MolochAI through `@ai-sdk/openai-compatible`:
`generateText`, `streamText` and a tool call, with reasoning, in-stream errors and retries handled.

## What it does

One file, three demos, chosen by the first argument:

- `generate`: `generateText`. Reasoning goes to stderr, the answer to stdout.
- `stream`: `streamText`, read from `fullStream` so that errors are seen (see below).
- `tools`: `generateText` with a `tool()` unit converter (no file, network or shell access) and
  `stopWhen: stepCountIs(n)`; each tool step is printed to stderr.

The model comes from `createOpenAICompatible({ name, baseURL, apiKey, includeUsage: true })` and
`chatModel(id)`, wrapped with `extractReasoningMiddleware({ tagName: 'think' })`. Every call passes
`maxRetries: 0` and goes through a small retry policy instead.

Everything under "verified" was observed against the mock server with `ai` 7.0.172 and
`@ai-sdk/openai-compatible` 3.0.92, by reading the requests and responses on the wire.

### Reasoning

- `reasoning_content` becomes `reasoningText` and `reasoning` parts in `generateText`, and
  `reasoning-delta` parts in `streamText` (verified).
- In a tool loop the SDK sends `reasoning_content` back on the assistant message that holds the
  tool call, with no extra code (verified on the wire).
- A non-streamed reply can carry reasoning inline as `<think>...</think>` (the gateway strips it
  only while streaming). Without the middleware it stays in `text` and `reasoningText` is
  undefined. With `extractReasoningMiddleware` it moves to `reasoningText` (both verified with
  `[mock:inline-think]`).

### Errors inside a stream: `streamText` does not throw

Verified with the mock's `[mock:timeout]` and `[mock:preempted]`, which send some text and then
`data: {"error": {"type": "upstream_timeout", ...}}` while the HTTP status is already 200:

- `fullStream` yields an `error` part holding a `StreamProviderError` with `type` (`upstream_timeout`,
  `preempted`), `message` and `isRetryable: false`. Extra fields such as `elapsed_s` are dropped.
  Then come `text-end`, `finish-step` and `finish`, and `finishReason` resolves to `'error'`.
- Nothing throws. Iterating `textStream` ends quietly with the partial text, so code that reads
  only `textStream` cannot tell a cut-off answer from a complete one.
- Without an `onError` option, the SDK prints the whole error object to stderr.

An HTTP error before the stream opens (such as 503 `model_unavailable`) behaves the same way: one
`error` part holding an `APICallError`, `textStream` yields nothing, and `await result.finishReason`
rejects with `NoOutputGeneratedError`.

The `stream` demo therefore reads `fullStream`, remembers the `error` part, passes
`onError: () => {}`, and exits with status 1 and a note that the printed reply is incomplete.

### HTTP errors and what `APICallError` keeps

`APICallError` has `statusCode`, `responseHeaders`, `responseBody` (the raw text) and
`isRetryable`. Its `message` and `data` come from the provider's error schema, which expects
`{"error": {"message", "type", "code", "param"}}`. Verified:

- Gateway errors (`{"error": {...}}`): `message` is the error's message and `data.error` keeps
  `message` and `type`. `resets_at`, `context_cap`, `estimated_tokens`, `bound_by` and `model` are
  dropped.
- Validation errors (422) and bare messages (403 `email_unverified`, the 429 rate limit):
  `data` is undefined and `message` is only the HTTP status text (`Unprocessable Entity`,
  `Forbidden`, `Too Many Requests`).
- The HTML 524 page: `message` is `unknown`.

So `describeError` parses `responseBody` itself and reads the status, `type`/`code` and the extra
fields from there.

### Retries

- `maxRetries` defaults to 2. The SDK retries when `isRetryable` is true: 408, 409, 429, every 5xx
  and connection errors. It waits 2 s, then 4 s, or `Retry-After` when that is under 60 s. After the
  last attempt it throws a `RetryError` (`reason: 'maxRetriesExceeded'`) that wraps the attempts.
- Verified: one `generateText` that hit a quota 429 made 3 requests over about 6 s before the
  `RetryError`; 503 `model_unavailable` was retried the same way. A quota error does not pass
  before `resets_at`. The AI SDK ignores the `X-Should-Retry: false` header that MolochAI sends on
  quota errors (the OpenAI SDKs obey it), so the 3 requests are the same with or without it.
- `createOpenAICompatible` has no option to change which errors count as retryable. (The
  provider's chat model class accepts an `errorStructure` with `isRetryable`, but the factory does
  not pass one through.)
- This example passes `maxRetries: 0` and wraps `generateText` in `withRetries()`: connection
  errors, 502, 504, 524 and 503 `server_busy` with exponential backoff and jitter, and the 429 rate
  limit after `Retry-After`. It never retries 401, 402, 403, 413, 422, quota 429s or 503
  `model_unavailable`. Retrying the `tools` demo reruns the whole loop, which is fine for tools
  without side effects. Streams are not retried.

### Other behavior, verified

- The usage chunk with `choices: []` is read whether or not `includeUsage` is set. `includeUsage:
  true` adds `stream_options`, which MolochAI drops; other OpenAI-compatible servers need it.
- `outputTokenDetails.reasoningTokens` is 0 even when the reply had reasoning, because the API does
  not break the count down. `outputTokens` includes reasoning, and `totalTokens` is what counts
  against quota.
- `: ka` keep-alives and queue comments are ignored; queue progress is not visible through the SDK.
- The cap notice is passed through in the text, with `finishReason: 'length'`; the demos remove it.
- Structured output through `Output.object` sends `response_format: {"type": "json_object"}`
  (this provider defaults to `supportsStructuredOutputs: false`) and does not put the schema in the
  prompt. MolochAI drops `response_format`, the reply is free text, and the SDK throws
  `NoObjectGeneratedError`. It also logs a warning that a JSON response format with a schema needs
  structured outputs. Ask for structured data with a tool, as the `tools` demo does, or put the
  schema in the prompt and validate it, as [../structured-extraction](../structured-extraction)
  does.

## Dependencies

`ai` and `@ai-sdk/openai-compatible` (both require Node.js 22 or newer), `zod` 4, plus `tsx` to run
TypeScript directly. All pinned in [../package.json](../package.json).

## Install

From the `typescript/` folder:

```bash
npm ci
```

## Configuration

| Variable / flag | Required | Default | Meaning |
|---|---|---|---|
| `MOLOCHAI_API_KEY` | yes | | API key from the [dashboard](https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples), or `mock-key` for the mock server |
| `MOLOCHAI_BASE_URL` | no | `https://api.molochai.dev/api/v1` | API base URL |
| `MOLOCHAI_MODEL` / `--model` | no | `glm-5.3-flash` | Model id |
| demo (first positional) | yes | | `generate`, `stream` or `tools` |
| prompt (second positional) | no | one per demo | The user message |
| `--max-steps` | no | 4 | `tools` only: model calls allowed |

## Run

From the `typescript/` folder:

```bash
npx tsx vercel-ai-sdk/main.ts generate
npx tsx vercel-ai-sdk/main.ts stream "Explain TCP slow start in one paragraph."
npx tsx vercel-ai-sdk/main.ts tools
```

Without a key, against the mock server (see [../../local-development](../../local-development)):

```bash
python ../local-development/devenv.py --mock -- npx tsx vercel-ai-sdk/main.ts stream
```

## Expected output

From the mock server, which returns canned text, not model output. The mock calls the first tool
with placeholder arguments built from its schema (numbers `1`, the first enum value). With the real
API the answers, arguments and token counts are the model's own.

```text
$ npx tsx vercel-ai-sdk/main.ts generate
[reasoning] Mock reasoning for glm-5.3-flash.
Mock reply from glm-5.3-flash: In two sentences, what is a context window?
[usage] input=11 output=27 total=38

$ npx tsx vercel-ai-sdk/main.ts tools
[step 1] convert_units({"value":1,"from_unit":"km","to_unit":"km"}) -> {"value":1,"from_unit":"km","to_unit":"km","result":1}
Mock reply from glm-5.3-flash after tool convert_units: {"value":1,"from_unit":"km","to_unit":"km","result":1}
[done] 2 model call(s)

$ npx tsx vercel-ai-sdk/main.ts stream "[mock:preempted] Explain TCP slow start."
[reasoning] Mock reasoning for glm-5.3-flash.
Mock reply
error: stream error preempted: The server running this reply was reclaimed mid-reply. Retry in a minute.
  the reply above is incomplete; retry in about a minute

$ npx tsx vercel-ai-sdk/main.ts generate "[mock:quota] hi"
error: HTTP 429 quota_exceeded: Monthly token cap reached for your plan. Add account credit to keep going, or wait until the cap resets. (resets_at=2026-11-13T18:04:43+00:00)
  hint: quota used up; retrying before resets_at will not help
```

Lines starting with `[` and `error:` are on stderr; the answers are on stdout.

## Error handling

- Every error exits with status 1 and one message on stderr from `describeError`, which reads the
  status and `type`/`code` from the response body, never the message text.
- `RetryError` is unwrapped to its last error; `StreamProviderError` is reported as a stream error.
- Tool arguments are validated against the tool's zod `inputSchema` by the SDK before `execute`
  runs. A conversion between a length and a mass goes back to the model as `{"error": ...}`.
