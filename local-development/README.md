# Local development

How to work on these examples (or your own code) without leaking a key or spending quota: keep
keys in `.env`, run against the mock server, point the examples at a self-hosted server, and stop
keys from being committed.

The commands in sections 1, 2 and 4 were run on Windows (PowerShell and Git Bash), and the mock
workflow runs on Linux in CI. Section 3 describes general differences between OpenAI-compatible
servers; it was not tested against each server named there. Commands assume the repository root
as the working directory unless they say otherwise.

## 1. Keep keys in `.env`

API keys are created at [dash.molochai.dev/account](https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples)
and shown once. They do not expire on a timer and are not scoped: a key can read account details
and create invoices, not only send chat requests. Treat it like a password, never put it in code,
and never ship it to a browser or a mobile app; keep it on a server you control.

```bash
cp .env.example .env     # then edit .env and set MOLOCHAI_API_KEY
```

`.env` (and `.env.*`, except `.env.example`) is in [../.gitignore](../.gitignore). The examples
only read environment variables, so load the file in one of these ways:

| Where | Command |
|---|---|
| Any OS, any example | `python local-development/devenv.py --env-file .env -- python python/streaming/main.py "hi"` |
| bash, zsh, Git Bash | `set -a; . ./.env; set +a` (applies to the current shell) |
| PowerShell | `foreach ($line in Get-Content .env) { if ($line -match '^\s*([A-Za-z_]\w*)\s*=\s*(.*)$') { Set-Item "env:$($Matches[1])" $Matches[2].Trim().Trim('"', "'") } }` |
| Node 20.6+ (from `javascript/`) | `node --env-file=../.env streaming-fetch/main.mjs "hi"` |

[devenv.py](devenv.py) reads `KEY=VALUE` lines, `#` comment lines and optional quotes around the
value; it does not support inline comments, multi-line values or `${VAR}` expansion. Variables that
are already set in the environment win over the file.

If a key leaks (pushed, pasted, logged), revoke it in the dashboard right away and create a new one.

## 2. Run against the mock server instead of the API

The mock server lives in [uncensored-ai-api](https://github.com/Moloch-AI/uncensored-ai-api)
(`mock-server/server.mjs`, Node.js 20+, no dependencies). It answers the same routes and error
shapes as the API with canned text, so nothing you run against it uses quota. Its replies are not
model output.

Clone it next to this repository:

```text
some-folder/
  uncensored-llm-examples/   (this repository)
  uncensored-ai-api/         (git clone https://github.com/Moloch-AI/uncensored-ai-api)
```

Then either let [devenv.py](devenv.py) start and stop it around one command (it picks a free port
and sets `MOLOCHAI_BASE_URL` and `MOLOCHAI_API_KEY=mock-key` for that command only):

```bash
python local-development/devenv.py --mock -- python python/quickstart/main.py
python local-development/devenv.py --mock --mock-args="--unavailable glm-5.3-flash" -- python python/model-selection/main.py
```

or start it yourself in a second terminal and point your shell at it:

```bash
node ../uncensored-ai-api/mock-server/server.mjs --port 4010
```

```bash
export MOLOCHAI_BASE_URL=http://127.0.0.1:4010/api/v1   # PowerShell: $env:MOLOCHAI_BASE_URL = "http://127.0.0.1:4010/api/v1"
export MOLOCHAI_API_KEY=mock-key                         # PowerShell: $env:MOLOCHAI_API_KEY = "mock-key"
```

Set `MOLOCHAI_MOCK_SERVER` if the mock lives somewhere else (relative paths are resolved from the
repository root).

Useful mock behavior (see the [mock's documentation](https://github.com/Moloch-AI/uncensored-ai-api)
for the full list):

- Tokens `mock-no-plan` and `mock-unverified` produce 402 `no_active_plan` and 403
  `email_unverified`; an empty token produces 401.
- A marker in the last user message triggers an error path: `[mock:rate-limit]`, `[mock:quota]`,
  `[mock:context]`, `[mock:busy]`, `[mock:unavailable]`, `[mock:timeout]` (in-stream error when
  streaming), `[mock:preempted]`, `[mock:flaky=N]`, `[mock:cloudflare-524]`, `[mock:queue]`
  (queue notices), `[mock:cap]` (cap notice), `[mock:inline-think]`.
- A fenced ```` ```json ```` block holding a JSON Schema with `properties` makes it answer with a
  placeholder instance of that schema; with `tools`, it calls the first tool with placeholder
  arguments.

To run every example against it, with the expected output checked:

```bash
python scripts/run-all.py
```

## 3. Point the examples at a self-hosted server

The examples use only `POST /chat/completions` and `GET /models` from the OpenAI-compatible
surface, so any server that implements those (vLLM, llama.cpp's `llama-server`, Ollama and others)
works by changing three variables:

```bash
export MOLOCHAI_BASE_URL=http://127.0.0.1:8000/v1   # wherever your server listens, ending in /v1
export MOLOCHAI_MODEL=the-model-name-your-server-uses
export MOLOCHAI_API_KEY=anything                     # many local servers ignore it, but it must be non-empty
```

What differs from MolochAI, and how the examples cope:

| MolochAI behavior | On another server |
|---|---|
| `GET /api/auth/me` with per-model context limits | missing; [document-summarizer](../python/document-summarizer) falls back to 8,192 tokens (or use `--chunk-tokens`) |
| `name` and `available` on `GET /models` | missing; [model-selection](../python/model-selection) treats every listed model as available |
| usage always sent at the end of a stream | sent only with `stream_options.include_usage`, which the streaming examples send |
| reasoning in `reasoning_content` while streaming | depends on the server and its flags; it may arrive inline as `<think>...</think>`, which the JSON examples strip |
| cap notice and `finish_reason: "length"` at the plan's cap | no cap notice; `length` when the server's own limit is hit |
| tool calls arrive whole | may arrive in fragments; [tool-calling](../python/tool-calling) joins them |
| error bodies described in the API docs | other shapes; the examples fall back to the HTTP status |

Running open-weight models yourself means their licenses and your own use policy apply; each
model's card has the details. Apps that face end users need their own rules and moderation either
way.

## 4. Stop keys from being committed

[check_secrets.py](check_secrets.py) (standard library only) refuses `.env` files, literal values
of 12 or more characters assigned to `MOLOCHAI_API_KEY` / `OPENAI_API_KEY`, and long literal
tokens sent as `Authorization: Bearer ...`.
Known placeholders (`your-api-key`, `mock-key`) and references (`$MOLOCHAI_API_KEY`,
`${{ secrets.X }}`, `process.env...`) pass. It does not know every key format; pair it with a
dedicated scanner such as [gitleaks](https://github.com/gitleaks/gitleaks) if you need more.

Install it as a git pre-commit hook (bash, or Git Bash on Windows; git runs hooks with its own
shell on every OS):

```bash
printf '#!/bin/sh\nexec python local-development/check_secrets.py\n' > .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

From then on `git commit` checks the staged content and stops when it finds something:

```text
.env: .env files hold real keys and must not be committed
.env:1: API key variable set to a literal value
check_secrets: possible secrets found; move keys to .env (git ignores it) or mark a false positive with 'secret-check: allow'
```

CI runs the same check over every tracked file:

```bash
python local-development/check_secrets.py --all
```
