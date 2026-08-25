---
name: nextcloud-deck
description: Workflows und Anweisungen für Hermes Agent zur Verwaltung von Nextcloud Deck Karten, Subtasks, Anhängen und Status-Übergängen.
---

# Nextcloud Deck Agent Workflow

Dieser Skill definiert die Standards für die Interaktion mit Nextcloud Deck Boards und Karten über die Plattform `deck`.

---

## 1. Status-Steuerung & Reorder
Verwende beim Senden von Antworten das Metadaten-Feld `target_status`, um den Status einer Karte auf dem Board automatisch anzupassen. Der Adapter löst diese Schlüssel über die `config.yaml` auf die korrekten Stack-IDs auf:
* `backlog`: Aufgabe zurückstellen oder für spätere Planung parken.
* `todo`: Bereit zur Bearbeitung durch ein Teammitglied oder den Agenten.
* `in_progress`: Aktuell in aktiver Bearbeitung.
* `review`: Wartet auf Abnahme oder Rückmeldung durch den Vorstand (z. B. Marten).
* `done`: Aufgabe vollständig abgeschlossen.

---

## 2. Umgang mit Subtasks (Checklisten in der Beschreibung)
Subtasks werden innerhalb der Kartenbeschreibung als Markdown-Tasklisten geführt:

- [ ] Task 1: Rechner-Setup ausführen
- [x] Task 2: Logs analysieren

**Regeln für Subtasks:**
* **Fortschritt aktualisieren:** Wenn du einen Teilaspekt der Aufgabe erledigt hast, aktualisiere die Beschreibung der Karte über das Metadaten-Feld `new_description` (oder `description`), indem du die entsprechende Zeile von `- [ ]` auf `- [x]` setzt.
* **Neue Teilaufgaben ergänzen:** Wenn während der Bearbeitung neue Teilschritte entstehen, füge sie als neue Checkbox-Zeilen `- [ ]` am Ende der Aufgabenliste in der Beschreibung hinzu.
* **Transparenz:** Erwähne abgehakte Teilschritte kurz im Antwort-Kommentar.

---

## 3. Verknüpfung von Anhängen im Kommentar
* Referenziere hochgeladene oder generierte Dateien direkt als Markdown-Links im Kommentar-Text (z. B. `[Anhang: Bericht.pdf](https://next.cloud.kiga-gramschatz.de/f/12345)`).
* Beziehe dich in deinen Antworten explizit auf existierende Karten-Anhänge oder übermittelte Dokumente.

---

## 4. Antwort-Struktur & Kommunikationsstil
* **Direkt & Präzise:** Halte Kommentare auf Deck-Karten kurz und übersichtlich. Nutze Bullet Points für Statusberichte.
* **Anrede:** Sprich Ansprechpartner persönlich mit Vornamen an (z. B. Marten).
* **Fehler-Handling:** Falls Server-Checks oder Tool-Executions fehlschlagen, dokumentiere die konkrete Ursache transparent im Kommentar und gib Handlungsoptionen an.