# Code explainer (Python)

Explain a source file passed on the command line, streaming the explanation to the terminal.

## What it does

- Reads the file (UTF-8, up to 400 KB as a local sanity limit) and sends it whole in a fenced
  code block, tagged with a language guessed from the extension.
- Uses a system prompt that asks for a short summary, a walk through the main parts in execution
  order, and any bugs or risks. `--question` asks something specific instead.
- Streams the answer to stdout. Reasoning is hidden unless `--show-reasoning` is given (then it
  goes to stderr).
- Holds back the last few characters while streaming so the cap notice the gateway appends at the
  output cap never reaches the terminal.

The server decides whether the file fits, not a local guess: if it does not, the gateway answers
413 `context_length_exceeded` with `estimated_tokens`, `context_cap` and `bound_by` (`plan` or
`server`), and the example prints them. For files too long for one request, chunk them as in
[../document-summarizer](../document-summarizer).

Do not send code you are not allowed to share with a third-party service.

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
| `file` (positional) | yes | | Source file to explain |
| `--question` | no | "Explain this file." | A specific question about the file |
| `--show-reasoning` | no | off | Print reasoning to stderr |

[sample_backoff.py](sample_backoff.py) is a small retry helper to try it on.

## Run

```bash
python python/code-explainer/main.py python/code-explainer/sample_backoff.py
python python/code-explainer/main.py python/code-explainer/sample_backoff.py --question "When does it stop retrying?"
```

## Expected output

From the mock server, which echoes the first 120 characters of the message instead of explaining
anything. A real model returns the explanation.

````text
$ python python/code-explainer/main.py python/code-explainer/sample_backoff.py
Mock reply from glm-5.3-flash: File: sample_backoff.py

```python
"""A small retry helper, used as sample input for the code explainer."""

from __futu
````

## Error handling

- Missing, unreadable, non-UTF-8 or oversized files: exit 1 before any request.
- 413 `context_length_exceeded`: exit 1 with the token estimate and the cap.
- An error event mid-stream: what arrived is printed, then the error, exit 1.
- Other API errors: the shared `describe_error` helper, exit 1.
