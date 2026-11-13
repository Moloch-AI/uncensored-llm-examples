# Tool calling (Python)

A complete tool loop with two safe local tools, argument validation and a step limit.

## What it does

1. Sends the conversation with `tools` (OpenAI format) and streams the reply.
2. Joins tool-call fragments by `index`. MolochAI sends each call's `arguments` whole in one chunk,
   but other OpenAI-compatible servers split them, so the code accumulates either way (the same
   way the SDK's own accumulator does).
3. When the model asks for tools, parses each call's `arguments` (a JSON string), validates it
   against that tool's own JSON Schema with `jsonschema`, runs the tool and appends a
   `role: "tool"` message with the matching `tool_call_id`.
4. Repeats until the model answers in text, or stops with exit status 1 after `--max-steps`
   model calls.

The tools only compute: `convert_units` (km, m, mi, ft, kg, lb) and `calculate`, which evaluates
arithmetic by walking Python's syntax tree (no `eval`, no names or calls, bounded exponents and
integer sizes). Nothing the model asks for can read files, open connections or run commands.

Tool errors (invalid JSON, schema violations, division by zero, an unknown tool name) go back to
the model as the tool result, `{"error": "..."}`, so it can correct itself. They count toward the
step limit; they do not crash the loop.

When the assistant turn has `reasoning_content`, it is sent back on that assistant message.
MolochAI accepts it there, which keeps the model's reasoning across the tool round trip.

## Dependencies

`openai` and `jsonschema` (pinned in [../requirements.txt](../requirements.txt)). Python 3.10 or newer.

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
| `--max-steps` | no | 6 | Model calls allowed before giving up |
| `prompt` (positional) | no | a distance and pace question | The user message |

Tool support depends on the model and the server serving it; check the model's card on
[molochai.dev/models](https://molochai.dev/models?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples).

## Run

```bash
python python/tool-calling/main.py
```

## Expected output

From the mock server. The mock always calls the first tool with placeholder arguments built from
its schema (numbers `1`, an enum its first value), then answers with a canned line that quotes the
tool result. A real model picks tools and arguments from the question.

```text
$ python python/tool-calling/main.py
[step 1] convert_units({"value":1,"from_unit":"km","to_unit":"km"}) -> {"value": 1, "from_unit": "km", "to_unit": "km", "result": 1.0}
[done] 2 model call(s)
Mock reply from glm-5.3-flash after tool convert_units: {"value": 1, "from_unit": "km", "to_unit": "km", "result": 1.0}
```

Tool activity goes to stderr; the final answer goes to stdout.

## Error handling

- `--max-steps` guards against a model that keeps calling tools: exit 1 with
  `no final answer after N model calls`.
- API errors (before or during the stream) are reported with the shared `describe_error` helper
  and exit 1.
- Adding a tool: add its schema to `TOOLS` and its function to `IMPLEMENTATIONS`. Keep tools that
  touch files, networks or shells out of a loop driven by model output unless you control and
  sandbox what they can reach, and treat tool arguments as untrusted input.
