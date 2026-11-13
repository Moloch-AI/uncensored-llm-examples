// Vercel AI SDK with @ai-sdk/openai-compatible pointed at MolochAI: generateText,
// streamText and a tool call.
//
//   npx tsx vercel-ai-sdk/main.ts generate "In two sentences, what is a context window?"
//   npx tsx vercel-ai-sdk/main.ts stream "Explain TCP slow start in one paragraph."
//   npx tsx vercel-ai-sdk/main.ts tools "How many miles is a 21.1 km half marathon?"
//
// What it shows (verified against the mock with ai 7.0.172 and
// @ai-sdk/openai-compatible 3.0.92):
// - reasoning_content arrives as reasoning (reasoningText, reasoning-delta parts)
//   and is sent back on assistant turns of a tool loop without extra code.
// - streamText never throws. An HTTP error before the stream and an error event
//   inside it both arrive as `error` parts; textStream just ends early. This
//   file reads fullStream so that a cut-off answer is reported as one.
// - The SDK's own retries (maxRetries, default 2) also retry 429 quota errors and
//   503 model_unavailable (it ignores the API's X-Should-Retry: false header), so
//   they are off here and a small policy decides.
//
// Answers go to stdout; reasoning, tool steps, usage and errors go to stderr.

import { parseArgs } from 'node:util';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import {
  APICallError,
  extractReasoningMiddleware,
  type FinishReason,
  generateText,
  type LanguageModel,
  type LanguageModelUsage,
  RetryError,
  StreamProviderError,
  stepCountIs,
  streamText,
  tool,
  wrapLanguageModel,
} from 'ai';
import { z } from 'zod';

const DEFAULT_BASE_URL = 'https://api.molochai.dev/api/v1';
const DEFAULT_MODEL = 'glm-5.3-flash';
const CAP_NOTICE = '\n\n[Response is too large for your current plan. Try a smaller request.]';
const ATTEMPTS = 3;

function makeModel(modelId: string): LanguageModel {
  const apiKey = process.env.MOLOCHAI_API_KEY?.trim();
  if (!apiKey) {
    process.stderr.write(
      'error: MOLOCHAI_API_KEY is not set. Create a key at https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples, or run against the mock server (see local-development/README.md).\n',
    );
    process.exit(1);
  }
  const molochai = createOpenAICompatible({
    name: 'molochai',
    baseURL: process.env.MOLOCHAI_BASE_URL?.trim() || DEFAULT_BASE_URL,
    apiKey,
    // Sends stream_options.include_usage. MolochAI drops it and always sends
    // usage; other OpenAI-compatible servers need it.
    includeUsage: true,
  });
  // A non-streamed reply can carry reasoning inline as <think>...</think> (the
  // gateway strips it only while streaming). This middleware moves it to reasoning.
  return wrapLanguageModel({
    model: molochai.chatModel(modelId),
    middleware: extractReasoningMiddleware({ tagName: 'think' }),
  });
}

// --- generateText ------------------------------------------------------------

async function runGenerate(model: LanguageModel, prompt: string): Promise<number> {
  const result = await withRetries(() => generateText({ model, prompt, maxRetries: 0 }));
  if (result.reasoningText) process.stderr.write(`[reasoning] ${result.reasoningText.trim()}\n`);
  const capped = result.text.endsWith(CAP_NOTICE);
  process.stdout.write(`${(capped ? result.text.slice(0, -CAP_NOTICE.length) : result.text).trim()}\n`);
  report(result.finishReason, capped, result.totalUsage);
  return 0;
}

// --- streamText --------------------------------------------------------------

async function runStream(model: LanguageModel, prompt: string): Promise<number> {
  // onError replaces the default, which prints the whole error object to stderr.
  const result = streamText({ model, prompt, maxRetries: 0, onError: () => {} });
  const capFilter = new CapNoticeFilter();
  let failure: unknown;
  let finishReason: FinishReason | undefined;
  let usage: LanguageModelUsage | undefined;
  let inReasoning = false;
  let sawText = false;
  for await (const part of result.fullStream) {
    if (part.type === 'reasoning-delta') {
      process.stderr.write(inReasoning ? part.text : `[reasoning] ${part.text}`);
      inReasoning = true;
    } else if (part.type === 'text-delta') {
      if (inReasoning) process.stderr.write('\n');
      inReasoning = false;
      sawText = true;
      process.stdout.write(capFilter.feed(part.text));
    } else if (part.type === 'error') {
      failure = part.error; // the stream ends after this
    } else if (part.type === 'finish') {
      finishReason = part.finishReason;
      usage = part.totalUsage;
    }
  }
  if (sawText) process.stdout.write(`${capFilter.finish()}\n`);
  if (inReasoning) process.stderr.write('\n');
  if (failure !== undefined) {
    let message = describeError(failure);
    if (StreamProviderError.isInstance(failure)) {
      const wait = failure.type === 'preempted' ? 'in about a minute' : 'with backoff';
      message += `\n  the reply above is incomplete; retry ${wait}`;
    }
    return fail(message);
  }
  report(finishReason, capFilter.found, usage);
  return 0;
}

// --- tools -------------------------------------------------------------------

const UNITS = { km: ['length', 1000], mi: ['length', 1609.344], kg: ['mass', 1], lb: ['mass', 0.45359237] } as const;
const Unit = z.enum(['km', 'mi', 'kg', 'lb']);

const convertUnits = tool({
  description: 'Convert a distance (km, mi) or a mass (kg, lb) to another unit.',
  inputSchema: z.object({ value: z.number(), from_unit: Unit, to_unit: Unit }),
  // The SDK validates the arguments against inputSchema before calling this.
  execute: async ({ value, from_unit, to_unit }) => {
    const [fromDimension, fromFactor] = UNITS[from_unit];
    const [toDimension, toFactor] = UNITS[to_unit];
    if (fromDimension !== toDimension) return { error: `cannot convert ${from_unit} to ${to_unit}` };
    return { value, from_unit, to_unit, result: Math.round((value * fromFactor * 10_000) / toFactor) / 10_000 };
  },
});

async function runTools(model: LanguageModel, prompt: string, maxSteps: number): Promise<number> {
  // One generateText call runs the whole loop: model call, tool call, model call...
  // A retry repeats the whole loop, which is fine for tools without side effects.
  const result = await withRetries(() =>
    generateText({
      model,
      system: 'Use the convert_units tool for every conversion. Answer briefly.',
      prompt,
      tools: { convert_units: convertUnits },
      stopWhen: stepCountIs(maxSteps),
      maxRetries: 0,
      onStepFinish: (step) => {
        for (const call of step.toolCalls) {
          const output = step.toolResults.find((r) => r.toolCallId === call.toolCallId)?.output;
          process.stderr.write(
            `[step ${step.stepNumber + 1}] ${call.toolName}(${JSON.stringify(call.input)}) -> ${JSON.stringify(output)}\n`,
          );
        }
      },
    }),
  );
  if (result.finishReason === 'tool-calls') {
    return fail(`no final answer after ${maxSteps} model calls (raise --max-steps?)`);
  }
  process.stdout.write(`${result.text.trim()}\n`);
  process.stderr.write(`[done] ${result.steps.length} model call(s)\n`);
  return 0;
}

// --- errors and retries ------------------------------------------------------

type Json = Record<string, unknown>;

/** The error object of a failed HTTP call: the inner `error` (shape A) or the whole body (B, C). */
function errorBody(err: APICallError): Json | undefined {
  try {
    const body = JSON.parse(err.responseBody ?? '') as Json;
    const inner = body.error;
    return inner !== null && typeof inner === 'object' ? (inner as Json) : body;
  } catch {
    return undefined; // not JSON, e.g. the edge's HTML 524 page
  }
}

/** Milliseconds to wait before the next attempt, or undefined to give up. Reads status and type. */
function retryDelayMs(err: unknown, attempt: number): number | undefined {
  if (attempt >= ATTEMPTS || !APICallError.isInstance(err)) return undefined;
  const backoff = Math.min(2 ** attempt, 10) * 1000 * (0.5 + Math.random() / 2);
  const status = err.statusCode;
  if (status === undefined) return err.isRetryable ? backoff : undefined; // no response: connection error
  const body = errorBody(err);
  if (status === 429) {
    if (body && 'resets_at' in body) return undefined; // quota: nothing changes before resets_at
    const retryAfter = Number(err.responseHeaders?.['retry-after']);
    return Number.isFinite(retryAfter) ? Math.min(retryAfter, 30) * 1000 : backoff;
  }
  if ([502, 504, 524].includes(status) || (status === 503 && body?.type === 'server_busy')) return backoff;
  return undefined; // 401/402/403/413/422 and 503 model_unavailable
}

async function withRetries<T>(call: () => Promise<T>): Promise<T> {
  for (let attempt = 1; ; attempt++) {
    try {
      return await call();
    } catch (err) {
      const delay = retryDelayMs(err, attempt);
      if (delay === undefined) throw err;
      process.stderr.write(`[retry] ${describeError(err).split('\n')[0]}; in ${(delay / 1000).toFixed(1)} s\n`);
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }
}

const HINTS: Record<string, string> = {
  '401': 'check MOLOCHAI_API_KEY (missing, mistyped or revoked)',
  no_active_plan:
    'plans and credit: https://molochai.dev/pricing?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples',
  email_unverified: "confirm the account's email address, then retry",
  context_length_exceeded: "the prompt does not fit this model's context; shorten it",
  '422': 'the request was rejected as invalid; check the model id against GET /models',
  '429': 'over 60 requests per minute; wait for the Retry-After header',
  quota_exceeded: 'quota used up; retrying before resets_at will not help',
  session_quota_exceeded: "5-hour session cap reached; retrying before resets_at won't help",
  model_unavailable: 'no server is serving this model right now; try another with --model',
  server_busy: 'no free slot right now; try again shortly',
  '524': 'the edge timed out a long non-streamed reply; use streaming',
};
const DETAIL_FIELDS = ['resets_at', 'estimated_tokens', 'context_cap', 'bound_by', 'model', 'status'];

/**
 * A readable message for any error. Reads fields, never message text. The SDK's
 * own `message` and `data` are not enough: for the validation (422) and
 * bare-message (401, 403, 429) bodies the message is only the HTTP status text,
 * and `data` keeps `type` but drops `resets_at`, `context_cap` and the rest. So
 * the body is parsed again from `responseBody`.
 */
function describeError(err: unknown): string {
  if (RetryError.isInstance(err)) return describeError(err.lastError);
  if (APICallError.isInstance(err)) {
    const status = err.statusCode;
    if (status === undefined)
      return `could not reach the API (${err.message}); check MOLOCHAI_BASE_URL and the network`;
    const body = errorBody(err);
    if (!body) return `HTTP ${status} with a non-JSON body${hint(String(status))}`;
    const kind = body.type ?? body.code;
    let text = `HTTP ${status}${kind ? ` ${String(kind)}` : ''}`;
    if (typeof body.message === 'string') text += `: ${body.message}`;
    const details = DETAIL_FIELDS.filter((key) => key in body).map((key) => `${key}=${String(body[key])}`);
    if (body.errors && typeof body.errors === 'object') {
      for (const [field, problems] of Object.entries(body.errors)) details.push(`${field}: ${String(problems)}`);
    }
    if (details.length) text += ` (${details.join(', ')})`;
    return text + hint(String(kind ?? ''), String(status));
  }
  if (StreamProviderError.isInstance(err)) return `stream error ${err.type ?? ''}: ${err.message}`;
  return err instanceof Error ? `${err.name}: ${err.message}` : String(err);
}

function hint(...keys: string[]): string {
  const found = keys.map((key) => HINTS[key]).find(Boolean);
  return found ? `\n  hint: ${found}` : '';
}

// --- shared helpers ----------------------------------------------------------

function report(finishReason: FinishReason | undefined, capNotice: boolean, usage: LanguageModelUsage | undefined) {
  if (finishReason === 'length' || capNotice) {
    process.stderr.write(
      "[note] the reply stopped at the plan's output cap (cap notice removed); max_tokens has no effect on MolochAI, so ask for less or split the task\n",
    );
  }
  if (usage) {
    process.stderr.write(
      `[usage] input=${usage.inputTokens} output=${usage.outputTokens} total=${usage.totalTokens}\n`,
    );
  }
}

/** Passes text through, holding back any tail that could be the start of CAP_NOTICE. */
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
    this.found = held === CAP_NOTICE;
    return this.found ? '' : held;
  }
}

function fail(message: string): number {
  process.stderr.write(`error: ${message}\n`);
  return 1;
}

async function main(): Promise<number> {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: { model: { type: 'string' }, 'max-steps': { type: 'string' } },
  });
  const [demo, prompt] = positionals;
  const maxSteps = Number(values['max-steps'] ?? 4);
  if (!['generate', 'stream', 'tools'].includes(demo ?? '') || !(maxSteps >= 1)) {
    return fail('usage: npx tsx vercel-ai-sdk/main.ts <generate|stream|tools> [prompt] [--model id] [--max-steps n]');
  }
  const model = makeModel(values.model || process.env.MOLOCHAI_MODEL || DEFAULT_MODEL);
  try {
    if (demo === 'generate') return await runGenerate(model, prompt ?? 'In two sentences, what is a context window?');
    if (demo === 'stream') return await runStream(model, prompt ?? 'Explain TCP slow start in one paragraph.');
    return await runTools(model, prompt ?? 'How many miles is a 21.1 km half marathon?', maxSteps);
  } catch (err) {
    return fail(describeError(err));
  }
}

process.exitCode = await main();
