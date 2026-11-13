# Contributing

Thanks for helping. This repository is a set of small programs people copy from, so each one has to
run, be correct about how the MolochAI API behaves, and be easy to read.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r python/requirements-dev.txt
cd javascript && npm ci && cd ..
cd typescript && npm ci && cd ..
git clone https://github.com/Moloch-AI/uncensored-ai-api ../uncensored-ai-api   # the mock server
```

Node.js 22 or newer and Python 3.10 or newer.

## Checks (all run in CI)

```bash
ruff check . && ruff format --check .
cd javascript && npx biome ci . && cd ..
cd typescript && npx biome ci . && npx tsc --noEmit && cd ..
python local-development/check_secrets.py --all
python scripts/run-all.py
```

`ruff format .` and `npx biome format --write .` fix formatting.

## Adding an example

1. Pick a task people actually have, that no existing example already teaches. Put it in
   `python/<name>/`, `javascript/<name>/` or `typescript/<name>/`, with a `main.py`, `main.mjs` or
   `main.ts` and a `README.md`.
2. Keep it self-contained: one file you can copy out. The small client and error helpers are
   repeated on purpose (in Python, the block under "Boilerplate shared (as a copy)"); copy them
   rather than importing across examples.
3. Follow the shared conventions:
   - read `MOLOCHAI_API_KEY` (required) and `MOLOCHAI_BASE_URL` (default
     `https://api.molochai.dev/api/v1`); default model `glm-5.3-flash`, overridable with `--model`
     and `MOLOCHAI_MODEL`;
   - exit non-zero with one clear message on stderr for every error; answers go to stdout,
     progress and diagnostics to stderr;
   - stream whenever a reply could be long (plain requests are cut off at about 100 seconds);
   - check the HTTP status first, never assume an error body is JSON, branch on `type`, `code`,
     `resets_at` and headers rather than message text;
   - do not retry 401, 402, 403, 413, 422 or 429 quota errors, and make sure your retries do not
     stack on top of the SDK's own;
   - never hardcode the model list as the source of truth; read `GET /models`;
   - if the API does not support something, say so in the README instead of faking it.
4. Add the dependency, if any, to the one file for that language (`python/requirements.txt`,
   `javascript/package.json` or `typescript/package.json`) with an exact version, and commit the
   updated `package-lock.json`.
5. Write the README with these sections, in this order:
   - title and a one-sentence description
   - **What it does**
   - **Dependencies** (and which file pins them)
   - **Install**
   - **Configuration** (a table of environment variables and flags)
   - **Run** (commands you actually ran)
   - **Expected output** (real output; say when it comes from the mock server, whose replies are
     canned text)
   - **Error handling**
6. Add at least one case to `cases()` in [scripts/run-all.py](scripts/run-all.py) for the normal
   path, and one per error path the example claims to handle. The mock's markers
   (`[mock:rate-limit]`, `[mock:timeout]`, `[mock:cap]` and the rest, listed in
   [local-development](local-development/README.md)) and its special tokens make those paths
   reproducible. A case passes only with the expected exit code, non-empty output, and the
   expected snippets.
7. Add a row to the examples table in [README.md](README.md).

## Writing style

Plain and specific, like an engineer explaining a system to another engineer. Describe the models
as less-restricted or as having reduced refusal behavior; do not call any model the best at
anything, and attribute model facts to the model's own card. No marketing words, no invented
numbers. Link to `molochai.dev` pages with the repository's UTM parameters, as the existing READMEs do.

## Never commit

Real API keys, `.env` files, or output from real API calls that contains private data. The secret
check above catches the common cases; it is not a guarantee.

## Commits and pull requests

Small, focused pull requests with a clear description of what changed and how you tested it. CI
must pass.
