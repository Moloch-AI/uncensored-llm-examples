# Model selection (Python)

List the model catalog, pick a model that is being served, and fall back to another one on 503
`model_unavailable` without retry loops.

## What it does

- Calls `GET /models`. Besides the OpenAI fields, each entry has two MolochAI extensions: `name`
  (display name) and `available` (a server is serving it right now). The table goes to stderr.
- Builds a candidate list: `--model` first, then `--fallbacks` (or, by default, every other model
  the listing marks available). Ids that are not in the catalog are skipped (they would be a 422),
  and so are ids marked `available: false`.
- Tries each candidate once. On 503 with `type: "model_unavailable"` it moves to the next one;
  any other error stops with a message. The gateway never reroutes a request to a different model
  on its own, and `model` in the response is always the id you asked for.
- The client uses `max_retries=0`. With the SDK default, each unavailable model would be retried
  twice before this code could move on.

The listing is a snapshot: a model marked available can stop being served before the request
arrives. That is the case the 503 handling covers. The catalog is not hardcoded anywhere; it can
change, and so can the build behind an id. See [molochai.dev/models](https://molochai.dev/models?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)
for model pages, and each base model's own card for what it can do.

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
| `MOLOCHAI_MODEL` / `--model` | no | `glm-5.3-flash` | Preferred model id |
| `--fallbacks` | no | every other available model | Comma-separated ids to try next, in order |
| `prompt` (positional) | no | a one-sentence question | The user message |

## Run

```bash
python python/model-selection/main.py
python python/model-selection/main.py --model qwen3.8-27b-uncensored --fallbacks glm-5.3-flash
```

## Expected output

From the mock server (canned text, not model output):

```text
$ python python/model-selection/main.py --model qwen3.8-27b-uncensored --fallbacks glm-5.3-flash
id                                     name                                       available
glm-5.3-flash                          GLM-5.3-Flash                              yes
glm-4.7-flash-uncensored               GLM-4.7-Flash-Uncensored                   yes
gemma4-26b-a4b-qat                     Gemma4-26B-A4B-QAT                         yes
qwen3.8-27b-uncensored                 Qwen3.8-27B-Uncensored                     yes
llama-3.3-70b-instruct-abliterated     Llama-3.3-70B-Instruct-abliterated-GGUF    yes
[answered by] qwen3.8-27b-uncensored
Mock reply from qwen3.8-27b-uncensored: Reply with one short sentence about tides.
```

To see the fallback path, start the mock with `--unavailable glm-5.3-flash` (the example then
skips that model), or put `[mock:unavailable]` in the prompt (every model answers 503, each is
tried once, and the example exits 1).

## Error handling

- `GET /models` sits behind the quota check: with no plan and no credit it returns 402
  `no_active_plan`, which the example reports and exits 1.
- 503 `model_unavailable`: next candidate, no retry on the same model. When every candidate fails,
  exit 1 with `no candidate model could serve the request`.
- Anything else (401, 402, 403, 413, 429, 5xx, connection errors) stops with exit 1. This example
  is about choosing a model; [../../javascript/text-processing](../../javascript/text-processing)
  shows a retry policy for `server_busy`, 502, 504 and rate limits.
- On a server that is not MolochAI, `available` is missing; the example treats every listed model
  as available.
