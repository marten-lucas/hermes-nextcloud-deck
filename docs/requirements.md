# Hermes Nextcloud Deck – Anforderungen

## Zielbild

Dieses Plugin soll Hermes um eine **eigene Plattform fuer Nextcloud Deck** erweitern.

Deck ist dabei nicht nur ein Benachrichtigungskanal, sondern die **primaere Arbeitsquelle**:

- Karten sind die Work Items
- eine Karte soll als eigene Hermes-Session-Quelle sichtbar sein
- Hermes reagiert auf Karten, die dem Hermes-User zugewiesen sind
- Hermes meldet Fortschritt primaer ueber **Spaltenwechsel** in Deck zurueck

## Abgrenzung

Nicht Teil des aktuellen Scopes:

- direkte Nextcloud-Tasks-Integration
- Wiederverwendung des bestehenden Talk-Adapters als primaere Inbound-Quelle
- generische Trello/Jira/Planner-Unterstuetzung

## Verbindliche Produktentscheidungen

### 1. Plattformform

Es soll eine **eigene Plattform `nextcloud-deck`** entstehen, nicht eine Erweiterung von `nextcloud-talk`.

Begruendung:

- Deck-Karten sind keine Chat-Nachrichten
- die Session-Quelle soll als Deck-Objekt sichtbar sein
- Lebenszyklus, Polling und Mapping unterscheiden sich stark von Talk

Die spaetere Implementierung soll sich dabei strikt an die Hermes-Doku aus dem Guide [adding-platform-adapters](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters) halten:

- Plugin-Pfad
- `plugin.yaml`
- `adapter.py`
- `BasePlatformAdapter`
- `connect()` / `disconnect()` / `send()` / `get_chat_info()`

### 2. Identitaet / Zuweisung

Hermes arbeitet als **fester Nextcloud-User**.

Dieser Hermes-User muss in den Channel-/Plugin-Settings explizit als **Deck-User-ID** konfiguriert werden.

Die Karte gehoert Hermes genau dann, wenn diese User-ID auf der Karte zugewiesen ist.

### 3. Fortschrittsmodell

Der eigentliche Status soll in **Deck-Spalten** modelliert sein, nicht in Kommentaren.

Kommentare bleiben zusaetzlich erlaubt fuer:

- knappe Rueckmeldungen
- Begruendungen
- Zwischenstaende
- Pairing-/Onboarding-Nachrichten

### 4. Checkbox-Semantik

Checkboxen in der Deck-Beschreibung sollen von Hermes als **Unteraufgaben** verstanden werden.

Das bedeutet fuer die spaetere Prompt-/Event-Aufbereitung:

- Markdown-Checkboxen werden in strukturierter Form in den Eingabekontext ueberfuehrt
- Hermes darf Checkboxen abhaken
- Hermes darf Checkboxen ergaenzen

### 5. Talk-Spiegelung

Talk ist nur **optional**.

Pro Board soll konfigurierbar sein:

- ob ueberhaupt nach Talk gespiegelt wird
- in welchen Talk-Raum gespiegelt wird

## Board-Discovery: Wie kommt ein Board zum Adapter?

Das Board kommt nicht ueber einen manuellen Import in den Adapter, sondern ueber die Deck-API und die Identitaet des Hermes-Users.

## Geplanter Discovery-Flow

1. Das Plugin wird mit Nextcloud-Zugangsdaten und der festen Hermes-Deck-User-ID gestartet.
2. `connect()` startet einen Polling-Loop als Inbound-Transport.
3. Der Adapter authentifiziert sich als Hermes-User gegen Nextcloud.
4. Er ruft `GET /boards` auf und sieht damit alle Boards, auf die dieser User Zugriff hat.
5. Fuer jedes sichtbare Board laedt er Stack-/Karteninformationen nach.
6. Sobald eine Karte gefunden wird, auf der die konfigurierte Hermes-Deck-User-ID zugewiesen ist, wird dieses Board fuer die inhaltliche Verarbeitung relevant.
7. Existiert fuer dieses Board noch kein Spalten-Mapping, wechselt das Board in den Zustand `pairing_required`.

## Relevante Deck-API-Belege

Aus der Deck-Doku:

- `GET /boards - Get a list of boards`
- `GET /boards/{boardId}/stacks - Get stacks`
- `PUT /boards/{boardId}/stacks/{stackId}/cards/{cardId}/assignUser - Assign a user to a card`
- `PUT /boards/{boardId}/stacks/{stackId}/cards/{cardId}/reorder - Change the sorting order of a card`
- `PUT /boards/{boardId}/stacks/{stackId}/cards/{cardId} - Update card details`
- `POST /cards/{cardId}/comments - Create a new comment`

Aus derselben Doku geht zudem hervor, dass die Karten-`description` **Markdown** ist.

## Anforderungen an die Konfiguration

### Pflicht

- `NEXTCLOUD_BASE_URL`
- `NEXTCLOUD_USERNAME`
- `NEXTCLOUD_APP_PASSWORD`
- `NEXTCLOUD_DECK_HERMES_USER_ID`

### Optional

- Poll-Intervall
- Standardverhalten fuer Talk-Mirroring
- Standard-Talk-Raum
- spaetere manuelle Mapping-Vorgaben fuer MVP-Phase 1/2

## MVP-Konfigurationsform fuer Boards

Im MVP werden relevante Boards bewusst ueber Konfiguration erfasst.

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
      boards:
        - board_id: "7"
          stack_mapping:
            todo: "Backlog"
            in_progress: "In Arbeit"
            blocked: "Blockiert"
            done: "Erledigt"
```

In dieser Stufe gilt:

- nur konfigurierte Boards werden inhaltlich ingestiert
- innerhalb dieser Boards werden nur Karten verarbeitet, die dem Hermes-User zugewiesen sind
- das Stack-Mapping ist noch manuell

## Pairing-Flow: Beweislage aus Hermes-Source/Doku

Die bestehende Hermes-Pairing-Implementierung ist **kein Formular- oder Metadaten-Onboarding**, sondern ein **Code-basiertes Freigabesystem fuer Messaging-Nutzer**.

### Beleg 1: Pairing-Source

In der Hermes-Quelle [gateway/pairing.py](/tmp/1786535372287-copilot-tool-output-ce65f8.txt:1) steht direkt:

- `DM Pairing System`
- `Code-based approval flow for authorizing new users on messaging platforms`
- `unknown users receive a one-time pairing code that the bot owner approves via the CLI`

Die eigentlichen Datenstrukturen und Freigabepfade zeigen ebenfalls nur:

- `platform`
- `user_id`
- `user_name`
- `request_id`
- `code`

Siehe dazu insbesondere:

- [approve_code](/tmp/1786535372287-copilot-tool-output-ce65f8.txt:665)
- [approve_request](/tmp/1786535372287-copilot-tool-output-ce65f8.txt:735)
- [list_pending](/tmp/1786535372287-copilot-tool-output-ce65f8.txt:770)

Dort gibt es **keine Felder fuer Board-Metadaten, Mapping-Informationen oder Zusatzformulare**.

### Beleg 2: Web-Dashboard-Doku

Die Hermes-Dashboard-Doku beschreibt fuer Pairing nur API-Endpunkte wie:

- `GET /api/pairing`
- `POST /api/pairing/approve` mit Body `{platform, code}`
- `POST /api/pairing/revoke` mit Body `{platform, user_id}`
- `POST /api/pairing/clear-pending`

Auch dort gibt es **keinen Formularpfad fuer strukturierte Zusatzdaten** wie Stack-Mappings.

## Schlussfolgerung fuer Deck

Der bestehende Hermes-Pairing-Flow kann **nicht belegt** als Mechanismus fuer Board-Formulare oder Mapping-Zusatzinfos genutzt werden.

Was wir sinnvoll wiederverwenden koennen, ist nur das **konzeptionelle Muster**:

- unbekanntes Objekt wird entdeckt
- Hermes fordert menschliche Bestaetigung an
- nach der Bestaetigung wird lokaler Zustand persistiert

Fuer Deck braucht es deshalb einen **eigenen Board-Onboarding-/Mapping-Flow**.

## Mapping-Idee fuer spaetere Ausbaustufen

Eine sinnvolle spaetere Ausbaustufe ist:

- Hermes liest die vorhandenen Deck-Spalten selbst per API
- Hermes erzeugt daraus per LLM einen Mapping-Vorschlag auf das kanonische Statusmodell
- wenn die Zuordnung klar ist, kann Hermes den Vorschlag direkt als Entwurf speichern
- wenn die Zuordnung unsicher ist, fragt Hermes im konfigurierten Channel nach

Beispiele:

- `Backlog` -> `todo`
- `In Arbeit` -> `in_progress`
- `Wartet auf Kunde` -> `blocked`
- `Erledigt` -> `done`

Wichtig:

- diese Idee ist **explizit nicht MVP**
- fuer den MVP bleibt das Mapping **konfigurationsbasiert**
- der genaue UX-Flow fuer Rueckfrage/Bestaetigung ist noch **TBD**

## Detaillierter Board-Pairing-Flow

Der folgende Flow ist ein **Zielbild** fuer spaetere Phasen, nicht der MVP-Mechanismus.

### Zustand 1: discovered

- Board wird ueber `GET /boards` gefunden
- es existiert lokal noch kein Board-State

### Zustand 2: candidate

- auf dem Board wird mindestens eine Karte gefunden, die dem konfigurierten Hermes-User zugewiesen ist

### Zustand 3: pairing_required

- Board ist fachlich relevant
- aber es gibt noch kein persistiertes Mapping von Deck-Spalten zu kanonischen Hermes-Statuswerten

### Zustand 4: pairing_proposed

Der Adapter erstellt aus den vorhandenen Stack-Namen einen Vorschlag, z. B.:

- `Backlog` -> `todo`
- `In Arbeit` -> `in_progress`
- `Review` -> `review`
- `Erledigt` -> `done`

### Zustand 5: paired

Ein Mensch bestaetigt oder korrigiert das Mapping.

Das Ergebnis wird board-lokal persistiert:

- Board-ID
- erkannte Stack-IDs
- Mapping Hermes-Status -> Deck-Stack
- Talk-Mirroring-Einstellungen

## TBD: einfacher Flow zum Hinzufuegen eines Boards

Der einfache User-Flow fuer "Wie kommt ein neues Board aktiv in den Adapter?" ist noch **TBD**.

Aktuell festgehaltene Ideen:

1. **MVP-Variante**
   - Board wird ueber Konfiguration aktiviert
   - Mapping wird ebenfalls ueber Konfiguration hinterlegt

2. **Discovery-Variante**
   - Hermes entdeckt Boards automatisch ueber `GET /boards`
   - ein Board wird relevant, sobald eine Karte dem Hermes-User zugewiesen ist

3. **LLM-Mapping-Variante**
   - Hermes liest Spaltennamen selbst
   - erzeugt per LLM ein Mapping
   - fragt nur bei Unsicherheit nach

4. **Channel-Rueckfrage**
   - Hermes fragt im konfigurierten Channel nach, wenn Mapping oder Board-Aktivierung unklar ist

Fuer die Umsetzung blockiert dieser Punkt den MVP **nicht**, da der MVP bewusst auf Konfiguration setzt.

## Wichtige MVP-Entscheidung

Da der bestehende Hermes-Pairing-Flow keine Formular-/Zusatzdaten-Eingabe nachweisbar unterstuetzt, sollte der **erste lauffaehige MVP nicht mit automatischem Pairing starten**.

Stattdessen:

- MVP: manuelles Mapping in der Plugin-Konfiguration
- spaeter: eigener Board-Onboarding-Flow fuer Mapping

## Gewuenschter Ziel-Output des Plugins

Wenn Hermes auf einer zugewiesenen Karte arbeitet, soll das Plugin spaeter mindestens koennen:

- Karte als Hermes-Session-Quelle anlegen
- Titel, Beschreibung, Kommentare und Checkbox-Unteraufgaben an Hermes uebergeben
- Hermes-Antworten in Deck-Kommentare schreiben
- Karte je nach Ergebnis in die passende Spalte verschieben
- optional nach Talk spiegeln
