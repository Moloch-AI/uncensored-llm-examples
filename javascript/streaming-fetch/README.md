# Streaming with plain fetch (JavaScript, no SDK)

Stream a reply with Node's built-in `fetch` and a hand-written server-sent events parser, to show
exactly what goes over the wire.

## What it does

- `POST /chat/completions` with `"stream": true`, using `fetch` and nothing else.
- Checks the HTTP status before reading the stream. Error bodies come in three shapes (gateway
  `{"error": {...}}`, validation `{"message", "errors"}`, bare `{"message"}`) and are sometimes not
  JSON at all; `describeHttpError` handles each and shows `Retry-After` on a 429.
- Parses SSE line by line: `data:` lines (joined if an event has several), blank lines that end an
  event, and comment lines that start with `:`. Comments are ignored, except that queue notices
  (`: queued · position 2 of 3 · waiting 4s`, sent while a request waits for a free slot) are
  shown on stderr as progress. The keep-alives (`: ka`, `: hb`) are ignored. The match is loose
  (`startsWith('queued')`), because the wording is meant for people.
- Writes `delta.content` to stdout and `delta.reasoning_content` to stderr (`--hide-reasoning`
  drops it), reads usage from the final chunk with `"choices": []`, and stops at `data: [DONE]`.
- Handles an error event after the stream opened (`data: {"error": {"type": "upstream_timeout", ...}}`
  under HTTP 200).
- Removes the cap notice the gateway appends at the output cap, even when it arrives split across
  chunks, and reports `finish_reason: "length"`.
- Aborts if no bytes arrive for 60 seconds. The gateway sends a keep-alive comment about once a
  second, so a longer silence means the connection is gone.

This is the parsing an SDK does for you. The OpenAI SDKs drop comment lines, so this is also the
way to see queue progress without the MolochAI SDKs
([Python](https://github.com/Moloch-AI/uncensored-ai-python),
[Node](https://github.com/Moloch-AI/uncensored-ai-node)). For dependency-free reference clients
with retries and backoff, see [uncensored-ai-api](https://github.com/Moloch-AI/uncensored-ai-api).

## Dependencies

None. Node.js 20 or newer (built-in `fetch`, `ReadableStream` async iteration, `util.parseArgs`).
`npm ci` in this folder is only needed for the other example and the linter.

## Install

Nothing to install for this example.

## Configuration

| Variable / flag | Required | Default | Meaning |
|---|---|---|---|
| `MOLOCHAI_API_KEY` | yes | | API key, or `mock-key` for the mock server |
| `MOLOCHAI_BASE_URL` | no | `https://api.molochai.dev/api/v1` | API base URL |
| `MOLOCHAI_MODEL` / `--model` | no | `glm-5.3-flash` | Model id |
| `--hide-reasoning` | no | off | Do not print reasoning |
| prompt (positional) | no | a question about TCP slow start | The user message |

Node 20.6 and newer can load a `.env` file directly: `node --env-file=../.env streaming-fetch/main.mjs`.

## Run

From the `javascript/` folder:

```bash
node streaming-fetch/main.mjs "Explain TCP slow start in one paragraph."
```

## Expected output

From the mock server (canned text, not model output). `[mock:queue]` makes the mock send two queue
notices before the reply:

```text
$ node streaming-fetch/main.mjs "[mock:queue] Explain TCP slow start."
[queue] queued · position 2 of 3 · waiting 1s
[queue] queued · position 1 of 3 · waiting 2s
[reasoning] Mock reasoning for glm-5.3-flash.
[/reasoning]
Mock reply from glm-5.3-flash: [mock:queue] Explain TCP slow start.
[usage] prompt=9 completion=25 total=34
```

## Error handling

- HTTP errors: one line with status, `type` or `code`, message and extra fields (`resets_at`,
  `estimated_tokens`, `context_cap`, `bound_by`, `model`), plus a hint. Exit status 1.
- Error event mid-stream: what arrived is printed, then the error; `preempted` means retry in about
  a minute. Exit status 1.
- Network failure or a stalled stream: exit status 1 with the cause.
- This example does not retry. For a retry policy, see [../text-processing](../text-processing).
