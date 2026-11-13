# JSON output (Python)

Get JSON that matches a schema from a model on an API with no JSON mode: schema in the prompt,
lenient extraction, validation, and one repair round.

## What it does

MolochAI has no JSON mode. `response_format` is accepted and silently dropped, so sending it
changes nothing. This example does what works instead:

1. Puts the JSON Schema in the system message, inside a fenced ```` ```json ```` block, and asks for
   one JSON object and nothing else.
2. Streams the reply and collects it (a reasoning model can think for a long time before it
   answers, and a plain request that runs past about 100 seconds is cut off at the edge).
3. Extracts the first JSON object from the reply, whether it came bare, inside a code fence, or
   after some prose. Inline `<think>` blocks are dropped first.
4. Validates it with [`jsonschema`](https://python-jsonschema.readthedocs.io/) (Draft 2020-12).
5. If validation fails, sends the reply back with the list of problems
   (`at $.rating: 7 is greater than the maximum of 5`) and asks once for a corrected object.
   If the second reply is still invalid, it gives up with exit status 1.

The default input is a short product review and the default schema extracts `product`,
`sentiment`, `rating`, `pros` and `cons`. Pass your own with `--input` and `--schema`.

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
| `--input FILE` | no | a built-in review | UTF-8 text to extract from |
| `--schema FILE` | no | a built-in review schema | JSON Schema file (Draft 2020-12) |

## Run

```bash
python python/json-output/main.py
```

## Expected output

From the mock server. The mock recognizes a fenced `json` block that holds a JSON Schema and
replies with a placeholder instance of it (strings become `"example"`, numbers `1`, an enum its
first value), so these values are not extracted from the review. A real model reads the text.

```text
$ python python/json-output/main.py
{
  "product": "example",
  "sentiment": "positive",
  "rating": 1,
  "pros": [
    "example"
  ],
  "cons": [
    "example"
  ]
}
```

## Error handling

- Invalid replies: one repair round, then exit 1 with the validation problems on stderr. The
  smoke tests cover this path with a schema the mock's placeholder cannot satisfy
  (`"minLength": 20` on a string).
- An invalid `--schema` file is rejected before any request is made.
- API errors are reported with the shared `describe_error` helper and exit 1.
- If the reply hits the output cap, the JSON is usually cut off and fails validation; the cap
  notice is stripped before parsing and a note is printed. Ask for less, or split the input.
- Validation proves the shape, not the truth: a schema-valid reply can still contain wrong values.
