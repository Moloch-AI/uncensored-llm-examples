// Structured extraction with one zod schema doing three jobs: the JSON Schema
// in the prompt, the runtime validation of the reply, and the static type.
//
//   npx tsx structured-extraction/main.ts structured-extraction/sample-notes.txt
//
// MolochAI has no JSON mode (`response_format` is silently dropped), so the
// schema goes into the prompt as a fenced ```json block, the reply is parsed
// leniently, validated with zod, and repaired once by sending the validation
// issues back. Validated JSON goes to stdout.

import { readFile } from 'node:fs/promises';
import { parseArgs } from 'node:util';
import OpenAI, { APIError } from 'openai';
import { z } from 'zod';

const DEFAULT_BASE_URL = 'https://api.molochai.dev/api/v1';
const DEFAULT_MODEL = 'glm-5.3-flash';
const CAP_NOTICE = '\n\n[Response is too large for your current plan. Try a smaller request.]';

const MeetingNotes = z.object({
  title: z.string().min(1).describe('A short title for the meeting'),
  decisions: z.array(z.string()).describe('Decisions that were made, one per entry'),
  action_items: z.array(
    z.object({
      owner: z.string().describe('Team or role responsible, as written in the notes'),
      task: z.string(),
      due: z.string().nullable().describe('Due date exactly as written, or null if none is given'),
    }),
  ),
  open_questions: z.array(z.string()).describe('Questions the meeting left open'),
});
type MeetingNotes = z.infer<typeof MeetingNotes>;

type Message = OpenAI.Chat.ChatCompletionMessageParam;

async function main(): Promise<number> {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: { model: { type: 'string' } } });
  const [file] = positionals;
  if (!file) return fail('usage: npx tsx structured-extraction/main.ts <notes.txt> [--model id]');
  const apiKey = process.env.MOLOCHAI_API_KEY?.trim();
  if (!apiKey) {
    return fail(
      'MOLOCHAI_API_KEY is not set. Create a key at https://dash.molochai.dev/account?utm_source=github&utm_medium=oss&utm_campaign=uncensored-llm-examples, or run against the mock server (see local-development/README.md).',
    );
  }

  let notes: string;
  try {
    notes = await readFile(file, 'utf8');
  } catch (err) {
    return fail(`could not read ${file}: ${(err as Error).message}`);
  }

  const client = new OpenAI({
    apiKey,
    baseURL: process.env.MOLOCHAI_BASE_URL?.trim() || DEFAULT_BASE_URL,
    timeout: 60_000,
    maxRetries: 2,
  });
  const model = values.model || process.env.MOLOCHAI_MODEL || DEFAULT_MODEL;
  const schema = JSON.stringify(z.toJSONSchema(MeetingNotes), null, 2);
  const messages: Message[] = [
    {
      role: 'system',
      content: `You extract structured data from meeting notes. Reply with one JSON object that matches this JSON Schema, and nothing else: no prose, no code fences.\n\n\`\`\`json\n${schema}\n\`\`\``,
    },
    { role: 'user', content: `Notes:\n<<<\n${notes}\n>>>` },
  ];

  for (const attempt of [1, 2]) {
    let reply: string;
    try {
      reply = await complete(client, model, messages);
    } catch (err) {
      return fail(describeError(err));
    }

    const data = extractJson(reply);
    const parsed = data === undefined ? undefined : MeetingNotes.safeParse(data);
    if (parsed?.success) {
      report(parsed.data);
      return 0;
    }
    const problems = parsed
      ? parsed.error.issues.map((issue) => `at ${issue.path.join('.') || '(root)'}: ${issue.message}`)
      : ['the reply did not contain a JSON object'];
    process.stderr.write(`[attempt ${attempt}] invalid reply:\n  ${problems.join('\n  ')}\n`);
    if (attempt === 1) {
      messages.push(
        { role: 'assistant', content: reply },
        {
          role: 'user',
          content: `That reply does not match the schema:\n- ${problems.join('\n- ')}\nReply again with only the corrected JSON object.`,
        },
      );
    }
  }
  return fail('the reply was still invalid after one repair attempt');
}

/** The validated value is fully typed from here on. */
function report(notes: MeetingNotes): void {
  process.stdout.write(`${JSON.stringify(notes, null, 2)}\n`);
  const undated = notes.action_items.filter((item) => item.due === null).length;
  process.stderr.write(
    `[extracted] ${notes.decisions.length} decision(s), ${notes.action_items.length} action item(s) (${undated} without a due date), ${notes.open_questions.length} open question(s)\n`,
  );
}

/**
 * Stream a reply and return the answer text. Streaming avoids the ~100 s edge
 * timeout on plain requests while a reasoning model is still thinking.
 */
async function complete(client: OpenAI, model: string, messages: Message[]): Promise<string> {
  const stream = await client.chat.completions.create({ model, messages, stream: true });
  let text = '';
  for await (const chunk of stream) {
    text += chunk.choices[0]?.delta.content ?? ''; // the usage chunk has `choices: []`
  }
  return text.endsWith(CAP_NOTICE) ? text.slice(0, -CAP_NOTICE.length) : text;
}

/** Find the first JSON object in a reply that may also contain prose or code fences. */
function extractJson(reply: string): unknown {
  const text = reply.replace(/<think>[\s\S]*?<\/think>/g, '').trim();
  const candidates = [text, ...[...text.matchAll(/```(?:json)?\s*\n([\s\S]*?)```/g)].map((m) => m[1] ?? '')];
  for (let start = text.indexOf('{'); start !== -1; start = text.indexOf('{', start + 1)) {
    const end = text.lastIndexOf('}');
    if (end > start) candidates.push(text.slice(start, end + 1));
  }
  for (const candidate of candidates) {
    try {
      const value: unknown = JSON.parse(candidate);
      if (value !== null && typeof value === 'object' && !Array.isArray(value)) return value;
    } catch {
      // not JSON; try the next candidate
    }
  }
  return undefined;
}

function describeError(err: unknown): string {
  if (!(err instanceof APIError)) return String(err);
  // err.error: the inner "error" object for gateway errors (and for error
  // events inside a stream, where err.status is undefined), or the whole body
  // for validation and bare-message errors.
  const body = (err.error ?? {}) as Record<string, unknown>;
  const kind = body.type ?? body.code;
  const where = err.status === undefined ? 'stream error' : `HTTP ${err.status}`;
  const extra = ['resets_at', 'estimated_tokens', 'context_cap', 'bound_by', 'model']
    .filter((key) => key in body)
    .map((key) => `${key}=${String(body[key])}`);
  return `${where}${kind ? ` ${String(kind)}` : ''}: ${typeof body.message === 'string' ? body.message : err.message}${extra.length ? ` (${extra.join(', ')})` : ''}`;
}

function fail(message: string): number {
  process.stderr.write(`error: ${message}\n`);
  return 1;
}

process.exitCode = await main();
