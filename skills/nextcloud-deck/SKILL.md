---
name: nextcloud-deck
description: Work with the Nextcloud Deck platform adapter and its configured card workflow.
---

# Nextcloud Deck

Use this skill when a task explicitly concerns the Hermes Nextcloud Deck integration and its agentic workflow.

## Runtime & Workflow Model (v5)

- **Board-Spalten (Hermes Kanban):** `Backlog` | `Triage` | `Todo` | `Ready` | `Running` | `Review` | `Blocked` | `Done`.
  - **Backlog:** Reine Ideensammlung. Karten im Backlog werden vom Agenten **ignoriert**, bis ein Mensch sie nach Triage/Todo zieht.
  - **Done:** Darf **nur von Menschen** gesetzt werden. Der Agent verschiebt fertige Aufgaben nach `Review` für die Abnahme.
- **Phasen als Labels:**
  - `hermes/phase:plan`: Read-only Konzeption. Agent analysiert Logs/Systeme und verfasst Plan + Subtasks in der Description. Keine produktiven Änderungen!
  - `hermes/phase:execute`: Umsetzung nach freigegebenem Plan. Subtasks werden sequenziell abgearbeitet und evidenzbasiert mit `[x]` abgehakt.
  - `hermes/approval:required` / `hermes/approval:approved`: Menschliche Freigabe für Phase `execute`.
- **Workflow Gates (technische Schutzmechanismen):**
  - **Gate 1 (Plan → Execute):** Nur für `hermes/type:implementation` oder `hermes/risk:medium|high|critical` verpflichtend. Ohne `hermes/approval:approved` darf der Agent dann weder nach `Running` schieben noch selbst in Phase `execute` wechseln. Für `documentation`/`troubleshooting`/`research` mit `risk:low` (progressive Autonomy) ist die Freigabe **optional** — der Agent darf eigenständig in die Umsetzung.
  - **Gate 2 (Execute → Done):** Der Agent darf eine Karte nicht selbst nach `Done` schieben; er schiebt nach `Review`.
  - **Gate 3 (Destructive):** Bei `hermes/risk:high` sind destruktive Tool-Aufrufe (Löschen, Neustart, Reset, Deaktivieren …) **technisch blockiert**, solange keine explizite Freigabe vorliegt. Der `pre_tool_call`-Hook verweigert solche Aufrufe; der Agent muss vorab eine Bestätigung im Kommentar einholen.
  - **Plan Mismatch:** Stellt der Agent fest, dass der Plan nicht funktioniert, darf er ihn **nicht stillschweigend neu erfinden**, sondern setzt Status auf `Blocked` und postet `🤖 PLAN CHANGE REQUESTED`.

## Trigger & Freigabe

- Ein menschenseitiger **Spaltenwechsel ODER Label-Wechsel** (z. B. `hermes/approval:approved`) ist ein echtes Event und triggert den Agenten erneut — die Labels sind Teil des Dedup-Fingerprints.
- Der Adapter setzt die Dedup-Baseline **nach** jedem Agent-Lauf auf den aktuellen Karten-Zustand zurück. Vom Agenten selbst gesetzte Labels/Description-Änderungen lösen damit keinen Loop aus; ein späterer Eingriff des Menschen aber schon.

## Card-Aktionen (Tool `deck_card_action`)

Um den Workflow-Vertrag zu erfüllen, nutze **immer** das Tool `deck_card_action`
— **nicht** einen reinen Text-Kommentar. Der Kommentar (`send_message`) kann die
Karte nicht strukturell verändern; nur `deck_card_action` setzt Description,
Status, Labels und Assignee. Die `card_id` steht im Kontext (`Karten-ID (card_id)`).

```json
{
  "card_id": "106",
  "target_status": "review",
  "description": "# Objective\n...\n## Subtasks\n- [x] 1. Analyse\n- [ ] 2. Umsetzung",
  "assign_labels": ["hermes/approval:required"],
  "remove_labels": ["hermes/phase:plan"],
  "assign_user": "marten",
  "comment": "Optionaler Kommentar, der im selben Schritt gepostet wird."
}
```

Wichtige Ablaufregeln:

- **Plan fertig:** `deck_card_action` mit `description` (Plan inkl. Subtasks),
  `target_status: "review"` und `assign_labels: ["hermes/approval:required"]`.
- **Subtask abgehakt:** `deck_card_action` mit aktualisierter `description`
  (Checkbox auf `[x]`, ggf. `Evidence: ...`-Zeile).
- **Abnahme übergeben:** `deck_card_action` mit `target_status: "review"`
  (niemals `done` — Gate 2) und ggf. `assign_user` auf den Menschen.
- **Plan-Mismatch:** `target_status: "blocked"` + `comment: "🤖 PLAN CHANGE REQUESTED"`.
- **Struktur + Kommentar in einem Schritt:** Nutze das Feld `comment`, um im
  selben `deck_card_action`-Aufruf eine sichtbare Nachricht zu posten — so brauchst
  du keinen zweiten, separaten Text-Kommentar.

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
          stack_mapping:
            backlog: "Ideen"
            triage: "Triage"
            todo: "Todo"
            ready: "Bereit"
            running: "In Arbeit"
            review: "Review"
            blocked: "Blockiert"
            done: "Erledigt"
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

### Board-Eignungsprüfung

Beim Start prüft der Adapter jedes konfigurierte Board und loggt einen Report
(`Deck Board-Eignung OK` / `Deck Board NICHT geeignet`). Benötigte Spalten:
`Running`, `Review`, `Blocked` (die anderen sind optional). Die Spalten-Titel
sollten exakt den kanonischen Namen entsprechen (`Todo`, `Running`, `Review`,
`Blocked`, `Done`) oder via `stack_mapping` gemappt werden — sonst kann der
Agent Karten nicht in die richtige Spalte schieben.
