---
name: nextcloud-deck
description: Work with the Nextcloud Deck platform adapter and its configured card workflow.
---

# Nextcloud Deck

Use this skill when a task explicitly concerns the Hermes Nextcloud Deck integration.

## Runtime model

- The adapter polls only boards listed under `platforms.nextcloud_deck.extra.boards`.
- A card is processed when the configured Hermes user is assigned, or when the Hermes user/username is explicitly mentioned in the card description or a comment.
- Hermes replies are written as Deck comments.
- Card state is deduplicated in memory for the lifetime of the gateway process.
- Talk reminders and automatic board onboarding are not implemented by this plugin version.

## Configuration

```yaml
platforms:
  nextcloud_deck:
    enabled: true
    extra:
      base_url: "https://cloud.example.org"
      username: "hermes"
      app_password: "..."
      hermes_user_id: "hermes"
      poll_interval_seconds: 30
      boards:
        - board_id: "7"
```

## Diagnostics

After changing the plugin:

```bash
systemctl --user restart hermes-gateway.service
hermes plugins doctor nextcloud-deck-platform
hermes skills inspect nextcloud-deck-platform:nextcloud-deck
```

The skill name is namespaced by the plugin. A bare `hermes skills inspect nextcloud-deck` lookup is not expected to resolve a plugin-provided skill.
