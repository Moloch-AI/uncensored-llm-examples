// Batch text processing: one request per input line, a few at a time, paced
// under the account's rate limit, with a retry policy that matches the API.
//
//   node text-processing/main.mjs text-processing/sample.txt > rewritten.jsonl
//   node text-processing/main.mjs text-processing/sample.txt --labels bug,feature,question,praise
//
// Output is one JSON object per line (JSONL) on stdout, in completion order,
// each with its input line number. Progress and errors go to stderr.
//
// Retry policy (the SDK's own retries are turned off so they cannot multiply
// with these):
//   429 "Too Many Attempts."      pause every worker for Retry-After (capped), retry
//   429 quota / session quota     stop the batch: nothing passes before resets_at
//   502, 503 server_busy, 504,    retry with exponential backoff and jitter
//   524, other 5xx, network
//   503 model_unavailable         stop the batch: pick another --model
//   413 context_length_exceeded   give up on that line, continue with the others
//   401, 402, 403, 422            stop the batch: every line would fail the same way

import { readFile } from 'node:fs/promises';
import { setTimeout as sleep } from 'node:timers/promises';
import { parseArgs } from 'node:util';
import OpenAI, { APIConnectionError, APIError } from 'openai';

const DEFAULT_BASE_URL = 'https://api.molochai.dev/api/v1';
const DEFAULT_MODEL = 'glm-5.3-flash';
const MAX_ATTEMPTS = 4;
const MAX_RETRY_AFTER_S = 60;
const DEFAULT_INSTRUCTION =
  'Rewrite the text in plain, neutral English. Keep the meaning. Reply with the rewritten text only.';

// Exit quietly when the reader of stdout goes away early (for example `| head`).
process.stdout.on('error', (err) => {
  if (err.code !== 'EPIPE') throw err;
  process.exit(process.exitCode ?? 0);
});

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      model: { type: 'string' },
      instruction: { type: 'string', default: DEFAULT_INSTRUCTION },
      labels: { type: 'string' },
      concurrency: { type: 'string', default: '4' },
      rpm: { type: 'string', default: '50' },
    },
  });
  const [file] = positionals;
  const concurrency = Number(values.concurrency);
  const rpm = Number(values.rpm);
  if (!file || !(concurrency >= 1) || !(rpm >= 1)) {
    return fail(
      'usage: node text-processing/main.mjs <file> [--labels a,b,c | --instruction "..."] [--concurrency 4] [--rpm 50] [--model id]',
    );
  }
  const apiKey = process.env.MOLOCHAI_API_KEY?.trim();
  if (!apiKey) {
    return fail(
      'MOLOCHAI_API_KEY is not set. Create a key at https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples, or run against the mock server (see local-development/README.md).',
    );
  }

  let text;
  try {
    text = await readFile(file, 'utf8');
  } catch (err) {
    return fail(`could not read ${file}: ${err.message}`);
  }
  const items = text
    .split(/\r?\n/)
    .map((line, index) => ({ number: index + 1, text: line.trim() }))
    .filter((item) => item.text !== '');

  const labels = values.labels
    ?.split(',')
    .map((label) => label.trim().toLowerCase())
    .filter(Boolean);
  const system = labels
    ? `Classify the user's text into exactly one of these labels: ${labels.join(', ')}. Reply with the label only.`
    : values.instruction;

  const client = new OpenAI({
    apiKey,
    baseURL: process.env.MOLOCHAI_BASE_URL?.trim() || DEFAULT_BASE_URL,
    maxRetries: 0, // this script owns the retry policy
    timeout: 90_000, // a plain request must finish before the edge gives up (~100 s)
  });
  const model = values.model || process.env.MOLOCHAI_MODEL || DEFAULT_MODEL;
  const pacer = new Pacer(rpm);
  const stats = { ok: 0, unlabeled: 0, failed: 0 };
  let stopReason;

  async function processItem(item) {
    for (let attempt = 1; ; attempt++) {
      if (stopReason) return;
      await pacer.acquire();
      if (stopReason) return;
      try {
        const completion = await client.chat.completions.create({
          model,
          messages: [
            { role: 'system', content: system },
            { role: 'user', content: item.text },
          ],
        });
        const reply = stripThink(completion.choices[0]?.message.content ?? '');
        const result = { line: item.number, input: item.text };
        if (labels) {
          result.label = matchLabel(reply, labels);
          if (result.label === null) {
            result.reply = reply; // keep what the model said when it is not a label
            stats.unlabeled++;
          }
        } else {
          result.output = reply;
        }
        process.stdout.write(`${JSON.stringify(result)}\n`);
        stats.ok++;
        return;
      } catch (err) {
        const decision = classify(err);
        const what = describe(err);
        if (decision.action === 'stop') {
          stopReason ??= `line ${item.number}: ${what}`;
          return;
        }
        if (decision.action === 'skip' || attempt >= MAX_ATTEMPTS) {
          log(`line ${item.number}: giving up after ${attempt} attempt(s): ${what}`);
          process.stdout.write(`${JSON.stringify({ line: item.number, input: item.text, error: what })}\n`);
          stats.failed++;
          return;
        }
        if (decision.action === 'wait') {
          log(`line ${item.number}: rate limited, pausing all workers ${decision.seconds} s (Retry-After)`);
          pacer.pauseFor(decision.seconds);
        } else {
          const delay = backoffMs(attempt);
          log(`line ${item.number}: ${what}; retrying in ${(delay / 1000).toFixed(1)} s`);
          await sleep(delay);
        }
      }
    }
  }

  log(`${items.length} line(s), model ${model}, concurrency ${concurrency}, at most ${rpm} requests/minute`);
  const queue = [...items];
  await Promise.all(
    Array.from({ length: Math.min(concurrency, queue.length) }, async () => {
      while (queue.length > 0 && !stopReason) await processItem(queue.shift());
    }),
  );

  const notStarted = items.length - stats.ok - stats.failed;
  log(
    `done: ${stats.ok} ok${labels ? ` (${stats.unlabeled} without a recognized label)` : ''}, ${stats.failed} failed, ${notStarted} not processed`,
  );
  if (stopReason) return fail(`stopped the batch. ${stopReason}`);
  return stats.failed > 0 ? 1 : 0;
}

/** Sliding 60-second window of request starts, plus a shared pause for Retry-After. */
class Pacer {
  starts = [];
  pausedUntil = 0;

  constructor(perMinute) {
    this.perMinute = perMinute;
  }

  pauseFor(seconds) {
    this.pausedUntil = Math.max(this.pausedUntil, Date.now() + seconds * 1000);
  }

  async acquire() {
    for (;;) {
      const now = Date.now();
      if (this.pausedUntil > now) {
        await sleep(this.pausedUntil - now);
        continue;
      }
      while (this.starts.length > 0 && this.starts[0] <= now - 60_000) this.starts.shift();
      if (this.starts.length < this.perMinute) {
        this.starts.push(now);
        return;
      }
      await sleep(this.starts[0] + 60_000 - now);
    }
  }
}

/** Decide what to do with an error. Reads status, `type` and headers, never message text. */
function classify(err) {
  if (err instanceof APIConnectionError) return { action: 'backoff' }; // includes timeouts
  if (!(err instanceof APIError) || err.status === undefined) return { action: 'stop' };
  // err.error is the inner "error" object for gateway errors, or the whole
  // body for validation and bare-message errors.
  const kind = err.error?.type ?? err.error?.code;
  if (err.status === 429) {
    if (kind === 'quota_exceeded' || kind === 'session_quota_exceeded') return { action: 'stop' };
    return { action: 'wait', seconds: retryAfterSeconds(err.headers) };
  }
  if (err.status === 503 && kind === 'model_unavailable') return { action: 'stop' };
  if (err.status >= 500) return { action: 'backoff' };
  if (err.status === 413) return { action: 'skip' };
  return { action: 'stop' };
}

function retryAfterSeconds(headers) {
  const raw = headers?.get('retry-after');
  let seconds = Number(raw);
  if (raw && !Number.isFinite(seconds)) seconds = (Date.parse(raw) - Date.now()) / 1000; // HTTP-date form
  if (!raw || !Number.isFinite(seconds) || seconds < 0) seconds = 5;
  return Math.min(Math.ceil(seconds), MAX_RETRY_AFTER_S);
}

function backoffMs(attempt) {
  const ceiling = Math.min(8_000, 500 * 2 ** (attempt - 1));
  return ceiling / 2 + Math.random() * (ceiling / 2); // exponential backoff with jitter
}

function describe(err) {
  if (!(err instanceof APIError) || err.status === undefined) return err.message;
  const body = err.error ?? {};
  const kind = body.type ?? body.code;
  const extra = ['resets_at', 'estimated_tokens', 'context_cap', 'model']
    .filter((key) => key in body)
    .map((key) => `${key}=${body[key]}`);
  // Without a `type`, the human message is the only description there is.
  const label = kind ? ` ${kind}` : typeof body.message === 'string' ? `: ${body.message}` : '';
  return `HTTP ${err.status}${label}${extra.length ? ` (${extra.join(', ')})` : ''}`;
}

/** Non-streamed replies can carry inline reasoning as <think>...</think>. */
function stripThink(text) {
  return text.replace(/<think>[\s\S]*?<\/think>/g, '').trim();
}

/** Accept the reply only if it is one of the labels, ignoring case and punctuation. */
function matchLabel(reply, labels) {
  const cleaned = reply
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, '')
    .trim();
  if (labels.includes(cleaned)) return cleaned;
  const mentioned = labels.filter((label) =>
    new RegExp(`(^|\\s)${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\s|$)`, 'u').test(cleaned),
  );
  return mentioned.length === 1 ? mentioned[0] : null;
}

function log(message) {
  process.stderr.write(`[batch] ${message}\n`);
}

function fail(message) {
  process.stderr.write(`error: ${message}\n`);
  return 1;
}

process.exitCode = await main();
