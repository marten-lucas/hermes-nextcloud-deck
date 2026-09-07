# Konzept v5 — Workflow-Design für `hermes-nextcloud-deck`

> **Stand:** 2026-09-07 · Konsolidiert aus drei Experten-Briefings (Kanban-Phasenmodell, Hermes-Kanban-Analyse, agentic Workflow Best Practices) und dem konzeptionellen Review des Repository-Adapters.

---

## 1. Architektur-Grundsatz

**Nextcloud Deck ist die persistente, menschliche Task-Oberfläche. Hermes bleibt die Ausführungs-Engine.**

Es wird **keine zweite Workflow-Engine** im Deck-Adapter gebaut. Deck beschreibt den Lebenszyklus der Aufgabe, Hermes entscheidet über Ausführung, Goal-Loop, Retry und Recovery.

```
Deck-Karte (Task Contract + Plan + Subtasks + Evidence)
        │
        ▼
Adapter (Phase-Erkennung, Gates, Prompt-Bau)
        │
        ▼
Hermes Agent (Capabilities je Phase, Goal-Loop)
        │
        ▼
Deck (Kommentare, Description-Updates, Label-Wechsel)
```

---

## 2. Board-Design: Generische Spalten (Hermes-nah)

Das Board nutzt **generische Lifecycle-Spalten**, die 1:1 auf Hermes Kanban abgebildet sind — **keine fachlichen Phasen-Spalten** (Konzept/Umsetzung etc.). Die fachliche Phase steckt als **Label** in der Karte.

```
BACKLOG │ TRIAGE │ TODO │ READY │ RUNNING │ REVIEW │ BLOCKED │ DONE
```

| Spalte | Bedeutung |
|---|---|
| **Backlog** | Ideen & Sammlung — hier starten Karten, die **noch nicht bearbeitet werden sollen**. Der Adapter ignoriert Backlog-Karten. Der Mensch schiebt die Karte nach TRIAGE, wenn er sie in Arbeit geben will. |
| **Triage** | Neu in Bearbeitung, noch nicht angefasst — Agent analysiert grob und stuft ein |
| **Todo** | Zur Bearbeitung angenommen, wartet auf Start |
| **Ready** | Kontext vorbereitet, bereit für Agentenlauf |
| **Running** | Agent arbeitet aktiv daran |
| **Review** | Agent ist fertig (oder Plan wartet auf Freigabe) — **wartet auf einen Menschen** |
| **Blocked** | Semantischer Wartezustand, erreichbar aus jeder Phase (Grund im Kommentar) |
| **Done** | Erst nach menschlicher Abnahme |

**Wichtige Mechanik (bereits im Repo vorhanden):**
- `stack_mapping` ist reine Board-Config — neue Spalten = keine Code-Änderung.
- `stack_id` **und `labels`** sind im Dedup-Fingerprint (`state.py`) enthalten → jeder Spaltenwechsel *und* jeder Label-Wechsel (z. B. `hermes/approval:approved`) triggert den Agenten erneut. Ein manueller Move **oder** eine Freigabe per Label ist damit ein echtes Event.
- Loop-Schutz: Nach jedem erfolgreichen Agent-Lauf wird die Dedup-Baseline auf den aktuellen Karten-Zustand zurückgesetzt — vom Agenten selbst gesetzte Labels/Description-Änderungen wirken nicht als neuer Trigger.

---

## 3. Phase als Label (nicht als Spalte)

Fachliche Semantik wird über **namensgespace Labels** abgebildet (Deck-Labels-API `assignLabel`/`removeLabel` ist verfügbar):

| Label | Bedeutung |
|---|---|
| `hermes/phase:plan` | Konzeptphase — read-only, produziert Plan |
| `hermes/phase:execute` | Umsetzungsphase — arbeitet freigegebenen Plan ab |
| `hermes/type:documentation` | Doku-Aufgabe |
| `hermes/type:troubleshooting` | Diagnose/Troubleshooting |
| `hermes/type:implementation` | Systemänderung |
| `hermes/type:research` | Recherche |
| `hermes/risk:low` / `:medium` / `:high` | Risikostufe |
| `hermes/approval:required` | Plan wartet auf Freigabe |
| `hermes/approval:approved` | Plan freigegeben (darf execute) |

Der Adapter liest die Labels, um Phase + Capabilities zu bestimmen. Der Mensch sieht am Board sofort, was los ist, und gibt per Label-Wechsel frei.

---

## 4. Capabilities & Autonomy (Agentenmodi je Phase)

Der Adapter baut je Phase einen strukturierten Prompt-Kontext:

**Phase `plan`:**
```
phase: plan
capabilities:
  research: true
  read_systems: true
  modify_systems: false
  create_files: true        # nur Drafts
  destructive_operations: false
objective: Plane die Aufgabe. Erzeuge Objective, Approach, Subtasks,
Acceptance Criteria in der Karten-Description. Keine produktiven Änderungen.
Danach: Karte nach REVIEW, Label hermes/approval:required setzen.
```

**Phase `execute`:**
```
phase: execute
capabilities:
  research: true
  read_systems: true
  modify_systems: true
  destructive_operations: false   # außer explizit freigegeben
objective: Setze den freigegebenen Plan ab. Arbeite Subtasks sequenziell ab,
hake sie evidenzbasiert ab. Erfinde den Plan nicht neu — bei Plan-Mismatch:
BLOCKED + Kommentar "PLAN CHANGE REQUESTED".
```

---

## 5. Task Contract in der Description

Standardisierte Description-Struktur (Subtasks als Markdown-Checkboxen — die Deck-REST-API hat **keine** Checklist-Endpunkte, Subtasks leben daher in der Description):

```markdown
# Objective
...

# Context
...

# Acceptance Criteria
- [ ] ...

# Plan
## Approach
...
## Subtasks
- [ ] 1. Bestehenden Recovery Flow analysieren
- [ ] 2. Flow konfigurieren
- [ ] 3. End-to-End testen
- [ ] 4. Dokumentieren

# Constraints
- ...

# Verification
...

# Result
_pending_
```

**Regeln:**
- **Plan-Phase:** Agent schreibt Objective / Approach / Subtasks / Acceptance Criteria in die Description.
- **Execute-Phase:** Agent hakt Subtasks ab (`- [x]`) und ergänzt bei wichtigen/riskanten Subtasks eine Evidence-Zeile (z.B. `Evidence: flow id abc123, GET /api/... → 200`).
- **Plan ist nach Approval immutabel:** Änderungen nur als neuer Abschnitt `## Plan v2` + Kommentar `PLAN CHANGE REQUESTED` + Label zurück auf `hermes/approval:required`. Kein stiller Scope-Ausweich.
- **Acceptance Criteria und Subtasks bleiben getrennt:** Subtasks = *Wie arbeite ich ab?* / Acceptance Criteria = *Woran erkenne ich Erfolg?*
- Progress wird aus Checkboxen berechnet (`3/8 subtasks`), nicht separat gespeichert.

---

## 6. Gates (technische Sperren im Adapter)

In `_move_card_to_status()` und beim Label-Handling:

| Gate | Regel |
|---|---|
| **Gate 1** (Plan → Execute) | Agent darf `hermes/phase:execute` nicht selbst setzen, solange `hermes/approval:required` — **aber nur bei `hermes/type:implementation` oder `hermes/risk:medium|high|critical`** (progressive Autonomy, §9). Freigabe = Mensch setzt `hermes/approval:approved` (Label-Wechsel ist ein echtes Event, siehe §2). |
| **Gate 2** (Execute → Done) | Agent darf die Karte nicht selbst nach DONE schieben. Er schiebt nach REVIEW und fordert Abnahme an. |
| **Gate 3** (Destructive) | Bei `hermes/risk:high` zusätzlich: destruktive Aktionen (Löschen, Neustart, Konfigänderungen an Auth-Systemen) nur nach expliziter Freigabe im Kommentar. **Technisch durchgesetzt** über einen `pre_tool_call`-Hook, der destruktive Tool-Namen bei `risk:high` ohne Freigabe blockt (Muster erweiterbar via `destructive_tool_patterns`). |

Verstoß → Adapter lehnt ab, Agent postet `WAITING FOR APPROVAL`-Kommentar. Keine bestehenden Transitionen ohne Mapping — das Verhalten bleibt deterministisch.

---

## 7. Assignee-Handoff

- Agent arbeitet: Assignee = hermes
- Wartet auf Freigabe/Abnahme: Agent weist den Menschen zu (`assignUser`-API verfügbar) oder entfernt sich — die Karte „leuchtet" für den Menschen.

---

## 8. Kommentare als strukturierte Events

Kommentare sind nicht nur Chat, sondern **Events/Entscheidungen** mit klarem Format:

```
🤖 PLAN COMPLETE — wartet auf Freigabe
🤖 SUBTASK 2/4 COMPLETE — Evidence: ...
🤖 PLAN CHANGE REQUESTED — Grund: ...
🤖 BLOCKED — Reason: missing SMTP credentials; Required: ...
👤 APPROVED
🤖 EXECUTION COMPLETE — Verification: PASS — bitte abnehmen
```

---

## 9. Task-Type-Workflows (fortschreitende Autonomie)

Je nach `hermes/type` und Risiko läuft die Karte unterschiedlich durch die generischen Spalten:

| Typ | Typischer Flow | Gates |
|---|---|---|
| `documentation` (risk:low) | BACKLOG → TRIAGE → RUNNING (plan) → REVIEW → RUNNING (execute) → REVIEW → DONE | keine Pflicht-Gates (Gate 1 optional) |
| `troubleshooting` (risk:low) | BACKLOG → TRIAGE → RUNNING (plan/diagnose) → REVIEW → optional RUNNING (execute) → REVIEW → DONE | Fix nur nach Freigabe |
| `implementation` (risk:high) | BACKLOG → TRIAGE → RUNNING (plan) → REVIEW (**Gate 1**) → RUNNING (execute) → REVIEW (**Gate 2**) → DONE | Pflicht-Gates |

**Umsetzung:** Gate 1 ist typ-/risiko-bewusst (`workflow.py: gate1_is_mandatory`).
Verpflichtend ist es für `hermes/type:implementation` sowie für `hermes/risk:medium|high|critical`.
Dokumentation, Troubleshooting und Recherche mit `risk:low` dürfen ohne menschliche Freigabe
eigenständig von `plan` nach `execute` wechseln.

---

## 10. Die drei Referenz-Aufgaben

### 10.1 „Schreibe eine Doku über die IT-Infrastruktur als Collective"
`hermes/type:documentation`, `risk:low`, kein Approval-Gate nötig.
1. BACKLOG → TRIAGE: Karte anlegen, Agent stuft ein
2. RUNNING (`phase:plan`): Agent untersucht bestehende Collectives, schreibt Gliederung + Subtasks in die Description → REVIEW
3. Mensch prüft Struktur, ergänzt/kürzt Subtasks, schiebt zurück → RUNNING (`phase:execute`)
4. Agent schreibt Collective-Seiten, hakt Subtasks ab, postet Link
5. REVIEW: Mensch liest gegen → DONE

### 10.2 „Finde heraus, warum sich User X auf Android nicht anmelden kann"
`hermes/type:troubleshooting`, `risk:low`.
1. RUNNING (`phase:plan`): Agent analysiert Logs (Nextcloud audit, Authentik, Reverse Proxy), formuliert Befund + Fix-Vorschlag → REVIEW
2. Mensch: „mach den Fix" → RUNNING (`phase:execute`)
3. Agent spielt Fix ein, hakt Subtasks ab, testet → REVIEW
4. Mensch nimmt ab → DONE

### 10.3 „Implementiere in Authentik einen Reset-Password-Flow"
`hermes/type:implementation`, `risk:high`, `hermes/approval:required`.
1. RUNNING (`phase:plan`): Ist-Zustand analysieren, Konzept, Subtasks, Risiken → REVIEW + `approval:required` (**Gate 1**)
2. Mensch prüft Plan (besonders wichtig: Auth-System) → `hermes/approval:approved`
3. RUNNING (`phase:execute`): Agent baut Flow in Authentik, hakt Subtasks evidenzbasiert ab
4. REVIEW: Agent führt End-to-End-Test durch, postet Verification (**Gate 2**)
5. Mensch nimmt ab → DONE

---

## 11. Konkrete Adapter-Änderungen

| # | Änderung | Datei | Aufwand |
|---|---|---|---|
| 1 | Stack-Titel + Labels in den Prompt (`_process_card`) — Agent kennt seine Spalte/Phase | `adapter.py` | klein |
| 2 | Phase-/Capability-Prompt je Label | `adapter.py` (oder neu `phases.py`) | mittel |
| 3 | Gate-Sperre für `target_status` + Label-Wechsel | `adapter.py` | klein |
| 4 | Label-Assign/Remove + AssignUser/UnassignUser im Client | `client.py` | klein (API-Endpunkte bestätigt) |
| 5 | Subtask-Parser (Markdown-Checkboxen) + Progress-Berechnung | `adapter.py` | mittel |
| 6 | SKILL.md: Workflow-Regeln (Phase-Verhalten, Gates, Evidence, kein stiller Scope-Wechsel, Backlog = nicht anfassen) | `skills/nextcloud-deck/SKILL.md` | klein |
| 7 | Task-Template (Description-Skelett) bei leerer Description | `adapter.py` | optional, später |

**Fakten zum Repo-Stand (Ausgangslage):**
- **Gate-Sperre vorhanden** — Agent kann `metadata["target_status"]` nicht beliebig setzen: `done`/`backlog` sind immer gesperrt (Gate 2), `running` in der Plan-Phase je nach Typ/Risiko (Gate 1, progressive Autonomy).
- Agent **kennt seine Spalte & Labels** (Prompt enthält Titel, Beschreibung, Spalte, Labels, Subtask-Fortschritt, letzten Kommentar).
- Client unterstützt **Checklist-/Label-/Assignee-API** (`assignLabel`/`removeLabel`/`assignUser`/`unassignUser` — Endpunkte in der Deck-REST-API). Checklist-Endpunkte existieren nicht → Subtasks als Markdown-Checkboxen in der Description.
- Dedup-Fingerprint enthält `stack_id` **und `labels`** → Spaltenwechsel *und* Label-Wechsel = neues Event (mit Re-Baseline nach jedem Lauf als Loop-Schutz).
- SKILL.md dokumentiert `target_status`/Description-Änderung und die Label-/Assignee-Metadaten.

---

## 12. Design-Prinzipien (Zusammenfassung)

1. **Deck ist UI, nicht Workflow-Engine** — Hermes besitzt Ausführung und Task-Lifecycle.
2. **State beschreibt Scheduling, nicht Semantik** — `running` heißt „Agent arbeitet", nicht „Implementation". Fachliche Phase steckt im Label.
3. **Semantik steckt im Task Contract** — Objective, Context, Acceptance Criteria, Constraints, Plan, Evidence in der Description.
4. **Plan before execution** — kein Handel ohne freigegebenen Plan bei riskanten Aufgaben.
5. **Subtasks evidenzbasiert abhaken** — nicht behaupten, sondern belegen.
6. **Plan ist nach Approval immutabel** — Replan nur als expliziter, wieder genehmigter Schritt.
7. **Review ≠ Done** — Agent kann Review anfordern, der Mensch entscheidet.
8. **Blocked ist semantisch** — mit Grund und benötigtem Input, nicht nur rote Spalte.
9. **Progressive Autonomy** — je höher das Risiko, desto mehr menschliche Gates.
10. **Backlog ist separates Eintrittstor** — Ideen warten dort, bis der Mensch sie bewusst nach TRIAGE zieht; der Agent fasst sie dort nicht an.
