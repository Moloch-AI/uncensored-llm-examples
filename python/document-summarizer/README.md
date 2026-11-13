# Document summarizer (Python)

Summarize a text file longer than one request can hold: size chunks from the model's context
limit, summarize each chunk, then merge the summaries.

## What it does

1. Reads the context limit for the chosen model from `GET /api/auth/me`. That endpoint is
   MolochAI-specific and lives outside `/api/v1`: the example derives it by replacing the trailing
   `/v1` of the base URL with `/auth/me`. The per-model `context_limit` is the smaller of the
   plan's and the serving server's limit. On a server that is not MolochAI (no `/v1` suffix, 404,
   non-JSON), it falls back to a conservative 8,192 tokens and says so on stderr.
2. Sets the chunk size to the context limit minus room for the reply (reasoning tokens count
   against the context too), capped at 16,000 tokens, and splits the file on paragraph boundaries.
3. Map: summarizes each chunk. Reduce: merges the partial summaries, in more than one round if
   they do not fit in one request. The final summary streams to stdout; progress goes to stderr.

Token counts are estimated as characters / 3, which overestimates for English prose. Text in
scripts such as Chinese or Japanese can use more tokens per character than that; if a chunk is
still too large, the gateway answers 413 `context_length_exceeded` and the example splits that
chunk in two once before giving up. `--chunk-tokens` overrides the computed size.

Every request streams, even the ones whose output is only collected: long inputs mean long
thinking, and a plain request that runs past about 100 seconds is cut off at the edge.

## Dependencies

`openai` (pinned in [../requirements.txt](../requirements.txt)). `GET /api/auth/me` is called with
the standard library (`urllib`), since the OpenAI SDK has no method for it. Python 3.10 or newer.

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
| `file` (positional) | yes | | UTF-8 text file to summarize |
| `--chunk-tokens` | no | computed | Chunk size in estimated tokens |

[sample.md](sample.md) is a short article on rate-limiting algorithms written for this example.

## Run

```bash
python python/document-summarizer/main.py python/document-summarizer/sample.md
python python/document-summarizer/main.py python/document-summarizer/sample.md --chunk-tokens 300
```

The second command forces small chunks so the map and reduce steps run on a short file.

## Expected output

From the mock server, which echoes the start of the last message instead of summarizing. A real
model returns an overview and bullet points.

```text
$ python python/document-summarizer/main.py python/document-summarizer/sample.md --chunk-tokens 300
[summarizer] 5024 characters -> 7 chunk(s) of up to ~300 tokens
[summarizer] map 1/7
[summarizer] map 2/7
[summarizer] map 3/7
[summarizer] map 4/7
[summarizer] map 5/7
[summarizer] map 6/7
[summarizer] map 7/7
[summarizer] reduce round 1: 7 summaries -> 2
[summarizer] reduce (final)
Mock reply from glm-5.3-flash: Mock reply from glm-5.3-flash: Mock reply from glm-5.3-flash: # Rate limiting: four common algorithms and their trade-of
```

Without `--chunk-tokens`, the first line reads
`[summarizer] context limit for glm-5.3-flash: 65536 tokens` (the mock's value) and the file fits
in one chunk.

## Error handling

- 413 `context_length_exceeded`: the chunk is split in two once; a second 413 stops with exit 1
  and suggests a smaller `--chunk-tokens`.
- Any other API error stops with exit 1. Requests run one after another, which keeps a long file
  well under the 60 requests/minute limit on most inputs; for parallel requests with pacing, see
  [../../javascript/text-processing](../../javascript/text-processing).
- A summary is only as reliable as the model: check summaries of documents that matter against
  the source.
