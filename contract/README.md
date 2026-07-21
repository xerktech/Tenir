# WS message contract

`ws-messages.schema.json` is the **single source of truth** for every JSON frame
exchanged over the api WebSocket. Both sides are generated from it — never
hand-edit the generated files:

| Target | Output | Generator |
|---|---|---|
| Client (TS) | `packages/contract/src/messages.ts` | `json-schema-to-typescript` |
| Api (Python) | `api/src/api/contract/messages.py` | `datamodel-code-generator` (Pydantic v2) |

## Regenerate

From the repo root:

```bash
npm install            # once, installs the TS generator
make gen               # regenerates BOTH ts + py (py needs datamodel-code-generator)
```

`make gen-ts` / `make gen-py` run them individually. The Python generator is
installed with `pip install datamodel-code-generator` (see
`api/` for a venv).

## Transport conventions

- **Audio is binary, not JSON.** Raw PCM (16 kHz, signed 16-bit little-endian,
  mono) is sent as binary WebSocket frames, matching the G2 mic natively. The
  JSON messages in the schema are control (client→server) and results
  (server→client).
- **Discriminated by `type`.** Every message carries a `type` string literal;
  `ClientMessage` and `ServerMessage` are the two top-level unions.
- **Direction** is documented per message in the schema `description`.

## Message map

Client → server: `session.start`, `mic.switch`, `session.end`, `ping`.

Server → client: `session.ready`, `caption.partial`, `caption.final`,
`pong`, `error`.
