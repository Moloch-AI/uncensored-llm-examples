// A small multi-turn terminal chat: history, streaming replies, and commands.
//
//   npx tsx terminal-chat/main.ts
//   npx tsx terminal-chat/main.ts --model llama-3.3-70b-instruct-abliterated --system "Be terse."
//
// Commands: /model (list models), /model <id> (switch), /reset (clear history),
// /help, /exit. End of input (Ctrl+D, or Ctrl+Z then Enter on Windows) also exits.
// The exit status is 1 if any turn failed, so scripted runs can detect errors.

import { stderr, stdin, stdout } from 'node:process';
import * as readline from 'node:readline';
import { parseArgs } from 'node:util';
import OpenAI, { APIConnectionError, APIError } from 'openai';

const DEFAULT_BASE_URL = 'https://api.molochai.dev/api/v1';
const DEFAULT_MODEL = 'glm-5.3-flash';
const CAP_NOTICE = '\n\n[Response is too large for your current plan. Try a smaller request.]';
const HELP =
  '/model [id]  list models or switch\n/reset       clear the conversation\n/help        this text\n/exit        quit';

// MolochAI accepts `reasoning_content` on assistant messages (an extension the
// SDK types do not know). Sending it back keeps reasoning across turns.
type AssistantTurn = OpenAI.Chat.ChatCompletionAssistantMessageParam & { reasoning_content?: string };
type Message = OpenAI.Chat.ChatCompletionMessageParam | AssistantTurn;
type DeltaWithReasoning = OpenAI.Chat.ChatCompletionChunk.Choice.Delta & { reasoning_content?: string | null };
type ModelWithExtensions = OpenAI.Models.Model & { name?: string; available?: boolean };

class FatalError extends Error {}

async function main(): Promise<number> {
  const { values } = parseArgs({
    options: {
      model: { type: 'string' },
      system: { type: 'string' },
      'show-reasoning': { type: 'boolean', default: false },
      'keep-reasoning': { type: 'boolean', default: false },
    },
  });
  const apiKey = process.env.MOLOCHAI_API_KEY?.trim();
  if (!apiKey) {
    return fail(
      'MOLOCHAI_API_KEY is not set. Create a key at https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples, or run against the mock server (see local-development/README.md).',
    );
  }

  const client = new OpenAI({
    apiKey,
    baseURL: process.env.MOLOCHAI_BASE_URL?.trim() || DEFAULT_BASE_URL,
    // While streaming this is the longest silence allowed between bytes; the
    // gateway sends keep-alive comments about once a second.
    timeout: 60_000,
    maxRetries: 2,
  });
  let model = values.model || process.env.MOLOCHAI_MODEL || DEFAULT_MODEL;
  const system: Message[] = values.system ? [{ role: 'system', content: values.system }] : [];
  let history: Message[] = [...system];
  let failedTurns = 0;

  const rl = readline.createInterface({ input: stdin, output: stdout, terminal: stdin.isTTY });
  rl.setPrompt('you> ');
  info(`model ${model}. Type /help for commands.`);
  rl.prompt();

  try {
    for await (const raw of rl) {
      const line = raw.trim();
      if (line === '') {
        rl.prompt();
        continue;
      }
      if (line === '/exit' || line === '/quit') break;
      if (line === '/help') {
        info(HELP);
      } else if (line === '/reset') {
        history = [...system];
        info('conversation cleared');
      } else if (line === '/model' || line.startsWith('/model ')) {
        model = await chooseModel(client, model, line.slice('/model'.length).trim());
      } else if (line.startsWith('/')) {
        info(`unknown command ${line.split(' ')[0]}; /help lists commands`);
      } else {
        history.push({ role: 'user', content: line });
        try {
          const reply = await streamReply(client, model, history, values['show-reasoning']);
          const turn: AssistantTurn = { role: 'assistant', content: reply.content };
          if (values['keep-reasoning'] && reply.reasoning) turn.reasoning_content = reply.reasoning;
          history.push(turn);
        } catch (err) {
          history.pop(); // forget the user turn that did not get an answer
          failedTurns++;
          const { message, fatal } = describeError(err);
          stderr.write(`\nerror: ${message}\n`);
          if (fatal) throw new FatalError(message);
          info('the message was not added to the history; send it again to retry');
        }
      }
      rl.prompt();
    }
  } catch (err) {
    if (err instanceof FatalError) return 1;
    throw err;
  } finally {
    rl.close();
  }
  stdout.write('\n');
  return failedTurns > 0 ? 1 : 0;
}

async function streamReply(
  client: OpenAI,
  model: string,
  history: Message[],
  showReasoning: boolean,
): Promise<{ content: string; reasoning: string }> {
  const stream = await client.chat.completions.create({
    model,
    messages: history as OpenAI.Chat.ChatCompletionMessageParam[],
    stream: true,
  });
  const filter = new CapNoticeFilter();
  let content = '';
  let reasoning = '';
  let finishReason: string | null = null;
  let usage: OpenAI.CompletionUsage | undefined;
  stdout.write(`${model}> `);
  try {
    for await (const chunk of stream) {
      if (chunk.usage) usage = chunk.usage;
      const choice = chunk.choices[0];
      if (!choice) continue; // the final usage chunk has `choices: []`
      const delta = choice.delta as DeltaWithReasoning;
      if (delta.reasoning_content) {
        reasoning += delta.reasoning_content;
        if (showReasoning) stderr.write(`\x1b[2m${delta.reasoning_content}\x1b[0m`);
      }
      if (delta.content) {
        content += delta.content;
        stdout.write(filter.feed(delta.content));
      }
      finishReason = choice.finish_reason ?? finishReason;
    }
  } finally {
    stdout.write(`${filter.finish()}\n`);
  }
  if (finishReason === 'length' || filter.found) {
    info("the reply stopped at the plan's output cap; ask for less or split the request");
  }
  if (usage) info(`tokens: ${usage.prompt_tokens} in, ${usage.completion_tokens} out`);
  // Keep the cap notice out of the history as well: it is not model output.
  return { content: content.endsWith(CAP_NOTICE) ? content.slice(0, -CAP_NOTICE.length) : content, reasoning };
}

async function chooseModel(client: OpenAI, current: string, wanted: string): Promise<string> {
  let models: ModelWithExtensions[];
  try {
    models = (await client.models.list()).data as ModelWithExtensions[];
  } catch (err) {
    stderr.write(`error: ${describeError(err).message}\n`);
    return current;
  }
  if (!wanted) {
    for (const m of models) {
      const mark = m.id === current ? '*' : ' ';
      info(`${mark} ${m.id}${m.available === false ? '  (not available right now)' : ''}`);
    }
    return current;
  }
  const match = models.find((m) => m.id === wanted);
  if (!match) {
    info(`${wanted} is not in the catalog; /model lists the ids`);
    return current;
  }
  if (match.available === false) info(`${wanted} is listed as not available; requests may fail with 503`);
  info(`model ${wanted}; the conversation history is kept`);
  return wanted;
}

/** Holds back any tail of the text that could be the start of CAP_NOTICE. */
class CapNoticeFilter {
  private held = '';
  found = false;

  feed(text: string): string {
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

  finish(): string {
    const held = this.held;
    this.held = '';
    if (held === CAP_NOTICE) {
      this.found = true;
      return '';
    }
    return held;
  }
}

const HINTS: Record<string, string> = {
  401: 'check MOLOCHAI_API_KEY (missing, mistyped or revoked)',
  no_active_plan:
    'plans and credit: https://molochai.dev/pricing?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples',
  email_unverified: "confirm the account's email address, then retry",
  context_length_exceeded: 'the conversation no longer fits the context; /reset to start over',
  422: 'the request was rejected as invalid; /model lists valid ids',
  429: 'over 60 requests per minute; wait a moment',
  quota_exceeded: 'quota used up; retrying before resets_at will not help',
  session_quota_exceeded: "5-hour session cap reached; retrying before resets_at won't help",
  model_unavailable: 'no server is serving this model right now; /model to pick another',
  server_busy: 'no free slot right now; try again shortly',
  preempted: 'the server was reclaimed mid-reply; retry in about a minute',
};
// Errors after which no further turn can succeed in this session.
const FATAL = new Set(['401', 'no_active_plan', 'email_unverified', 'quota_exceeded', 'session_quota_exceeded']);

function describeError(err: unknown): { message: string; fatal: boolean } {
  if (err instanceof APIConnectionError) {
    return { message: `could not reach the API (${err.message}); check MOLOCHAI_BASE_URL`, fatal: false };
  }
  if (!(err instanceof APIError)) return { message: String(err), fatal: false };
  // err.error: the inner "error" object for gateway errors (also for error
  // events inside a stream, where err.status is undefined), or the whole body
  // for validation and bare-message errors.
  const body = (err.error ?? {}) as Record<string, unknown>;
  const kind = typeof body.type === 'string' ? body.type : typeof body.code === 'string' ? body.code : undefined;
  const where = err.status === undefined ? 'stream error' : `HTTP ${err.status}`;
  let message = `${where}${kind ? ` ${kind}` : ''}${typeof body.message === 'string' ? `: ${body.message}` : ''}`;
  if (typeof body.resets_at === 'string') message += ` (resets at ${body.resets_at})`;
  const key = kind ?? String(err.status);
  const hint = HINTS[key];
  if (hint) message += `\n  hint: ${hint}`;
  return { message, fatal: FATAL.has(key) };
}

function info(message: string): void {
  stderr.write(`[chat] ${message}\n`);
}

function fail(message: string): number {
  stderr.write(`error: ${message}\n`);
  return 1;
}

process.exitCode = await main();
