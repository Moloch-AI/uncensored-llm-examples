# MolochAI examples

[![CI](https://github.com/Moloch-AI/uncensored-llm-examples/actions/workflows/ci.yml/badge.svg)](https://github.com/Moloch-AI/uncensored-llm-examples/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Task-oriented, runnable examples in Python, JavaScript and TypeScript for the MolochAI API, built on
the official OpenAI SDKs, plus integrations with LangChain, LiteLLM and the Vercel AI SDK.

## Overview

[MolochAI](https://molochai.dev/?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)
is an OpenAI-compatible API that serves open-weight models with reduced refusal behavior. "Uncensored"
is the word people search for; what it means here is that refusal behavior is reduced. The models
still sometimes decline, they are not more accurate for it, the
[Acceptable Use Policy](https://molochai.dev/acceptable-use?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)
still applies, and apps that face end users need their own rules and moderation.

Each example solves one task end to end (streaming a reply, choosing a model, getting valid JSON,
running a tool loop, summarizing a long document, batch processing under a rate limit, a terminal
chat) and handles the parts of this API that differ from OpenAI's: reasoning in
`reasoning_content`, usage in a final chunk with empty `choices`, error events inside an open
stream, the cap notice appended at the output cap, three error body shapes, no JSON mode, and the
roughly 100-second edge timeout on requests that do not stream.

Every example also runs against the mock server from
[uncensored-ai-api](https://github.com/Moloch-AI/uncensored-ai-api), so you can try them, and CI can
test them, without a key or quota.

```text
example --- MOLOCHAI_BASE_URL ---+--> https://api.molochai.dev/api/v1   real API (key, uses quota)
                                 +--> http://127.0.0.1:<port>/api/v1    mock server (canned replies)
                                 +--> http://<your-server>/v1           self-hosted OpenAI-compatible server
```

The examples use the OpenAI SDKs because that is the documented integration path, and they remain
a fine choice. The MolochAI SDKs ([Python](https://github.com/Moloch-AI/uncensored-ai-python),
[Node](https://github.com/Moloch-AI/uncensored-ai-node)) additionally keep SSE comment lines (queue
progress), turn the error bodies into typed errors with `resets_at` and `context_cap`, do not retry
quota errors, read per-model context limits from `/api/auth/me`, and deal with in-stream errors
and the cap notice for you. Those packages are not published to PyPI or npm yet; install them from
GitHub as their READMEs describe.

## Examples

| Example | Language | What it teaches |
|---|---|---|
| [python/quickstart](python/quickstart) | Python | One chat completion with the OpenAI SDK; `reasoning_content` and inline `<think>`; reading every error shape |
| [python/streaming](python/streaming) | Python | Content to stdout, reasoning to stderr, usage from the empty-`choices` chunk, in-stream error events, removing the cap notice, `finish_reason: "length"` |
| [python/model-selection](python/model-selection) | Python | `GET /models` with `available`; falling back to another model on 503 `model_unavailable`, once per model |
| [python/json-output](python/json-output) | Python | JSON without a JSON mode: schema in the prompt, lenient extraction, `jsonschema` validation, one repair round |
| [python/tool-calling](python/tool-calling) | Python | A full tool loop: streamed tool calls, argument validation against each tool's schema, safe local tools, a step limit |
| [python/document-summarizer](python/document-summarizer) | Python | Context limits from `GET /api/auth/me`, chunking, map-reduce, a fallback for other servers, handling 413 |
| [python/code-explainer](python/code-explainer) | Python | Streaming an explanation of a source file; what a 413 tells you |
| [javascript/streaming-fetch](javascript/streaming-fetch) | JavaScript, no SDK | The wire format: `fetch`, a hand-written SSE parser, comment lines and queue notices, in-stream errors |
| [javascript/text-processing](javascript/text-processing) | JavaScript | Batch processing with a concurrency limit, pacing under 60 requests/minute, `Retry-After`, a retry policy per error type |
| [typescript/terminal-chat](typescript/terminal-chat) | TypeScript | Multi-turn chat with history, streaming, `/model` and `/reset`, sending `reasoning_content` back |
| [typescript/structured-extraction](typescript/structured-extraction) | TypeScript | One zod schema for the prompt, the validation and the static type |
| [python/langchain](python/langchain) | Python, LangChain | `ChatOpenAI` with invoke, streaming and `bind_tools`; why `with_structured_output` needs `method="function_calling"` here; keeping `reasoning_content`; retries that do not stack |
| [python/litellm](python/litellm) | Python, LiteLLM | `completion`/`acompletion` with `openai/<model id>`; which LiteLLM exception each error becomes; no network at import; no blind retries |
| [typescript/vercel-ai-sdk](typescript/vercel-ai-sdk) | TypeScript, Vercel AI SDK | `generateText`, `streamText` and a tool with `@ai-sdk/openai-compatible`; reasoning; in-stream errors that do not throw |
| [local-development](local-development) | Python (tooling) | Keys in `.env`, running against the mock, switching to a self-hosted server, a pre-commit secret check |

Each example folder has its own README: what it does, dependencies, install, configuration, how to
run it, sample output (labeled when it comes from the mock) and error handling notes. The three
framework READMEs also say which behavior was verified against the mock and with which versions.

## Using it from other tools

Apps that you configure rather than program cannot be tested here, so their setup lives on the
website instead of in this repository:

- [All integrations](https://molochai.dev/integrations?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)
- [SillyTavern](https://molochai.dev/integrations/sillytavern?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)
- [Open WebUI](https://molochai.dev/integrations/open-webui?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)
- [LibreChat](https://molochai.dev/integrations/librechat?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)
- [Coding agents](https://molochai.dev/integrations/coding-agents?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)

Any other tool that speaks the OpenAI chat completions API needs the base URL, a key and a model id.
Before relying on a parameter, check the
[OpenAI compatibility matrix](https://molochai.dev/docs/openai-compatibility?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples),
which lists the supported, ignored and unavailable parameters.

## Requirements

- Python 3.10 or newer for the Python examples and the scripts (CI uses 3.12).
  [python/requirements.txt](python/requirements.txt) includes LangChain and LiteLLM, which make
  the install noticeably larger; the other Python examples need only `openai` and `jsonschema`.
- Node.js 22 or newer for the SDK-based JavaScript and TypeScript examples (`openai` 7.x and the
  Vercel AI SDK packages require it). [javascript/streaming-fetch](javascript/streaming-fetch)
  alone runs on Node.js 20.
- For offline runs and the smoke tests: a clone of
  [uncensored-ai-api](https://github.com/Moloch-AI/uncensored-ai-api) next to this repository.
- For the real API: a key from [dash.molochai.dev/account](https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)
  and an active plan or credit ([pricing](https://molochai.dev/pricing?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)).

## Installation

Dependencies are pinned once per language: [python/requirements.txt](python/requirements.txt),
[javascript/package.json](javascript/package.json), [typescript/package.json](typescript/package.json).

```bash
git clone https://github.com/Moloch-AI/uncensored-llm-examples
git clone https://github.com/Moloch-AI/uncensored-ai-api        # the mock server, for offline runs
cd uncensored-llm-examples

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r python/requirements-dev.txt   # runtime deps plus ruff; requirements.txt alone is enough to run

cd javascript && npm ci && cd ..
cd typescript && npm ci && cd ..
```

## Quickstart

Without a key, against the mock server (this starts it on a free port, runs one command, stops it):

```bash
python local-development/devenv.py --mock -- python python/quickstart/main.py
```

With a key, against the API:

```bash
cp .env.example .env               # set MOLOCHAI_API_KEY in .env
python local-development/devenv.py --env-file .env -- python python/streaming/main.py "Explain TCP slow start in one paragraph."
```

More ways to load `.env` (bash, PowerShell, `node --env-file`) are in
[local-development](local-development#1-keep-keys-in-env).

## Run everything against the mock

```bash
python scripts/run-all.py          # all examples; -k <text> to filter, -v to print output, --list
```

[scripts/run-all.py](scripts/run-all.py) starts the mock server, runs every example with sample
inputs (67 cases, including the error paths: rate limits, quota, in-stream errors, the cap notice,
503 fallback, JSON repair, the tool step limit, and each framework's error mapping and retries),
and fails if a case exits with an unexpected code, prints nothing, or misses its expected output. It finds the mock through `MOLOCHAI_MOCK_SERVER`
(default `../uncensored-ai-api/mock-server/server.mjs`, relative to the repository root).

Linting, as in CI:

```bash
ruff check . && ruff format --check .
cd javascript && npx biome ci . && cd ..
cd typescript && npx biome ci . && npx tsc --noEmit && cd ..
```

## Configuration

Every example reads the same variables:

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `MOLOCHAI_API_KEY` | yes | | API key. `mock-key` for the mock server |
| `MOLOCHAI_BASE_URL` | no | `https://api.molochai.dev/api/v1` | API base URL: the API, the mock, or another OpenAI-compatible server |
| `MOLOCHAI_MODEL` | no | `glm-5.3-flash` | Model id when `--model` is not given |
| `MOLOCHAI_MOCK_SERVER` | no | `../uncensored-ai-api/mock-server/server.mjs` | Mock server script, for `scripts/run-all.py` and `devenv.py --mock` |

Model ids as of 2026-11-07: `glm-5.3-flash`, `glm-4.7-flash-uncensored`, `gemma4-26b-a4b-qat`,
`qwen3.8-27b-uncensored`, `llama-3.3-70b-instruct-abliterated`. The list can change and so can the
build behind an id, so code should read `GET /models` (see
[python/model-selection](python/model-selection)); the current list and each base model's card are
linked from [molochai.dev/models](https://molochai.dev/models?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples).

## API reference

The full documentation is at [molochai.dev/docs](https://molochai.dev/docs?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples).
The OpenAPI spec, curl examples and dependency-free reference clients are in
[uncensored-ai-api](https://github.com/Moloch-AI/uncensored-ai-api). Points the examples rely on:

- Three endpoints: `POST /chat/completions`, `GET /models`, and `GET /api/auth/me` (outside `/api/v1`).
- `max_tokens` is accepted but has no effect (the plan's output cap applies), and `response_format`,
  `stop`, `n`, `seed` and the penalty parameters are dropped without an error.
- Rate limit: 60 requests per minute per account, with `Retry-After` on 429.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `MOLOCHAI_API_KEY is not set` | Export it, load `.env` (see [local-development](local-development)), or use `devenv.py --mock` |
| HTTP 401 | Key missing, mistyped or revoked |
| HTTP 402 `no_active_plan` | No active plan and no credit on the account |
| HTTP 403 `email_unverified` | Confirm the account's email address |
| HTTP 413 `context_length_exceeded` | Prompt larger than the context limit (`bound_by` says whether the plan or the server set it); shorten or chunk it |
| HTTP 429 with `resets_at` | Quota or 5-hour session cap; retrying before `resets_at` will not help |
| HTTP 503 `model_unavailable` | No server is serving that model right now; pick another (see [python/model-selection](python/model-selection)) |
| HTTP 524 with an HTML body | A plain request ran past the edge timeout; use a streaming example |
| `stream error upstream_timeout` / `preempted` | The stream failed after it started; retry (for `preempted`, after about a minute) |
| `UnicodeEncodeError: 'charmap' codec` on Windows when redirecting output | Set `PYTHONUTF8=1` (PowerShell: `$env:PYTHONUTF8 = "1"`) so Python writes UTF-8 |
| `npm ci` warns about the engine, or the SDK fails on Node 20 | `openai` 7.x and the Vercel AI SDK need Node.js 22 or newer |
| LangChain `with_structured_output` fails with `Invalid JSON` or missing fields | Its default method relies on `response_format`, which MolochAI drops; use `method="function_calling"` (see [python/langchain](python/langchain)) |
| LiteLLM on Windows logs `cl100k_base.tiktoken` download errors when offline | A packaging issue in LiteLLM's Windows wheel; run one streamed request online once (see [python/litellm](python/litellm)) |
| `mock server not found` | Clone uncensored-ai-api next to this repository or set `MOLOCHAI_MOCK_SERVER` |

For problems with the hosted API, use [molochai.dev/contact](https://molochai.dev/contact?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples).

## Security considerations

- API keys are not scoped: a key can read account details and create invoices. Keep it in `.env` or
  a secret store, never in code, and never in a browser or mobile app. These examples are
  server-side or command-line programs for that reason.
- [local-development/check_secrets.py](local-development/check_secrets.py) can run as a pre-commit
  hook; CI runs it over every tracked file.
- Prompts, files and documents you send go to the API. Do not send what you are not allowed to share.
- Model output is untrusted input: validate JSON and tool arguments before acting on them (as
  [python/json-output](python/json-output) and [python/tool-calling](python/tool-calling) do), and
  keep tools that touch files, networks or shells out of a model-driven loop unless you sandbox them.
- Reduced refusal behavior means your application decides what is acceptable. Apps that face end
  users need their own rules and moderation, and the
  [Acceptable Use Policy](https://molochai.dev/acceptable-use?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples) applies.

To report a vulnerability in this repository, see [SECURITY.md](SECURITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add an example: the README sections it needs and
the smoke-test case it must have.

## License

[MIT](LICENSE)
