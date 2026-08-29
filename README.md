# Hermes Nextcloud Deck Plugin

Nextcloud Deck Plattform-Plugin fuer Hermes Agent.

Aktueller Stand:

- Phase 1: Plugin-Skelett und Deck-Authentifizierung
- Phase 2: Ingestion fuer konfigurierte Boards und Hermes-zugewiesene Karten
- Phase 3: Deck-Writeback fuer Kommentare, Spaltenwechsel und Checkbox-Updates

Deferiert / bewusst nicht Teil des aktuellen Scope:

- Talk-Reminder
- Board-Onboarding mit automatischem Mapping
- generische LLM-Mapping-Logik

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
- schreibt Kommentare auf die Karte zurueck
- verschiebt Karten anhand eines manuellen Status-Mappings in konfigurierten Ziel-Stack
- aktualisiert Checkboxen in der Kartenbeschreibung

## Was das Plugin bewusst noch nicht kann

- automatisches Board-Onboarding
- automatisches Stack-Mapping
- LLM-gestuetzte Mapping-Vorschlaege
- Talk-Reminder als eigener Produktiv-Workflow

Der aktuelle Scope ist bewusst auf den Hermes-Plugin-Guide ausgerichtet: ein natives Platform-Plugin mit `plugin.yaml`, `adapter.py` und `register(ctx)`. Zusätzliche Features wie Talk-Reminder oder Onboarding bleiben als separate Verfeinerungsstufen bewusst offen.

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

## Hermes-Plugin-Guide Alignment

Das Plugin folgt bewusst dem offiziellen Hermes-Plugin-Pattern fuer native Platform-Adapter:

- `plugin.yaml` definiert Plugin-Metadaten, `requires_env`/`optional_env` und die Plattform-Registrierung
- `adapter.py` enthaelt die Adapter-Implementierung und `register(ctx)`
- `__init__.py` bleibt bewusst leicht und importiert nur den Register-Hook
- Das Plugin wird als eigenstaendiges Plugin-Verzeichnis in der Hermes-Plugin-Umgebung geladen, ohne Core-Code zu aendern

Aktuell ist der Scope bewusst begrenzt auf das, was in der offiziellen Doku als native platform adapter beschrieben wird. Talk-Reminder und automatisches Board-Onboarding sind nicht Teil dieser Doku-Alignment und werden separat entschieden.

Referenz:

- https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
