# Streaming (Python)

Print a reply as it is generated, with every MolochAI stream detail handled.

## What it does

- Calls `POST /chat/completions` with `stream=True` through the official `openai` SDK.
- Writes `delta.content` to stdout as it arrives and `delta.reasoning_content` (a MolochAI
  extension) to stderr. `--hide-reasoning` drops the reasoning.
- Reads usage from the last chunk, which has `"choices": []`. The loop checks for an empty
  `choices` list before indexing.
- Handles an error event inside the stream. Once the stream is open the HTTP status is already 200,
  so an upstream failure arrives as `data: {"error": {"type": "upstream_timeout", ...}}`. The SDK
  raises it as `openai.APIError` (not `APIStatusError`) with `body` set to the inner object. The
  example prints what arrived, the error type and advice (`preempted` means retry in about a minute).
- Removes the cap notice. When a reply stops at the plan's output cap, the gateway appends
  `\n\n[Response is too large for your current plan. Try a smaller request.]` to the content. It
  arrives as ordinary deltas and can be split across chunks, so `CapNoticeFilter` holds back any
  tail that could be the start of the notice and drops it if the stream ends with it.
- Reports `finish_reason: "length"` on stderr.

The OpenAI SDK drops SSE comment lines, so the queue notices the gateway sends while a request waits
for a slot (`: queued · position 2 of 3 · waiting 4s`) are not visible here. See
[../../javascript/streaming-fetch](../../javascript/streaming-fetch) for a parser that shows them.

## Dependencies

`openai` (pinned in [../requirements.txt](../requirements.txt)). Python 3.10 or newer.

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
| `MOLOCHAI_API_KEY` | yes | | API key, or `mock-key` for the mock server |
| `MOLOCHAI_BASE_URL` | no | `https://api.molochai.dev/api/v1` | API base URL |
| `MOLOCHAI_MODEL` / `--model` | no | `glm-5.3-flash` | Model id |
| `--hide-reasoning` | no | off | Do not print reasoning |
| `prompt` (positional) | no | a question about TCP slow start | The user message |

The client timeout is 60 seconds. While streaming that is the longest silence allowed between
bytes; the gateway sends a keep-alive comment about once a second, so a stalled connection shows up
well inside it.

## Run

```bash
python python/streaming/main.py "Explain TCP slow start in one paragraph."
```

## Expected output

From the mock server (canned text, not model output):

```text
$ python python/streaming/main.py "Explain TCP slow start in one paragraph."
[reasoning] Mock reasoning for glm-5.3-flash.
[/reasoning]
Mock reply from glm-5.3-flash: Explain TCP slow start in one paragraph.
[usage] prompt=10 completion=26 total=36
```

The mock can also trigger the error paths: put `[mock:timeout]`, `[mock:preempted]` or
`[mock:cap]` in the prompt. The smoke tests in [../../scripts/run-all.py](../../scripts/run-all.py)
do exactly that.

## Error handling

- Errors before the stream opens (401, 402, 413, 422, 429, 503 and so on) are raised by `create()`
  as `APIStatusError` and printed with the same helper as [../quickstart](../quickstart). Exit status 1.
- An error event inside the stream ends with exit status 1 and the message
  `the reply above is incomplete`. Retry the request; for `preempted`, wait about a minute.
- A dropped connection mid-stream surfaces as `APIConnectionError` and is handled the same way.
- A capped reply exits 0, because the text up to the cap is valid output; the note on stderr says
  it is truncated.
- `stream_options={"include_usage": True}` is sent for portability: MolochAI always sends usage and
  drops the parameter, while other OpenAI-compatible servers need it to send usage at all.
