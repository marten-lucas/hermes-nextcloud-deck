# Hermes Nextcloud Deck Plugin

Nextcloud Deck Plattform-Plugin fuer Hermes Agent.

Aktueller Stand:

- Phase 1: Plugin-Skelett und Deck-Authentifizierung
- Phase 2: Ingestion fuer konfigurierte Boards und Hermes-zugewiesene Karten
- Phase 3: Deck-Writeback fuer Kommentare, Spaltenwechsel und Checkbox-Updates
- Phase 4: optionale Talk-Reminder bei ausbleibender menschlicher Reaktion

Weitere Konzept- und Planungsdetails:

- [Anforderungen](</home/marten/Development/kiga AI/hermes-nextcloud-deck/docs/requirements.md>)
- [MVP-Plan](</home/marten/Development/kiga AI/hermes-nextcloud-deck/docs/mvp-plan.md>)
- [User Manual](</home/marten/Development/kiga AI/hermes-nextcloud-deck/docs/user-manual.md>)

## Was das Plugin heute kann

- verbindet sich als normaler Nextcloud-User mit Deck
- pollt die sichtbaren Boards des Users
- verarbeitet nur Boards aus der Konfiguration
- verarbeitet in diesen Boards nur Karten, die dem konfigurierten Hermes-Deck-User zugewiesen sind
- uebergibt Karteninhalt, Kommentare und Markdown-Checkboxen als Hermes-Kontext
- kann spaeter im Lauf:
  - Kommentare auf Karten schreiben
  - Karten anhand eines manuellen Status-Mappings in andere Spalten verschieben
  - Checkboxen in der Beschreibung aktualisieren
- kann optional einen Talk-Reminder vorbereiten, statt sofort in zwei Kanaele zu posten

## Was das Plugin heute noch nicht kann

- automatisches Board-Onboarding
- automatisches Stack-Mapping
- LLM-gestuetzte Mapping-Vorschlaege
- vollstaendige Produktions-UX fuer Talk-Reminder

## Konfiguration

Pflichtvariablen:

- `NEXTCLOUD_BASE_URL`
- `NEXTCLOUD_USERNAME`
- `NEXTCLOUD_APP_PASSWORD`
- `NEXTCLOUD_DECK_HERMES_USER_ID`

Optional:

- `NEXTCLOUD_DECK_POLL_INTERVAL_SECONDS`

Beispiel fuer `config.yaml`:

```yaml
platforms:
  nextcloud_deck:
    enabled: true
    extra:
      base_url: "https://cloud.example.org"
      username: "hermes"
      app_password: "app-password"
      hermes_user_id: "hermes-user"
      poll_interval_seconds: 30
      boards:
        - board_id: "7"
          stack_mapping:
            todo: "Backlog"
            in_progress: "In Arbeit"
            blocked: "Blockiert"
            done: "Erledigt"
          reminder_via_talk: false
          talk_room_id: ""
          patience: "medium"
```

## Lokale Tests

Die aktuelle Test-Suite:

```bash
cd '/home/marten/Development/kiga AI/hermes-nextcloud-deck'
python -m unittest discover -s tests -p 'test*.py' -v
```

## Entwicklungshinweis

Das Plugin ist bewusst nach dem Hermes-Plugin-Guide aufgebaut:

- `plugin.yaml`
- `adapter.py`

Referenz:

- https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters
