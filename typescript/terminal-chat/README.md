# Terminal chat (TypeScript)

A small multi-turn chat in the terminal: conversation history, streamed replies, and `/model` and
`/reset` commands.

## What it does

- Keeps the conversation in memory and sends the whole history with each turn.
- Streams each reply with the official `openai` SDK. Reasoning is hidden unless
  `--show-reasoning` is given (then it is printed dimmed on stderr).
- Removes the cap notice the gateway appends at the output cap, from the screen and from the
  history, and says when a reply was cut off.
- On a failed turn, removes the unanswered message from the history and keeps the session going,
  except after errors that no later turn can get past (401, 402 `no_active_plan`, 403
  `email_unverified`, 429 quota errors), which end the session.
- `--keep-reasoning` sends `reasoning_content` back on assistant messages, a MolochAI extension
  that keeps the model's reasoning across turns. It also makes every turn use more prompt tokens.
  The SDK's types do not know the field, so the example widens the type.

Commands:

| Command | Effect |
|---|---|
| `/model` | list models from `GET /models`, marking the current one and any that are not available |
| `/model <id>` | switch model (checked against the catalog); history is kept |
| `/reset` | clear the history (the `--system` message stays) |
| `/help` | list commands |
| `/exit`, `/quit` | quit; end of input (Ctrl+D, or Ctrl+Z then Enter on Windows) also quits |

## Dependencies

`openai` 7.x (requires Node.js 22 or newer), plus `tsx` to run TypeScript directly and
`typescript` for type checking. All pinned in [../package.json](../package.json).

## Install

From the `typescript/` folder:

```bash
npm ci
```

## Configuration

| Variable / flag | Required | Default | Meaning |
|---|---|---|---|
| `MOLOCHAI_API_KEY` | yes | | API key, or `mock-key` for the mock server |
| `MOLOCHAI_BASE_URL` | no | `https://api.molochai.dev/api/v1` | API base URL |
| `MOLOCHAI_MODEL` / `--model` | no | `glm-5.3-flash` | Starting model id |
| `--system` | no | none | A system message for the whole session |
| `--show-reasoning` | no | off | Print reasoning (dimmed, stderr) |
| `--keep-reasoning` | no | off | Send reasoning back in the history |

## Run

From the `typescript/` folder:

```bash
npx tsx terminal-chat/main.ts
```

## Expected output

From the mock server (canned text, not model output), with these lines piped in:
`What is a mutex?`, `/model`, `/model llama-3.3-70b-instruct-abliterated`, `And a semaphore?`,
`/reset`, `/exit`. Piped input is not echoed, so your own lines do not appear after `you>`; in an
interactive terminal they do.

```text
[chat] model glm-5.3-flash. Type /help for commands.
you> glm-5.3-flash> Mock reply from glm-5.3-flash: What is a mutex?
[chat] tokens: 4 in, 20 out
you> [chat] * glm-5.3-flash
[chat]   glm-4.7-flash-uncensored
[chat]   gemma4-26b-a4b-qat
[chat]   qwen3.8-27b-uncensored
[chat]   llama-3.3-70b-instruct-abliterated
you> [chat] model llama-3.3-70b-instruct-abliterated; the conversation history is kept
you> llama-3.3-70b-instruct-abliterated> Mock reply from llama-3.3-70b-instruct-abliterated: And a semaphore?
[chat] tokens: 20 in, 17 out
you> [chat] conversation cleared
you>
```

The second turn's `20 in` is the history at work: the first exchange was sent again.

## Error handling

- Exit status: 0 if every turn succeeded, 1 if any turn failed (useful when the chat is scripted
  through a pipe) or the session ended on a fatal error.
- The SDK retries 429 and 5xx twice before a turn fails (`maxRetries: 2`), but not quota 429s,
  which MolochAI marks `X-Should-Retry: false`.
- History grows with every turn. When it no longer fits, the gateway answers 413
  `context_length_exceeded`; the hint suggests `/reset`. A longer-lived app would drop or summarize
  old turns instead.
- An error event mid-stream ends the turn; the partial reply is shown but not kept in the history.
