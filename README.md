# Hermes Nextcloud Deck Plugin

Native Hermes platform adapter for Nextcloud Deck.

## Design decisions

The integration is deliberately small and safe:

- only explicitly configured boards are ingested;
- only cards assigned to the configured Hermes user are normal triggers;
- explicit mentions remain supported as a fallback trigger;
- Deck comments are sent using JSON as documented by the Deck API;
- API/network errors are surfaced instead of being silently converted to empty lists;
- polling reports a connection only after an API request succeeds;
- plugin-provided skills use Hermes' namespaced skill mechanism;
- reminders are explicitly marked as not implemented rather than pretending to schedule them.

## Repository layout

```text
.
├── __init__.py             # Plugin entrypoint (exports register)
├── adapter.py              # Core platform adapter: polling, triggers, send()
├── client.py               # Nextcloud Deck REST client (Cards, Stacks, Labels, Assignees, OCS)
├── workflow.py             # Workflow, Phasen, Gates & Subtask-Logik (Konzept v5)
├── identity.py             # DeckIdentityResolver: actor resolution, group lookup, ContextVars
├── outbound.py             # Outbound message categorization (lifecycle/error/suppress/forward)
├── state.py                # Card snapshot & deduplication state
├── plugin.yaml             # Plugin metadata (v0.5.0)
├── docs/
│   └── workflow-concept-v5.md # Vollständiges Workflow-Konzept
├── skills/
│   └── nextcloud-deck/     # Bundled skill (namespaced via ctx.register_skill)
│       └── SKILL.md
└── tests/
    ├── test_phase1_adapter.py
    ├── test_platform_contract.py
    └── test_workflow_gates.py
```

## Installation

Copy or clone the plugin into your Hermes plugins directory:

```bash
git clone <repo> ~/.hermes/plugins/nextcloud-deck/
```

Set the required environment variables (`NEXTCLOUD_DECK_BASE_URL`,
`NEXTCLOUD_DECK_USERNAME`, `NEXTCLOUD_DECK_APP_PASSWORD`) — either in
`~/.hermes/.env` or via `hermes plugins install` prompts — and enable the
platform in `config.yaml` (see Configuration below).

## Configuration

```yaml
platforms:
  nextcloud_deck:
    enabled: true
    extra:
      base_url: "https://cloud.example.org"
      username: "hermes"
      app_password: "APP_PASSWORD"
      hermes_user_id: "hermes"
      home_channel: "log"   # optional: Cron-/Cross-Platform-Zustellung ins Logfile statt auf Karte
      poll_interval_seconds: 30
      backlog_format_quiet_seconds: 300   # optional: siehe unten
      max_in_progress: 1                  # optional: WIP-Limit, 0 = unbegrenzt
      boards:
        - board_id: "7"
```

Do not omit `boards`. An empty board list means the adapter connects but intentionally ingests no cards.

`home_channel` is optional. When omitted, Deck has **no** home channel — the
global `NEXTCLOUD_HOME_CHANNEL` (a Talk room) is **not** reused, so no misleading
"no home channel" notice appears. Set it to a Deck card target
(`deck:board:<id>:card:<id>`) to receive cron/cross-platform messages on a card,
or to `"log"` to route them to `~/.hermes/logs/deck-home.log` instead.

`backlog_format_quiet_seconds` is optional (default `300`). It controls the
Backlog format correction: cards whose `lastModified` is more recent than this
value are treated as *actively edited* and are **not** touched, so an in-progress
human edit is never interrupted by an automatic template fix. Set it to `0` to
disable the quiet window entirely (idle and recently-edited cards are always
corrected). The value can also be set via the environment variable
`NEXTCLOUD_DECK_BACKLOG_FORMAT_QUIET_SECONDS`.

`max_in_progress` is optional (default `0` = unlimited). It is the
**work-in-progress limit**: the maximum number of cards that may sit in active
columns (`Todo`/`Ready`/`Running`) at the same time. When the limit is reached,
new cards in those columns are **not** started until a running one moves to
`Review`/`Blocked`/`Done`. Set `1` to force strictly sequential execution so the
model never multitasks across cards. Also settable via
`NEXTCLOUD_DECK_MAX_IN_PROGRESS`.

## Diagnostics

```bash
systemctl --user restart hermes-gateway.service
hermes plugins doctor nextcloud-deck-platform
hermes skills list
```

Important: `hermes skills inspect` is a Skills Hub/source resolver in the affected
Hermes releases; it is not a reliable verifier for a `SKILL.md` shipped inside a
platform plugin. A `No skill named ... found in any source` result therefore does
not prove that the plugin itself failed to load. Verify plugin loading with
`hermes plugins doctor` and inspect the plugin's local `skills/` directory directly.

### Board suitability check

On `connect()`, the adapter validates every configured board against the workflow
model and logs a report. It resolves each canonical column
(`Backlog` | `Triage` | `Todo` | `Ready` | `Running` | `Review` | `Blocked` | `Done`)
via `stack_mapping` (by stack id **or** title) with an exact-title fallback, and
warns loudly if a **required** column is missing. Required columns:

- `running` — the agent works actively here
- `review` — the agent hands off for human acceptance/freigabe (Gate 2 forces this)
- `blocked` — the agent reports `PLAN CHANGE REQUESTED` / missing prerequisites here

Look for `Deck Board-Eignung` (suitable) or `Deck Board NICHT geeignet` in
`gateway.log` right after startup. A non-suitable board is logged but does **not**
block the gateway.

## Tests

```bash
python -m unittest discover -s tests -p 'test*.py' -v
```

## Outbound message filtering

Every outgoing message is categorized before it is written as a Deck comment
(loop prevention, mirroring the upstream gateway's own noise filters):

| Category | Behavior |
| --- | --- |
| **Lifecycle** (`Gateway restarting/shutting down/online`, draining) | Silently discarded — Deck has no presence concept |
| **Suppress** (retry/rate-limit chatter, compression noise, stall watchdog, internal `[CONTEXT …]`/`[ASYNC …]` markers, silence narration like `*(silent)*` or a bare `.`) | Silently discarded |
| **Error** (⚠️-prefixed failures, provider/tool errors) | Posted as comment with `🚫 **Fehler**` prefix |
| **Forward** | Posted as normal comment |

Card actions via `metadata` bypass the filter — they are structural operations,
not chat messages (see [Workflow model](#workflow-model-v5) below).

## How to use it (human workflow)

### Board columns

The board uses generic lifecycle columns (no per-phase columns — the *semantic*
phase lives in labels):

| Column | Meaning |
| --- | --- |
| **Backlog** | Idea collection. The agent **ignores** cards here — it is the "on/off" switch. Write and refine the card description freely; nothing runs until you move it out. |
| **Triage** | "Pick it up and look at it" — the agent does a rough pass and classifies (type/risk). Optional. |
| **Todo** | Accepted for work, waiting to start. |
| **Ready** | Context prepared, ready for an agent run. Functionally treated like `Running`. |
| **Running** | The agent actively works here (plan phase, then optionally execute phase). |
| **Review** | The agent is done (or the plan awaits approval) — **waits for a human**. |
| **Blocked** | Semantic hold (reason in a comment). Reachable from any phase. |
| **Done** | Only after human acceptance (the agent can never set this — Gate 2). |

### Labels (tags) and what they do

These are **namespaced labels** (`hermes/*`). You set `type` and `risk` **yourself**
before starting; the agent sets `phase` and `approval` as it works.

| Label | Who sets it | Effect |
| --- | --- | --- |
| `hermes/type:documentation` | human | Documentation/writing task — read-only by default. |
| `hermes/type:research` | human | Investigation/research task — read-only by default. |
| `hermes/type:troubleshooting` | human | Diagnose a problem — read-only; a fix requires your approval. |
| `hermes/type:implementation` | human | A system **change** — always requires approval before executing (Gate 1). |
| `hermes/risk:low` | human | Low risk → progressive autonomy (no mandatory gate). |
| `hermes/risk:medium` | human | Forces Gate 1 approval before execute. |
| `hermes/risk:high` | human | Forces Gate 1 approval **and** blocks destructive tool calls until you approve (Gate 3). |
| `hermes/phase:plan` | agent | Read-only conception: the agent writes Objective/Plan/Subtasks, makes no production changes. |
| `hermes/phase:execute` | agent | Execute the approved plan, check off subtasks with evidence. |
| `hermes/approval:required` | agent | The plan waits for your approval. |
| `hermes/approval:approved` | human | You approve the plan — the agent may move to `execute`. |

### The flow, step by step

1. **Backlog** — write the card description (objective, what you want). The agent does nothing here.
2. **Set `type` and `risk`** labels (your risk assessment — the agent does **not** guess these).
3. **Move to `Todo`** (or `Triage`) — this is the start trigger.
4. The agent works in `Running` (plan → optionally execute).
   - With `max_in_progress` set (e.g. `1`), cards are processed **strictly one
     at a time** — no model multitasking. New cards in `Todo`/`Ready`/`Running`
     wait until the active one reaches `Review`/`Blocked`/`Done`.
5. When done, the agent moves the card to **`Review`** (and sets `hermes/approval:required`) — it never moves to `Done` (Gate 2).
6. **You review** the result and move it to **`Done`**.

### Risk & type — how the flow differs

- **`risk:low` + `documentation`/`research`/`troubleshooting`** → **no mandatory gate**. The agent may go from `plan` to `execute` autonomously. Example: *"Write the current time into a Collective page"* — it just runs and reports.
- **`type:implementation`** (any risk) or **`risk:medium`/`high`** (any type) → **Gate 1 is mandatory**. The agent must produce a plan first, move to `Review`, and wait for `hermes/approval:approved` before it may touch anything.
- **`risk:high`** additionally enables **Gate 3**: destructive tool calls (`delete`, `restart`, `reset`, auth changes, …) are *technically blocked* until you explicitly approve in a comment — regardless of type.

Practically for a "write a page" task: set `hermes/type:documentation` + `hermes/risk:low`
and the whole run completes without waiting. For anything that *changes systems*,
use `hermes/type:implementation` (and a matching `risk`) so the approval gate
protects you.

## Workflow model (v5)

The adapter implements the agentic Kanban workflow described in
[`docs/workflow-concept-v5.md`](docs/workflow-concept-v5.md). Board columns are
generic lifecycle states (`Backlog` | `Triage` | `Todo` | `Ready` | `Running` |
`Review` | `Blocked` | `Done`); the *semantic* phase lives in namespaced labels.

### Phases (labels)

| Label | Meaning |
| --- | --- |
| `hermes/phase:plan` | Read-only conception — agent writes plan + subtasks, makes no productive changes |
| `hermes/phase:execute` | Execute the approved plan, check off subtasks with evidence |
| `hermes/type:documentation` / `:troubleshooting` / `:implementation` / `:research` | Task type |
| `hermes/risk:low` / `:medium` / `:high` | Risk level |
| `hermes/approval:required` / `:approved` | Human approval for the `execute` phase |

### Gates (enforced in `workflow.py`)

| Gate | Rule |
| --- | --- |
| **Gate 1** (Plan → Execute) | Mandatory only for `hermes/type:implementation` or `hermes/risk:medium|high|critical` (progressive autonomy). The agent may not move to `Running`/set `execute` without `hermes/approval:approved`. `documentation`/`troubleshooting`/`research` with `risk:low` may proceed autonomously. |
| **Gate 2** (Execute → Done) | The agent may never move a card to `Done`; it moves to `Review` for human acceptance. |
| **Gate 3** (Destructive) | For `hermes/risk:high`, destructive tool calls (`delete`, `restart`, `reset`, `deactivate`, …) are **technically blocked** by a `pre_tool_call` hook until explicit approval is given. The blocklist is extensible via `extra.destructive_tool_patterns`. |

### Card actions via `deck_card_action` tool

The agent mutates cards through the **`deck_card_action`** tool (not `send_message`,
which cannot pass structured metadata). Parameters: `card_id`, `target_status`,
`description`, `assign_labels`, `remove_labels`, `assign_user`, `unassign_user`:

```json
{
  "card_id": "106",
  "target_status": "review",
  "description": "# Objective\n...\n## Subtasks\n- [x] 1. Analyse\n- [ ] 2. Umsetzung",
  "assign_labels": ["hermes/approval:required"],
  "remove_labels": ["hermes/phase:plan"]
}
```

- `target_status` resolves via board config `status_mapping`/`stack_mapping` or a
  case-insensitive stack-title match.
- `assign_user` resolves a username/display-name to a Nextcloud UID via the
  provisioning API (`identity.resolve_user_uid`).
- Subtasks are Markdown checkboxes in the description (Deck has no checklist API);
  progress is derived from them, not stored separately.

### Trigger & dedup

- `stack_id` **and** `labels` are part of the dedup fingerprint (`state.py`), so a
  human column move *or* label change (e.g. `hermes/approval:approved`) re-triggers
  the agent.
- After each agent run the dedup baseline is re-set to the current card state
  (`adapter._rebaseline_card`), so the agent's own label/description changes don't
  loop — but a later human edit does.

## Identity propagation

The adapter resolves the human actor behind a card trigger (comment author
priority, fallback via `MCP_IDENTITY_FALLBACK_USER` → `NEXTCLOUD_DECK_USERNAME`)
and propagates identity to downstream MCP tools via the `hermes-x-on-behalf`
plugin:

- A `PrincipalContext` (user, groups, conversation `deck:board:<id>:card:<id>`)
  is built per card event and applied with a token-based context manager
  (leak-proof reset after processing)
- **Fallback actors** (no human comment author, or the bot itself — matched via
  `hermes_user_id` and the configured `bot_aliases`) become `kind=system`
  principals: they never receive personal or team memory and carry no
  conversation id. This prevents unrelated card events from piling into the
  bot account's personal memory scope.
- Derived `X-On-Behalf-Of` / `X-User-Groups` headers on the session source
- ContextVars consumed by the plugin's HTTP interceptors
- The conversation id `deck:board:<id>:card:<id>` participates in deterministic
  memory routing via the explicit `memory.conversation_scopes` list in
  `~/.hermes/config.yaml` (board titles are user-owned and are NOT parsed for
  memory tags). Prefix matching respects segment boundaries: `deck:board:3`
  matches `deck:board:3:card:44` but not `deck:board:30`.

## License

MIT
