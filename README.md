# Hermes Nextcloud Deck Plugin

Native Hermes platform adapter for Nextcloud Deck.

## 0.3.0 focus

This release deliberately makes the integration smaller and safer:

- only explicitly configured boards are ingested;
- only cards assigned to the configured Hermes user are normal triggers;
- explicit mentions remain supported as a fallback trigger;
- Deck comments are sent using JSON as documented by the Deck API;
- API/network errors are surfaced instead of being silently converted to empty lists;
- polling reports a connection only after an API request succeeds;
- plugin-provided skills use Hermes' namespaced skill mechanism;
- reminders are explicitly marked as not implemented rather than pretending to schedule them.

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
hermes skills inspect nextcloud-deck-platform:nextcloud-deck
```

Plugin skills are namespaced as `plugin:skill` by Hermes; a bare skill name is not expected to resolve.

## Tests

```bash
python -m unittest discover -s tests -p 'test*.py' -v
```
