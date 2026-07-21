# @tenir/contract

The generated **TypeScript** half of the WS+REST message contract — the single
source of truth shared by every TS frontend (`even/`, `mobile/`, and `web/`).
`src/messages.ts` is generated from
[`../../contract/ws-messages.schema.json`](../../contract/ws-messages.schema.json)
by `make gen`; **never hand-edit it**.

Consumers import from the package name, not a relative path:

```ts
import type { ServerMessage, MicSource, Lang } from "@tenir/contract";
```

The package exports the `.ts` source directly (no build step) — both Vite
frontends and `tsc --noEmit` resolve it through the workspace. The Pydantic half
lives at `api/src/api/contract/messages.py`; see
[`../../contract/README.md`](../../contract/README.md) for the regeneration
workflow and transport conventions.
