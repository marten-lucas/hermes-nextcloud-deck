# User Manual – Hermes Nextcloud Deck

Dieses Dokument beschreibt, wie du den **aktuellen Implementierungsstand** testest.

Es geht also nicht um das spaetere Zielbild, sondern um das, was im Repo heute bereits lauffaehig ist.

## 1. Voraussetzungen

Du brauchst:

- eine erreichbare Nextcloud-Instanz mit aktivem **Deck**
- einen dedizierten Hermes-Nextcloud-User
- ein App-Passwort fuer diesen User
- mindestens ein Deck-Board, auf das dieser User Zugriff hat
- mindestens eine Karte, die dem Hermes-User zugewiesen ist

Optional fuer spaetere Reminder-Tests:

- das bestehende Nextcloud-Talk-Plugin fuer Hermes
- einen Talk-Raum, in den spaeter erinnert werden soll

## 2. Was du vor dem Testen vorbereiten solltest

### Hermes-User

Der Hermes-User muss:

- sich bei Nextcloud authentifizieren koennen
- Zugriff auf das relevante Deck-Board haben
- auf den zu testenden Karten als zugewiesener User erscheinen

### Test-Board

Lege ein Board an, z. B.:

- `Backlog`
- `In Arbeit`
- `Blockiert`
- `Erledigt`

Lege mindestens eine Karte an und weise den Hermes-User zu.

Beispielbeschreibung:

```md
Diese Aufgabe dient zum Testen des Hermes Deck Plugins.

- [ ] Erstes Teilziel
- [ ] Zweites Teilziel
```

## 3. Plugin-Konfiguration

Der aktuelle MVP erwartet relevante Boards explizit in der Konfiguration.

Beispiel:

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

## 4. Was im aktuellen Stand automatisch passiert

Nach dem Start des Plugins:

1. Hermes verbindet sich mit Nextcloud Deck
2. das Plugin liest die sichtbaren Boards
3. es verarbeitet nur Boards aus `extra.boards`
4. in diesen Boards werden nur Karten verarbeitet, die dem konfigurierten `hermes_user_id` zugewiesen sind
5. aus der Kartenbeschreibung werden Checkboxen als Unteraufgaben erkannt
6. Kommentare der Karte werden mit in den Kontext aufgenommen

## 5. Was du konkret testen kannst

## Test A – Board Discovery

Ziel:

- pruefen, ob das Plugin dein konfiguriertes Board findet

Erwartung:

- keine Auth-Fehler
- das Board wird erkannt

## Test B – Karten-Ingestion

Ziel:

- pruefen, ob eine Hermes-zugewiesene Karte als Work Item erkannt wird

Vorbereitung:

- Karte muss im konfigurierten Board liegen
- Hermes-User muss zugewiesen sein

Erwartung:

- die Karte wird ingestiert
- Titel, Beschreibung, Kommentare und Checkboxen werden Teil des Hermes-Kontexts

## Test C – Checkbox-Semantik

Ziel:

- pruefen, ob Checkboxen als Unteraufgaben interpretiert werden

Vorbereitung:

- Beschreibung mit Markdown-Checkboxen

Erwartung:

- Checkboxen werden intern als strukturierte Liste verarbeitet

## Test D – Kommentar-Writeback

Ziel:

- pruefen, ob Hermes einen Kommentar auf die Karte schreiben kann

Erwartung:

- auf der Karte erscheint ein neuer Kommentar

Hinweis:

Das passiert nicht allein durch Polling, sondern wenn der Hermes-Lauf spaeter eine Antwort ueber `send()` auf das Work Item zurueckgibt.

## Test E – Spaltenwechsel

Ziel:

- pruefen, ob ein konfiguriertes Status-Mapping funktioniert

Vorbereitung:

- `stack_mapping` muss gesetzt sein

Erwartung:

- wenn Hermes den Status `todo`, `in_progress`, `blocked` oder `done` zurueckmeldet, wird die Karte in die gemappte Spalte verschoben

## Test F – Checkbox-Update

Ziel:

- pruefen, ob Hermes Checkboxen abhaken oder ergaenzen kann

Erwartung:

- die Beschreibung der Karte wird aktualisiert
- Nicht-Checklist-Text soll erhalten bleiben

## 6. Talk-Reminder testen

Der aktuelle Stand implementiert **Reminder-Logik**, kein aggressives Mirroring.

Konfiguration:

```yaml
boards:
  - board_id: "7"
    stack_mapping:
      todo: "Backlog"
      in_progress: "In Arbeit"
      blocked: "Blockiert"
      done: "Erledigt"
    reminder_via_talk: true
    talk_room_id: "room-token-oder-room-id"
    patience: "medium"
```

Wichtig:

- Deck bleibt primaer
- Talk wird nur als Follow-up gedacht
- ohne installiertes Nextcloud-Talk-Plugin kann kein Reminder delegiert werden

Aktueller technischer Stand:

- Reminder wird geplant
- Reminder wird nur gesendet, wenn keine menschliche Reaktion erkannt wird
- die konkrete Praxis im echten Hermes-Setup haengt davon ab, dass das Talk-Plugin aktiv und erreichbar ist

## 7. Bekannte aktuelle Grenzen

- Boards werden noch nicht automatisch aufgenommen
- Mapping ist noch manuell
- es gibt noch keinen User-Flow fuer Board-Onboarding
- `patience` ist aktuell technisch eher ein einfaches Profil als eine echte LLM-Dringlichkeitslogik
- Talk-Reminder ist als Architektur schon da, aber noch kein voll ausgebauter Produktions-Workflow

## 8. Lokale Test-Suite

Zum schnellen technischen Check:

```bash
cd '/home/marten/Development/kiga AI/hermes-nextcloud-deck'
python -m unittest discover -s tests -p 'test*.py' -v
```

Aktuell decken die Tests unter anderem ab:

- Konfigurationsvalidierung
- Board-Discovery
- Karten-Ingestion
- Snapshot-/Aenderungslogik
- Kommentar-Writeback
- Status-Mapping
- Checkbox-Update
- Reminder-Scheduling
- Reminder-Delegation
- Aufloesen von Remindern bei menschlicher Reaktion

## 9. Empfohlener Testablauf fuer dich

1. Nextcloud-Zugangsdaten und `hermes_user_id` setzen
2. ein Test-Board mit klaren Spalten anlegen
3. `board_id` und `stack_mapping` in die Konfiguration eintragen
4. Hermes-User einer Testkarte zuweisen
5. Beschreibung mit Checkboxen versehen
6. Plugin/Gateway starten
7. pruefen, ob die Karte ingestiert wird
8. danach Kommentar-, Spalten- und Checkbox-Writeback testen
9. erst danach optional Talk-Reminder aktivieren
