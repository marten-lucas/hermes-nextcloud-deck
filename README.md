# Nextcloud Deck Platform Adapter for Hermes Agent

An official platform adapter plugin (`kind: platform`) connecting Nextcloud Deck to **Hermes Agent**.

Monitors Deck boards, stacks, and cards; extracts work items; attributes actor identity to assigned users or commenters; and provides writeback capabilities for automated project management.

## Features

- **Identity Propagation**: Dynamically resolves execution identity (`X-On-Behalf-Of` and `ContextVars`). When a card is assigned to Hermes, the bot executes under its own identity; when triggered by card comments, execution context switches to the comment author.
- **State Tracking & Deduplication**: Utilizes `DeckStateManager` and `DeckCardSnapshot` to track card mutations and prevent redundant executions or infinite polling loops.
- **Full Writeback Capabilities**: Supports posting comments via `send()`, as well as reordering/moving cards across stacks and updating card titles or descriptions via `NextcloudDeckClient`.
- **Bundled Collaboration Skill**: Automatically registers the `nextcloud-collaboration` skill rules to enforce identity, permission, and mutation guardrails in LLM sessions.

## Project Structure

```
hermes-nextcloud-deck/
├── plugin.yaml               # Plugin manifest & environment definitions
├── __init__.py               # Package entrypoint
├── adapter.py                # NextcloudDeckPlatform adapter & registration
├── client.py                 # OCS REST API client for Nextcloud Deck
├── identity.py               # Assignee vs. commenter identity resolution
├── state.py                  # Snapshot state tracking & polling deduplication
├── reminders.py              # Card reminder dispatching
├── skills/
│   └── nextcloud-collaboration/
│       └── SKILL.md          # LLM collaboration & identity guardrails
└── tests/
    └── test_phase1_adapter.py # Unit test suite
```

## Configuration

### Environment Variables

Configure the following environment variables in your environment or `.env` file:

| Variable | Required | Description | Default |
| :--- | :--- | :--- | :--- |
| `NEXTCLOUD_DECK_BASE_URL` | Yes | Base URL of your Nextcloud instance (e.g. `https://cloud.example.org`) | - |
| `NEXTCLOUD_DECK_USERNAME` | Yes | Username of the Nextcloud bot account | - |
| `NEXTCLOUD_DECK_APP_PASSWORD` | Yes | App password for authentication | - |
| `NEXTCLOUD_DECK_HERMES_USER_ID` | No | Bot User ID in Nextcloud | Same as username |
| `NEXTCLOUD_DECK_POLL_INTERVAL` | No | Polling frequency in seconds | `5.0` |

### Hermes Gateway Configuration

Add the platform entry under the `gateway.platforms` section in `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    nextcloud_deck:
      enabled: true
      extra:
        base_url: "[https://cloud.example.org](https://cloud.example.org)"
        username: "hermes"
        app_password: "YOUR_APP_PASSWORD"
        hermes_user_id: "hermes"
        poll_interval: 5.0
```

## Installation & Verification

1. Place or clone this repository into your Hermes plugins directory:
   ```bash
   cd ~/.hermes/plugins/
   git clone [https://github.com/marten-lucas/hermes-nextcloud-deck.git](https://github.com/marten-lucas/hermes-nextcloud-deck.git) nextcloud-deck-platform
   ```

2. Verify plugin manifest and runtime discovery:
   ```bash
   hermes plugins doctor nextcloud-deck-platform
   ```

3. Run unit tests directly via standard discovery:
   ```bash
   python -m unittest discover -s tests -p 'test*.py' -v
   ```

## License

MIT License. See `LICENSE` for details.