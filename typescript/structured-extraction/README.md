# Structured extraction (TypeScript)

Extract typed records from free text with one [zod](https://zod.dev) schema doing three jobs: the
JSON Schema in the prompt, the runtime validation of the reply, and the static TypeScript type.

## What it does

- Defines the shape once with zod (`MeetingNotes`: title, decisions, action items with owner,
  task and an optional due date, open questions).
- Converts it with `z.toJSONSchema()` and puts the result in the system message inside a fenced
  ```` ```json ```` block. MolochAI has no JSON mode (`response_format` is silently dropped), so
  the prompt is the only way to ask for a shape.
- Streams the reply and collects it, which avoids the roughly 100-second edge timeout on plain
  requests while a reasoning model thinks.
- Extracts the first JSON object (bare, in a code fence, or after prose), validates it with
  `MeetingNotes.safeParse()`, and on failure sends the zod issues back once for a corrected reply.
- From then on the data is typed as `MeetingNotes`; the stderr summary uses those types.

Compared with [../../python/json-output](../../python/json-output), which validates against a
JSON Schema file with `jsonschema`, this shows the schema-first approach that fits a TypeScript
codebase.

## Dependencies

`openai` 7.x (requires Node.js 22 or newer) and `zod` 4, plus `tsx` to run TypeScript directly.
All pinned in [../package.json](../package.json).

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
| `MOLOCHAI_MODEL` / `--model` | no | `glm-5.3-flash` | Model id |
| file (positional) | yes | | Meeting notes as UTF-8 text |

[sample-notes.txt](sample-notes.txt) is a made-up meeting note to try it on.

## Run

From the `typescript/` folder:

```bash
npx tsx structured-extraction/main.ts structured-extraction/sample-notes.txt
```

## Expected output

From the mock server, which answers a fenced JSON Schema with a placeholder instance (strings
`"example"`, one item per array), so nothing here comes from the notes. A real model extracts the
actual decisions and action items.

```text
$ npx tsx structured-extraction/main.ts structured-extraction/sample-notes.txt
{
  "title": "example",
  "decisions": [
    "example"
  ],
  "action_items": [
    {
      "owner": "example",
      "task": "example",
      "due": "example"
    }
  ],
  "open_questions": [
    "example"
  ]
}
[extracted] 1 decision(s), 1 action item(s) (0 without a due date), 1 open question(s)
```

## Error handling

- Invalid reply: one repair round that sends back each zod issue as `at <path>: <message>`, then
  exit 1.
- Unreadable input file or API error: exit 1 with a message. The SDK retries 429 and 5xx twice.
- Validation checks shape and types, not facts. Review extracted data before acting on it.
