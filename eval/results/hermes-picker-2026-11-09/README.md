# Native Hermes model-picker verification

The installed default profile is **AutoRouter → AutoRouter-All**. The running
Hermes gateway's `model.options` response lists the entry, and the native
`switch_model` pipeline accepts it without replacing its visible name.

The regular `~/.hermes` profile passed a fresh live check: the virtual parent
made real requests to `claude-haiku-5-5`, delegated a task to
`deepseek-v4.1-flash`, and returned the specialist result unchanged. API response
models, positive usage, parent/child identities, and the result digest were
required. `verification.json` records both picker and handoff verification;
`events.json` contains metadata only. This establishes working routing for this
account and job, not comparative capability or quota savings.

AutoRouter-All considers existing subscription allowances and verified free
models together. Availability checks rejected the listed free candidate with
HTTP 402, so it was excluded; no extra paid usage was authorized.

The facade uses a native Chat Completions client and preserves raw response
model metadata and pinned workspace headers. Other routine transports are
rejected before activation. Tests capture fake HTTP traffic to verify that a
picker visibility marker or supplied wrong account header cannot replace the
pinned login/workspace, and a wrong response model cannot pass verification.
Specialists continue to use their native provider.

`runtime-source-sha256.json` matched both installed runtime copies. Tokens,
account references, raw auth exports, private configuration backups, and user
prompts are excluded from this evidence. Credentials remain on this machine.
