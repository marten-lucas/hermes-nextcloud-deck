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
