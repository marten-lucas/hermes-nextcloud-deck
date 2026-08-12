# Hermes Nextcloud Deck – MVP-Plan

## Planungsprinzip

Der Plan folgt strikt dem **MVP-Prinzip**:

1. erst ein **laufendes minimales Produkt**
2. dann ein **minimal nuetzliches Produkt**
3. danach Komfort, Pairing und Robustheit

Die spaetere Plugin-Implementierung soll sich an die Hermes-Doku aus dem Guide [adding-platform-adapters](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters) halten.

## Phase 0 – Repo- und Konzeptbasis

Ziel:

- Repo lokal initialisieren
- Remote verbinden
- Anforderungen und Phasenplan festschreiben

Ergebnis:

- dieses Repo ist als Arbeitsbasis vorbereitet
- das Team hat eine stabile Scope-Definition

## Phase 1 – Laufendes minimales Produkt

Ziel:

Ein Hermes-Plugin `nextcloud-deck`, das von Hermes geladen werden kann und technisch sauber startet.

Umfang:

- `plugin.yaml` anlegen
- `adapter.py` anlegen
- Adapter von `BasePlatformAdapter` ableiten
- `connect()` / `disconnect()` / `send()` / `get_chat_info()` minimal implementieren
- Pflichtkonfiguration laden:
  - Base URL
  - Username
  - App Password
  - Hermes Deck User ID
- einfacher Polling-Loop startet und kann `GET /boards` erfolgreich ausfuehren
- gefundene Boards werden geloggt

Noch nicht enthalten:

- Session-Erzeugung aus Karten
- Kommentare
- Spaltenwechsel
- Talk-Spiegelung
- Pairing

Definition of done:

- Plugin laesst sich gemaess Hermes-Plugin-Doku laden
- `connect()` funktioniert
- Nextcloud-Authentifizierung klappt
- Board-Liste wird erfolgreich gelesen

## Phase 2 – Minimal nuetzlicher Deck-MVP

Ziel:

Aus zugewiesenen Deck-Karten werden tatsaechlich Hermes-Eingaben.

Umfang:

- Karten des Hermes-Users erkennen
- pro Karte stabile Session-Source erzeugen
- Beschreibung, Kommentare und Checkboxen in Hermes-Kontext ueberfuehren
- relevante Karten-Aenderungen erkennen
- lokale Change-Marker / Loop-Schutz einfuehren

Wichtige Vereinfachung fuer den MVP:

- **Stack-Mapping wird zunaechst manuell konfiguriert**
- kein automatischer Pairing-Flow in dieser Phase
- einfacher Board-Add-Flow bleibt **TBD**; fuer den MVP werden relevante Boards ueber Konfiguration erfasst

Definition of done:

- eine dem Hermes-User zugewiesene Karte kann einen Hermes-Lauf ausloesen
- Checkboxen werden als Unteraufgaben im Kontext sichtbar
- wiederholte Polls erzeugen keine Endlosschleifen

## Phase 3 – Erste Writeback-Faehigkeit

Ziel:

Hermes kann sichtbar auf Deck zurueckschreiben.

Umfang:

- Deck-Kommentare schreiben
- Karten per Spalten-Mapping verschieben
- Checkboxen in der Beschreibung aktualisieren

Definition of done:

- Hermes kann nach einem Lauf einen Kommentar auf der Karte hinterlassen
- Hermes kann die Karte in eine konfigurierte Zielspalte verschieben
- Hermes kann Checkboxen abhaken oder ergaenzen

## Phase 4 – Optionale Talk-Spiegelung

Ziel:

Deck bleibt die Primaerquelle, Talk wird zum optionalen Informationskanal.

Umfang:

- pro Board aktivierbares Talk-Mirroring
- Konfiguration fuer Zielraum
- kompakte Spiegelnachrichten

Definition of done:

- bei aktiviertem Mirroring wird eine Deck-Aktion zusaetzlich in Talk gemeldet
- bei deaktiviertem Mirroring bleibt alles ausschliesslich in Deck

## Phase 5 – Board-Onboarding / Pairing

Ziel:

Das manuelle Stack-Mapping aus dem MVP wird durch einen gefuehrten Board-Onboarding-Flow ergaenzt oder ersetzt.

Umfang:

- unbekannte Boards erkennen
- Stack-Namen heuristisch einem kanonischen Statusmodell zuordnen
- optional LLM-gestuetzten Mapping-Vorschlag erzeugen
- Mapping-Bestaetigung durch Menschen einholen
- Board-State persistieren

Wichtig:

Dies ist **bewusst nicht Phase 1**, weil der bestehende Hermes-Pairing-Flow keine nachweisbare Formular-/Metadaten-Unterstuetzung fuer Stack-Mappings bietet.

Der konkrete einfache UX-Flow fuer "Board hinzufuegen" bleibt bis dahin **TBD**. Bereits notierte Ideen:

- Discovery per API + Relevanz ueber Hermes-Zuweisung
- LLM erzeugt Mapping-Vorschlag aus Spaltennamen
- Rueckfrage im Channel nur bei Unsicherheit
- spaetere Bestaetigung ueber Kommentar, Talk oder UI

Definition of done:

- ein neues relevantes Board kann ohne statische Vorabkonfiguration aufgenommen werden
- das resultierende Mapping wird lokal gespeichert und wiederverwendet

## Phase 6 – Robustheit und Produktreife

Ziel:

Das Plugin wird fuer echten Alltagseinsatz stabilisiert.

Umfang:

- Retry-/Fehlerstrategie
- bessere Konfliktbehandlung bei Beschreibungsaenderungen
- ETag/lastModified-basierte Optimierungen
- gezielte Tests
- Dokumentation und Betriebsanleitung

Definition of done:

- die Kernfluesse sind getestet
- Fehler sind sichtbar und diagnostizierbar
- die Bedienung ist dokumentiert

## Empfohlene technische Prioritaet

Wenn die Umsetzung beginnt, sollte die Reihenfolge sein:

1. bootfaehiges Plugin
2. Karten als Inbound-Quelle
3. Kommentar-Writeback
4. Spaltenwechsel
5. Checkbox-Writeback
6. Talk-Mirroring
7. Board-Onboarding
