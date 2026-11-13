# Quickstart (Python)

Send one chat completion to MolochAI with the official `openai` Python SDK and print the answer.

## What it does

- Builds an `OpenAI` client from `MOLOCHAI_API_KEY` and `MOLOCHAI_BASE_URL`.
- Sends one user message to `POST /chat/completions` without streaming.
- Prints the answer to stdout. Reasoning goes to stderr, so `> answer.txt` captures only the answer.
- Reads `reasoning_content`, a MolochAI extension the SDK does not type, with `getattr`. If the serving
  server left reasoning inline as `<think>...</think>` at the start of `content` (possible on
  non-streamed replies only), it splits that off too.
- Prints token usage from `usage` to stderr. `total_tokens` includes reasoning and is what counts against quota.

Use this for short prompts. A plain request has to finish within about 100 seconds or the edge
in front of the API cuts it off (HTTP 524). For anything that can run long, use [../streaming](../streaming).

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
| `MOLOCHAI_API_KEY` | yes | | API key from the [dashboard](https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples), or `mock-key` for the mock server |
| `MOLOCHAI_BASE_URL` | no | `https://api.molochai.dev/api/v1` | API base URL |
| `MOLOCHAI_MODEL` / `--model` | no | `glm-5.3-flash` | Model id; `--model` wins |
| `prompt` (positional) | no | a question about context windows | The user message |

## Run

```bash
python python/quickstart/main.py
python python/quickstart/main.py "Name three uses for a hash map."
```

To try it without a key or quota, run it against the mock server (see
[../../local-development](../../local-development)):

```bash
python local-development/devenv.py --mock -- python python/quickstart/main.py
```

## Expected output

From the mock server, which returns canned text, not model output. With the real API the answer
and reasoning are the model's own text and token counts differ.

```text
$ python python/quickstart/main.py "Name three uses for a hash map."
[reasoning]
Mock reasoning for glm-5.3-flash.
[/reasoning]
[usage] model=glm-5.3-flash prompt=8 completion=24 total=32
Mock reply from glm-5.3-flash: Name three uses for a hash map.
```

(`[reasoning]` and `[usage]` are on stderr; only the last line is on stdout.)

## Error handling

- Every error exits with status 1 and one readable message on stderr. The `describe_error`
  helper reads the status code and the `type`/`code` field, never the message text, and handles
  all three error body shapes: gateway errors (`{"error": {"type": ...}}`), validation errors
  (422, `{"message", "errors"}`) and bare messages (401, 403, the 429 rate limit). A non-JSON body,
  such as the edge's HTML 524 page, is reported as such.
- Extra fields are shown when present: `resets_at` for quota errors, `estimated_tokens`,
  `context_cap` and `bound_by` for 413 `context_length_exceeded`, `model` for 503 `model_unavailable`.
- The SDK retries connection errors, 429 and 5xx twice on its own (`max_retries=2`). That includes
  429 quota errors, which cannot pass before `resets_at`, and 503 `model_unavailable`. For a single
  request that costs a few seconds at most; [../model-selection](../model-selection) and
  [../../javascript/text-processing](../../javascript/text-processing) turn SDK retries off and
  apply their own policy.
- `finish_reason: "length"` means the reply hit the plan's output cap. `max_tokens` is accepted but
  has no effect on MolochAI, so the fix is a smaller request.
