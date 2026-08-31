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
├── client.py               # Nextcloud Deck REST client (+ cloud_ocs_get for provisioning)
├── identity.py             # DeckIdentityResolver: actor resolution, group lookup, ContextVars
├── outbound.py             # Outbound message categorization (lifecycle/error/suppress/forward)
├── state.py                # Card snapshot & deduplication state
├── plugin.yaml             # Plugin metadata
├── skills/
│   └── nextcloud-deck/     # Bundled skill (namespaced via ctx.register_skill)
│       └── SKILL.md
└── tests/
    ├── test_phase1_adapter.py
    └── test_platform_contract.py
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
      poll_interval_seconds: 30
      boards:
        - board_id: "7"
```

Do not omit `boards`. An empty board list means the adapter connects but intentionally ingests no cards.

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

Card actions via metadata (`description`, `target_status`) bypass the filter —
they are structural operations, not chat messages.

## Identity propagation

The adapter resolves the human actor behind a card trigger (comment author
priority, fallback via `MCP_IDENTITY_FALLBACK_USER` → `NEXTCLOUD_DECK_USERNAME`)
and propagates identity to downstream MCP tools:

- `X-On-Behalf-Of` / `X-User-Groups` headers on the session source
- ContextVars consumed by the `hermes-x-on-behalf` plugin's HTTP interceptors

## License

MIT
