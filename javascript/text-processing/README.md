# Batch text processing (JavaScript)

Rewrite or classify every line of a file, a few requests at a time, paced under the account's rate
limit, with a retry policy that matches what the API's errors mean.

## What it does

- Sends one request per non-empty input line with the official `openai` SDK, through a small
  worker pool (`--concurrency`, default 4).
- Paces request starts with a sliding 60-second window (`--rpm`, default 50). The account limit is
  60 requests per minute and is shared by everything that uses your key, so the default leaves
  headroom.
- Writes one JSON object per line (JSONL) to stdout as results complete, each with its input line
  number. Progress goes to stderr.
- Two modes: rewrite with an instruction (default: plain, neutral English), or `--labels` to
  classify. In label mode the reply must be one of the labels (case and punctuation ignored);
  anything else is kept as `"label": null` with the raw `reply`, not guessed.
- Turns off the SDK's own retries (`maxRetries: 0`) so that retries cannot multiply, and applies
  this policy instead:

| Response | Action |
|---|---|
| 429 `Too Many Attempts.` (rate limit) | pause every worker for `Retry-After` seconds (capped at 60), then retry |
| 429 `quota_exceeded` / `session_quota_exceeded` | stop the batch: nothing passes before `resets_at` |
| 502, 503 `server_busy`, 504, 524, other 5xx, network errors | retry with exponential backoff and jitter |
| 503 `model_unavailable` | stop the batch: choose another `--model` |
| 413 `context_length_exceeded` | give up on that line, continue with the rest |
| 401, 402, 403, 422 | stop the batch: every line would fail the same way |

A line is tried at most 4 times. Failed lines are written as `{"line", "input", "error"}`.

## Dependencies

`openai` 7.x (pinned in [../package.json](../package.json)), which requires Node.js 22 or newer.

## Install

From the `javascript/` folder:

```bash
npm ci
```

## Configuration

| Variable / flag | Required | Default | Meaning |
|---|---|---|---|
| `MOLOCHAI_API_KEY` | yes | | API key, or `mock-key` for the mock server |
| `MOLOCHAI_BASE_URL` | no | `https://api.molochai.dev/api/v1` | API base URL |
| `MOLOCHAI_MODEL` / `--model` | no | `glm-5.3-flash` | Model id |
| file (positional) | yes | | Input text, one item per line |
| `--instruction` | no | rewrite in plain English | System instruction for rewrite mode |
| `--labels a,b,c` | no | | Classify into these labels instead |
| `--concurrency` | no | 4 | Requests in flight at once |
| `--rpm` | no | 50 | Request starts per rolling minute |

## Run

From the `javascript/` folder:

```bash
node text-processing/main.mjs text-processing/sample.txt > rewritten.jsonl
node text-processing/main.mjs text-processing/sample.txt --labels bug,feature,question,praise
```

## Expected output

From the mock server, which echoes each line instead of rewriting or classifying it, so in label
mode no reply is a label and every line gets `"label": null`. A real model returns the rewritten
text or a label.

```text
$ node text-processing/main.mjs text-processing/sample.txt --labels bug,feature,question,praise
[batch] 8 line(s), model glm-5.3-flash, concurrency 4, at most 50 requests/minute
{"line":1,"input":"The export button does nothing when I click it in Firefox.","label":null,"reply":"Mock reply from glm-5.3-flash: The export button does nothing when I click it in Firefox."}
{"line":2,"input":"could u add a dark mode to the dashboard pls","label":null,"reply":"Mock reply from glm-5.3-flash: could u add a dark mode to the dashboard pls"}
...
{"line":7,"input":"App crashes on startup since yesterdays update.","label":null,"reply":"Mock reply from glm-5.3-flash: App crashes on startup since yesterdays update."}
[batch] done: 8 ok (8 without a recognized label), 0 failed, 0 not processed
```

Results arrive in completion order; sort by `line` if you need input order.

## Error handling

- Exit status 0 when every line succeeded, 1 when any line failed or the batch was stopped. When
  the batch stops, lines already written stay valid and the stderr summary says how many were not
  processed.
- The smoke tests cover the rate-limit path (the mock's `[mock:rate-limit]` answers 429 with
  `Retry-After: 1` every time, so that line gives up after 4 attempts while the others finish),
  the quota stop, and backoff on 502.
- Requests are not streamed, because each output is one line. Non-streamed replies can contain
  inline `<think>...</think>` reasoning, which is removed. For long outputs, stream instead: a plain
  request that runs past about 100 seconds is cut off at the edge (524, retried here).
