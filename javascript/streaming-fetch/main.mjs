// Streaming with plain fetch: no SDK, Node >= 20. Shows the wire format.
//
//   node streaming-fetch/main.mjs "Explain TCP slow start in one paragraph."
//
// What it handles, in the order a stream can produce it:
//   - HTTP errors before the stream opens (three JSON body shapes, or non-JSON)
//   - SSE comment lines (": ka", ": hb", ": queued · position 2 of 3 · waiting 4s"):
//     ignored, except that queue notices are shown on stderr as progress
//   - `delta.reasoning_content` (to stderr) and `delta.content` (to stdout)
//   - an error event after the stream opened (HTTP status is already 200)
//   - the cap notice appended at the output cap, removed before printing
//   - the final usage chunk, whose `choices` list is empty
//   - `data: [DONE]`

import { parseArgs } from 'node:util';

const DEFAULT_BASE_URL = 'https://api.molochai.dev/api/v1';
const DEFAULT_MODEL = 'glm-5.3-flash';
const CAP_NOTICE = '\n\n[Response is too large for your current plan. Try a smaller request.]';
// The gateway sends a keep-alive comment about once a second while a model is
// quiet, so a long silence means the connection is gone.
const IDLE_TIMEOUT_MS = 60_000;

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      model: { type: 'string' },
      'hide-reasoning': { type: 'boolean', default: false },
    },
  });
  const apiKey = process.env.MOLOCHAI_API_KEY?.trim();
  if (!apiKey) {
    return fail(
      'MOLOCHAI_API_KEY is not set. Create a key at https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples, or run against the mock server (see local-development/README.md).',
    );
  }
  const baseUrl = (process.env.MOLOCHAI_BASE_URL?.trim() || DEFAULT_BASE_URL).replace(/\/+$/, '');
  const model = values.model || process.env.MOLOCHAI_MODEL || DEFAULT_MODEL;
  const prompt = positionals.join(' ') || 'Explain TCP slow start to a new engineer in one paragraph.';

  const controller = new AbortController();
  let idleTimer;
  const stillAlive = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(
      () => controller.abort(new Error(`no data for ${IDLE_TIMEOUT_MS / 1000} s`)),
      IDLE_TIMEOUT_MS,
    );
  };
  stillAlive();

  try {
    let response;
    try {
      response = await fetch(`${baseUrl}/chat/completions`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify({
          model,
          messages: [{ role: 'user', content: prompt }],
          stream: true,
          // Dropped by MolochAI, which always sends usage; other
          // OpenAI-compatible servers need it to send usage at all.
          stream_options: { include_usage: true },
        }),
        signal: controller.signal,
      });
    } catch (err) {
      return fail(`could not reach ${baseUrl}: ${err.cause?.message ?? err.message}`);
    }
    if (!response.ok) return fail(await describeHttpError(response));

    const capFilter = new CapNoticeFilter();
    let usage;
    let finishReason;
    let inReasoning = false;

    for await (const event of sseEvents(response.body, stillAlive)) {
      if (event.comment !== undefined) {
        // Comments carry no data. Only queue notices are worth showing; match
        // loosely, the exact wording is for people and can change.
        if (event.comment.startsWith('queued')) process.stderr.write(`[queue] ${event.comment}\n`);
        continue;
      }
      if (event.data === '[DONE]') break;

      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        process.stderr.write(`[warn] skipped a data line that is not JSON: ${event.data}\n`);
        continue;
      }

      if (payload.error) {
        // An upstream failure after the stream opened: HTTP status stays 200.
        process.stdout.write(`${capFilter.finish()}\n`);
        const { type, message } = payload.error;
        const advice =
          type === 'preempted'
            ? 'the server was reclaimed mid-reply; retry in about a minute'
            : 'retry the request, with backoff';
        return fail(`stream error ${type}: ${message}\n  the reply above is incomplete; ${advice}`);
      }
      if (payload.usage) usage = payload.usage;
      const choice = payload.choices?.[0];
      if (!choice) continue; // the usage chunk has `choices: []`

      const reasoning = choice.delta?.reasoning_content;
      if (reasoning && !values['hide-reasoning']) {
        if (!inReasoning) process.stderr.write('[reasoning] ');
        inReasoning = true;
        process.stderr.write(reasoning);
      }
      const content = choice.delta?.content;
      if (content) {
        if (inReasoning) process.stderr.write('\n[/reasoning]\n');
        inReasoning = false;
        process.stdout.write(capFilter.feed(content));
      }
      if (choice.finish_reason) finishReason = choice.finish_reason;
    }

    process.stdout.write(`${capFilter.finish()}\n`);
    if (inReasoning) process.stderr.write('\n[/reasoning]\n');
    if (finishReason === 'length' || capFilter.found) {
      process.stderr.write(
        "[note] the reply stopped at the plan's output cap (cap notice removed); max_tokens has no effect on MolochAI, so ask for less or split the task\n",
      );
    }
    if (usage) {
      process.stderr.write(
        `[usage] prompt=${usage.prompt_tokens} completion=${usage.completion_tokens} total=${usage.total_tokens}\n`,
      );
    }
    return 0;
  } catch (err) {
    if (controller.signal.aborted) return fail(`stream stalled: ${controller.signal.reason?.message}`);
    return fail(`stream failed: ${err.cause?.message ?? err.message}`);
  } finally {
    clearTimeout(idleTimer);
  }
}

/**
 * Parse a server-sent events body into `{ data }` and `{ comment }` items.
 * Data lines of one event are joined with "\n"; a blank line ends an event.
 * Other fields (`event`, `id`, `retry`) are not used by this API and skipped.
 */
async function* sseEvents(body, onBytes) {
  const decoder = new TextDecoder();
  let buffer = '';
  let data = [];
  const handleLine = function* (line) {
    if (line === '') {
      if (data.length > 0) yield { data: data.join('\n') };
      data = [];
    } else if (line.startsWith(':')) {
      yield { comment: line.slice(1).trim() };
    } else {
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'data') data.push(value);
    }
  };
  for await (const bytes of body) {
    onBytes();
    buffer += decoder.decode(bytes, { stream: true });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop(); // the last piece may be an incomplete line
    for (const line of lines) yield* handleLine(line);
  }
  buffer += decoder.decode();
  if (buffer) yield* handleLine(buffer);
  yield* handleLine(''); // dispatch an event the server did not end with a blank line
}

/** Holds back any tail of the text that could be the start of CAP_NOTICE. */
class CapNoticeFilter {
  held = '';
  found = false;

  feed(text) {
    this.held += text;
    let keep = 0;
    for (let size = Math.min(this.held.length, CAP_NOTICE.length); size > 0; size--) {
      if (CAP_NOTICE.startsWith(this.held.slice(-size))) {
        keep = size;
        break;
      }
    }
    const ready = this.held.slice(0, this.held.length - keep);
    this.held = this.held.slice(this.held.length - keep);
    return ready;
  }

  finish() {
    const held = this.held;
    this.held = '';
    if (held === CAP_NOTICE) {
      this.found = true;
      return '';
    }
    return held;
  }
}

// What to tell the user, keyed by error `type`/`code` or by HTTP status.
const HINTS = {
  401: 'check MOLOCHAI_API_KEY (missing, mistyped or revoked)',
  no_active_plan:
    'plans and credit: https://molochai.dev/pricing?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples',
  email_unverified: "confirm the account's email address, then retry",
  context_length_exceeded: "the prompt does not fit this model's context; shorten it",
  422: 'the request was rejected as invalid; check the model id against GET /models',
  429: 'over 60 requests per minute; wait for the Retry-After header',
  quota_exceeded: 'quota used up; retrying before resets_at will not help',
  session_quota_exceeded: "5-hour session cap reached; retrying before resets_at won't help",
  model_unavailable: 'no server is serving this model right now; try another with --model',
  server_busy: 'no free slot right now; try again shortly',
  524: 'the edge timed out a long non-streamed reply; use streaming',
};
const DETAIL_FIELDS = ['resets_at', 'estimated_tokens', 'context_cap', 'bound_by', 'model', 'status'];

/** Read an error response. Check the status first; never assume the body is JSON. */
async function describeHttpError(response) {
  const status = response.status;
  const raw = await response.text().catch(() => '');
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    body = undefined;
  }
  if (!body || typeof body !== 'object') {
    return `HTTP ${status} with a non-JSON body${HINTS[status] ? `\n  hint: ${HINTS[status]}` : ''}`;
  }
  // Gateway errors nest under "error"; validation (422) and bare-message
  // errors (401, 403, 429 rate limit) are flat.
  const error = body.error && typeof body.error === 'object' ? body.error : body;
  const kind = error.type ?? error.code;
  let text = `HTTP ${status}${kind ? ` ${kind}` : ''}${error.message ? `: ${error.message}` : ''}`;
  const details = DETAIL_FIELDS.filter((key) => key in error).map((key) => `${key}=${error[key]}`);
  if (error.errors && typeof error.errors === 'object') {
    for (const [key, messages] of Object.entries(error.errors)) details.push(`${key}: ${[messages].flat().join('; ')}`);
  }
  const retryAfter = response.headers.get('retry-after');
  if (status === 429 && retryAfter) details.push(`retry after ${retryAfter} s`);
  if (details.length > 0) text += ` (${details.join(', ')})`;
  const hint = HINTS[kind] ?? HINTS[status];
  return hint ? `${text}\n  hint: ${hint}` : text;
}

function fail(message) {
  process.stderr.write(`error: ${message}\n`);
  return 1;
}

process.exitCode = await main();
