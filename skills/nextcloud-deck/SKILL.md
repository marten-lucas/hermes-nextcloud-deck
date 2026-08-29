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
hermes skills list
```

Do not use `hermes skills inspect` as the plugin-local verification step. In
affected Hermes releases, `skills inspect` resolves Skills Hub/source entries
and can report `No skill named ... found in any source` for a valid local skill.
Use `hermes plugins doctor nextcloud-deck-platform` for plugin registration and
check `skills/nextcloud-deck/SKILL.md` on disk for the plugin-local skill.
