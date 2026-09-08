"""Workflow-, Phasen-, Gate- und Subtask-Logik für hermes-nextcloud-deck (Konzept v5)."""

from __future__ import annotations

import contextvars
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Standard-Präfixe für Hermes Deck-Labels
LABEL_PREFIX_PHASE = "hermes/phase:"
LABEL_PREFIX_TYPE = "hermes/type:"
LABEL_PREFIX_RISK = "hermes/risk:"
LABEL_PREFIX_APPROVAL = "hermes/approval:"

PHASE_PLAN = "plan"
PHASE_EXECUTE = "execute"

APPROVAL_REQUIRED = "required"
APPROVAL_APPROVED = "approved"

# Task-Typen, bei denen das menschliche Gate 1 (Plan -> Execute) zwingend ist.
# Alle anderen Typen (documentation, troubleshooting, research) bzw. risk=low
# dürfen eigenständig von plan nach execute wechseln (progressive Autonomy).
GATE1_MANDATORY_TYPES = {"implementation"}

# Risikostufen, die Gate 1 unabhängig vom Typ verpflichtend machen.
GATE1_MANDATORY_RISKS = {"medium", "high", "critical"}

# Generische Lifecycle-Spalten nach Hermes Kanban
STATUS_BACKLOG = "backlog"
STATUS_TRIAGE = "triage"
STATUS_TODO = "todo"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_REVIEW = "review"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"

# Default-Status, die durch Gates für den Agenten gesperrt sind
# - done darf NIE direkt vom Agenten angesteuert werden (erfordert menschliche Abnahme nach review)
# - backlog ist die Ideensammlung, die der Agent nicht selbst ansteuern soll
GATED_AGENT_STATUSES: Set[str] = {STATUS_DONE, STATUS_BACKLOG}

# Alle kanonischen Lifecycle-Spalten aus dem v5-Konzept (Reihenfolge = Board-Layout).
CANONICAL_STATUSES: Tuple[str, ...] = (
    STATUS_BACKLOG,
    STATUS_TRIAGE,
    STATUS_TODO,
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_REVIEW,
    STATUS_BLOCKED,
    STATUS_DONE,
)

# Spalten, ohne die der Agent seinen Workflow nicht sauber abbilden kann:
# - running: der Agent arbeitet aktiv
# - review:  der Agent übergibt zur menschlichen Abnahme/Freigabe (Gate 2 erzwingt das)
# - blocked: der Agent meldet PLAN CHANGE REQUESTED / fehlende Voraussetzungen
REQUIRED_STATUSES: Tuple[str, ...] = (STATUS_RUNNING, STATUS_REVIEW, STATUS_BLOCKED)

# Regex zum Parsen von Markdown-Checkbox-Subtasks
_CHECKBOX_PATTERN = re.compile(
    r"^[ \t]*[-*]\s+\[(?P<state>[ xX/])\]\s+(?P<text>.+)$",
    re.MULTILINE,
)

# Default-Muster für destruktive Tool-Namen (Gate 3). Können via
# Board-/Plugin-Config (`destructive_tool_patterns`) überschrieben/erweitert werden.
# Achtung: Tool-Namen sind typischerweise snake_case — daher wird unten mit einer
# Unterstrich-bewussten Wortgrenze gematcht (delete_user, auth_reset_password, ...).
DEFAULT_DESTRUCTIVE_PATTERNS: Tuple[str, ...] = (
    "delete",
    "remove",
    "purge",
    "drop",
    "reset",
    "restart",
    "reboot",
    "kill",
    "terminate",
    "deactivate",
    "disable",
    "uninstall",
    "truncate",
    "rm",
    "destroy",
    "wipe",
    "flush",
)


def _word_pattern(token: str) -> str:
    """Baut eine Unterstrich-bewusste Wortgrenze um ein Token.

    `delete` matcht `delete_user`, `user_delete`, `auth_reset_password`, aber
    nicht `undelete` oder `nondestructive`.
    """
    return rf"(?<![a-zA-Z]){re.escape(token)}(?![a-zA-Z])"


def compile_destructive_patterns(extra: Optional[List[str]] = None) -> List[re.Pattern[str]]:
    """Kompiliert die destruktiven Tool-Muster (Default + optionale Erweiterung).

    Enthalten die Defaults keine Regex-Syntax, werden sie als Unterstrich-bewusste
    Wortgrenzen behandelt. Custom-Einträge (mit Regex-Syntax) werden wie angegeben
    übernommen.
    """
    patterns: List[str] = [_word_pattern(t) for t in DEFAULT_DESTRUCTIVE_PATTERNS]
    if extra:
        patterns.extend(str(p) for p in extra if str(p).strip())
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def is_destructive_tool(tool_name: str, patterns: List[re.Pattern[str]]) -> bool:
    """Erkennt destruktive Tools anhand ihres Namens."""
    name = str(tool_name or "").strip()
    if not name:
        return False
    return any(p.search(name) for p in patterns)


@dataclass(frozen=True)
class DeckWorkflowContext:
    """Aktiver Deck-Workflow-Kontext für einen Agent-Turn (Gate 3)."""

    board_id: str
    card_id: str
    phase: str
    risk: str
    approval: Optional[str] = None

    @property
    def high_risk(self) -> bool:
        return (self.risk or "").lower() in {"high", "critical"}


# ContextVar für den aktiven Deck-Karten-Kontext. Wird vom Adapter während der
# Verarbeitung einer Karte gesetzt und vom pre_tool_call-Hook gelesen, um Gate 3
# nur für die *aktuelle* Karte greifen zu lassen (kein globaler Tool-Block).
current_deck_context: contextvars.ContextVar[Optional[DeckWorkflowContext]] = contextvars.ContextVar(
    "deck_workflow_context", default=None
)

# ContextVar: zählt deck_card_action-Aufrufe im aktuellen Turn (Diagnose).
current_deck_action_count: contextvars.ContextVar[int] = contextvars.ContextVar(
    "deck_card_action_count", default=0
)


@dataclass(frozen=True)
class Subtask:
    text: str
    done: bool
    state_char: str


@dataclass(frozen=True)
class SubtaskProgress:
    total: int
    completed: int
    subtasks: List[Subtask] = field(default_factory=list)

    @property
    def percentage(self) -> int:
        if self.total == 0:
            return 0
        return int(round((self.completed / self.total) * 100))

    def summary(self) -> str:
        if self.total == 0:
            return "Keine Subtasks definiert"
        return f"{self.completed}/{self.total} Subtasks erledigt ({self.percentage}%)"


def parse_subtasks(description: str) -> SubtaskProgress:
    """Extrahiert alle Markdown-Checkboxen aus der Beschreibung."""
    if not description:
        return SubtaskProgress(total=0, completed=0, subtasks=[])

    items: List[Subtask] = []
    completed = 0
    for match in _CHECKBOX_PATTERN.finditer(description):
        state = match.group("state")
        text = match.group("text").strip()
        is_done = state.lower() == "x"
        if is_done:
            completed += 1
        items.append(Subtask(text=text, done=is_done, state_char=state))

    return SubtaskProgress(total=len(items), completed=completed, subtasks=items)


def extract_hermes_labels(card: Dict[str, Any]) -> Dict[str, str]:
    """Liest strukturierte hermes/* Labels aus einer Karte.

    Gibt ein Dict zurück mit Schlüsseln wie 'phase', 'type', 'risk', 'approval'.
    """
    labels = card.get("labels") or []
    result: Dict[str, str] = {}
    for label in labels:
        if not isinstance(label, dict):
            continue
        title = str(label.get("title") or "").strip().lower()
        if title.startswith(LABEL_PREFIX_PHASE):
            result["phase"] = title[len(LABEL_PREFIX_PHASE):]
        elif title.startswith(LABEL_PREFIX_TYPE):
            result["type"] = title[len(LABEL_PREFIX_TYPE):]
        elif title.startswith(LABEL_PREFIX_RISK):
            result["risk"] = title[len(LABEL_PREFIX_RISK):]
        elif title.startswith(LABEL_PREFIX_APPROVAL):
            result["approval"] = title[len(LABEL_PREFIX_APPROVAL):]
    return result


def is_backlog_stack(stack: Dict[str, Any], board_config: Optional[Dict[str, Any]] = None) -> bool:
    """Prüft, ob der übergebene Stack die Backlog-Spalte ist (Ideensammlung, nicht bearbeiten)."""
    stack_title = str(stack.get("title") or "").strip().lower()
    stack_id = str(stack.get("id") or "").strip()

    if board_config:
        mapping = board_config.get("status_mapping") or board_config.get("stack_mapping") or {}
        configured_backlog = mapping.get("backlog")
        if configured_backlog and (
            str(configured_backlog).strip() == stack_id
            or str(configured_backlog).strip().lower() == stack_title
        ):
            return True

    # Fallback-Namensmatching
    return stack_title in {"backlog", "ideen", "ideas", "icebox"}


def build_capabilities_prompt(
    phase: str,
    task_type: Optional[str] = None,
    risk: Optional[str] = None,
    approval: Optional[str] = None,
) -> str:
    """Baut den instruierenden Prompt-Block basierend auf Phase, Typ und Risiko."""
    norm_phase = (phase or PHASE_PLAN).lower()
    norm_risk = (risk or "low").lower()
    norm_type = (task_type or "general").lower()

    lines = [
        "### HERMES WORKFLOW AGENT CONTRACT",
        f"- Aktuelle Phase: {norm_phase.upper()}",
        f"- Task-Typ: {norm_type}",
        f"- Risiko: {norm_risk}",
        f"- Approval-Status: {approval or 'none'}",
        "",
        "🔧 **WICHTIG:** Beschreibung, Spalte, Labels und Assignee änderst du NUR",
        "über das Tool `deck_card_action`. Ein reiner Text-Kommentar ändert NICHTS an",
        "der Karte — sie bleibt in ihrer Spalte liegen und dein Plan geht verloren.",
        "Nutze das Tool in jedem Lauf, der die Karte strukturell weiterbewegt.",
        "",
    ]

    if norm_phase == PHASE_PLAN:
        lines.extend([
            "**MODUS: PLAN / KONZEPTION (READ-ONLY)**",
            "- Du befindest dich in der Planungsphase. Führe KEINE produktiven Änderungen an Systemen durch.",
            "- Sammle Informationen, untersuche Logs/Konfigurationen read-only und identifiziere Risiken.",
            "- Aktualisiere die Karten-Beschreibung mit strukturiertem Plan:",
            "  * Objective & Context",
            "  * Acceptance Criteria (- [ ] ...)",
            "  * Plan & Subtasks (- [ ] 1. Schritt ...)",
            "  * Verification",
            "- Wenn der Plan fertig ist:",
            "  * Verschiebe die Karte nach 'review'",
            "  * Setze Label 'hermes/approval:required' (oder fordere Freigabe per Kommentar)",
            "  * Wechsle NICHT selbst nach 'execute' — warte auf die menschliche Freigabe!",
        ])
    else:  # EXECUTE
        lines.extend([
            "**MODUS: EXECUTE / UMSETZUNG**",
            "- Der Plan wurde freigegeben. Setze die Subtasks aus der Beschreibung sequenziell um.",
            "- Hake erledigte Subtasks in der Beschreibung mit [x] ab.",
            "- Dokumentiere bei wichtigen/riskanten Schritten konkrete Nachweise (Evidence: ...).",
            "- **WICHTIG (Anti-Halluzination/Sicherheit):**",
            "  * Erfinde den Plan bei Problemen NICHT eigenmächtig neu!",
            "  * Wenn ein Teilschritt fehlschlägt oder der Plan nicht passt: Setze Status auf 'blocked'",
            "    und poste einen Kommentar '🤖 PLAN CHANGE REQUESTED' mit Begründung.",
            "- Nach erfolgreicher Abarbeitung aller Subtasks und Verifikation:",
            "  * Verschiebe nach 'review' (NICHT nach 'done' — der Mensch nimmt ab!)",
            "  * Dokumentiere das Gesamtergebnis im Result-Bereich der Beschreibung.",
            "  * Setze 'hermes/approval:required' für die Abnahme.",
        ])

    if norm_risk in {"high", "critical"}:
        lines.extend([
            "",
            "⚠️ **HIGH RISK:** Destruktive Aktionen (Löschen, Service-Neustarts, Auth-Flow-Änderungen)",
            "erfordern vor der Ausführung eine explizite Bestätigung im Kommentar!",
        ])

    return "\n".join(lines)


def gate1_is_mandatory(task_type: Optional[str], risk: Optional[str]) -> bool:
    """Legt fest, ob Gate 1 (Plan -> Execute) für diese Karte verpflichtend ist.

    Progressive Autonomy (§9): Nur `implementation`-Typen oder risk >= medium
    erzwingen die menschliche Freigabe. Dokumentation, Troubleshooting und
    Recherche mit risk:low dürfen eigenständig in die Umsetzung.
    """
    norm_type = (task_type or "").strip().lower()
    norm_risk = (risk or "low").strip().lower()
    if norm_type in GATE1_MANDATORY_TYPES:
        return True
    if norm_risk in GATE1_MANDATORY_RISKS:
        return True
    return False


def check_agent_status_gate(
    target_status: str,
    current_phase: str,
    approval_status: Optional[str] = None,
    task_type: Optional[str] = None,
    risk: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Prüft, ob ein vom Agenten angeforderter Statuswechsel durch ein Gate erlaubt ist.

    Rückgabe: (allowed, rejection_reason)
    """
    status_norm = (target_status or "").strip().lower()

    # Gate 2: DONE darf nie direkt vom Agenten gesetzt werden
    if status_norm in {STATUS_DONE, "erledigt", "closed", "finish", "finished"}:
        return False, (
            "Gate 2 verletzt: Der Agent darf Karten nicht selbst als 'done' markieren. "
            "Verschiebe stattdessen nach 'review' und fordere die menschliche Abnahme an."
        )

    # Backlog ist Start-Ideenspeicher — Agent schiebt nicht dorthin
    if status_norm in {STATUS_BACKLOG, "ideen"}:
        return False, "Karten können vom Agenten nicht ins Backlog verschoben werden."

    # Gate 1: Aus der Plan-Phase direkt nach Running/Execute ohne Approval —
    # nur, wenn Gate 1 für diesen Typ/diese Risikostufe verpflichtend ist.
    if (
        current_phase == PHASE_PLAN
        and status_norm in {STATUS_RUNNING, STATUS_READY, "in_progress", "umsetzung", "in arbeit"}
        and approval_status != APPROVAL_APPROVED
        and gate1_is_mandatory(task_type, risk)
    ):
        return False, (
            "Gate 1 verletzt: Die Karte befindet sich in der Phase 'plan' und ist noch nicht "
            "freigegeben (hermes/approval:approved fehlt). Verschiebe nach 'review' für Approval."
        )

    return True, None


def check_agent_label_gate(
    label_to_assign: str,
    current_phase: str,
    approval_status: Optional[str] = None,
    task_type: Optional[str] = None,
    risk: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Prüft, ob der Agent ein bestimmtes Label selbst setzen darf."""
    norm = label_to_assign.strip().lower()

    # Agent darf sich nicht selbst Freigabe erteilen
    if norm == f"{LABEL_PREFIX_APPROVAL}{APPROVAL_APPROVED}":
        return False, "Gate 1 verletzt: Der Agent darf 'hermes/approval:approved' nicht selbst setzen."

    # Agent darf nicht eigenmächtig von plan nach execute wechseln, wenn nicht approved
    if (
        norm == f"{LABEL_PREFIX_PHASE}{PHASE_EXECUTE}"
        and current_phase == PHASE_PLAN
        and approval_status != APPROVAL_APPROVED
        and gate1_is_mandatory(task_type, risk)
    ):
        return False, (
            "Gate 1 verletzt: Phasenwechsel zu 'execute' erfordert vorherige menschliche Freigabe "
            "(Label 'hermes/approval:approved')."
        )

    return True, None


class DestructiveToolBlocked(Exception):
    """Wird vom pre_tool_call-Hook geworfen, wenn ein destruktives Tool bei risk:high
    ohne Freigabe aufgerufen wird (Gate 3)."""


def check_destructive_gate(
    tool_name: str,
    workflow_ctx: Optional[DeckWorkflowContext],
    patterns: List[re.Pattern[str]],
) -> Tuple[bool, Optional[str]]:
    """Prüft Gate 3: destruktives Tool bei risk:high ohne Approval -> blocken.

    Rückgabe: (allowed, rejection_reason)
    """
    if workflow_ctx is None:
        return True, None
    if not workflow_ctx.high_risk:
        return True, None
    if not is_destructive_tool(tool_name, patterns):
        return True, None

    return False, (
        f"Gate 3 verletzt: Das Tool '{tool_name}' ist destruktiv und die Karte "
        f"({workflow_ctx.board_id}/{workflow_ctx.card_id}) trägt risk:high ohne "
        f"explizite Freigabe. Destruktive Aktionen erfordern vorab eine menschliche "
        f"Bestätigung im Karten-Kommentar."
    )


@dataclass(frozen=True)
class BoardSuitabilityResult:
    """Ergebnis der Board-Eignungsprüfung für ein konfiguriertes Board."""

    board_id: str
    board_title: str
    resolvable: Dict[str, Optional[str]] = field(default_factory=dict)
    missing_required: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def is_suitable(self) -> bool:
        return not self.missing_required

    def format_report(self) -> str:
        lines = [f"Board {self.board_id} ({self.board_title}):"]
        for status in CANONICAL_STATUSES:
            target = self.resolvable.get(status)
            mark = "✓" if target else "✗"
            suffix = f" -> Stack-ID {target}" if target else ""
            lines.append(f"  [{mark}] {status}{suffix}")
        if self.missing_required:
            lines.append(f"  ⚠️  Fehlende Pflicht-Spalten: {', '.join(self.missing_required)}")
        for w in self.warnings:
            lines.append(f"  ⓘ  {w}")
        return "\n".join(lines)


def resolve_stack_id_for_status(
    status_key: str,
    stacks: List[Dict[str, Any]],
    board_config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Löst einen kanonischen Status-Schlüssel zu einer Stack-ID auf.

    Priorität: 1) `stack_mapping`/`status_mapping` der Board-Config (nach ID
    ODER Titel), 2) exakter Titel-Match (casefold) auf den Spalten-Titel.
    """
    key = str(status_key or "").strip().lower()

    if board_config:
        mapping = board_config.get("status_mapping") or board_config.get("stack_mapping") or {}
        if isinstance(mapping, dict):
            target = mapping.get(key) or mapping.get(status_key)
            if target:
                target_s = str(target).strip()
                # Mapping kann Stack-ID oder Titel sein -> gegen beides prüfen
                for stack in stacks:
                    sid = str(stack.get("id") or "").strip()
                    title = str(stack.get("title") or "").strip()
                    if target_s == sid or target_s.lower() == title.lower():
                        return sid
                # Mapping-Ziel existiert nicht im Board -> None (wird als fehlend gewertet)

    # Fallback: exakter Titel-Match
    for stack in stacks:
        title = str(stack.get("title") or "").strip().casefold()
        if title == key:
            sid = str(stack.get("id") or "").strip()
            return sid or None
    return None


def analyze_board_suitability(
    board_id: str,
    board_title: str,
    stacks: List[Dict[str, Any]],
    board_config: Optional[Dict[str, Any]] = None,
) -> BoardSuitabilityResult:
    """Prüft, ob ein Board für den Hermes-Deck-Workflow geeignet ist.

    Ergebnis: pro kanonischer Spalte eine Stack-ID (oder None), plus Liste der
    fehlenden Pflicht-Spalten und Warnungen.
    """
    resolvable: Dict[str, Optional[str]] = {}
    for status in CANONICAL_STATUSES:
        resolvable[status] = resolve_stack_id_for_status(status, stacks, board_config)

    missing_required = [
        status for status in REQUIRED_STATUSES if not resolvable.get(status)
    ]

    warnings: List[str] = []
    # Warnung für optionale Spalten, die nicht auflösbar sind (kein harter Fehler)
    for status in CANONICAL_STATUSES:
        if status not in REQUIRED_STATUSES and not resolvable.get(status):
            warnings.append(f"Optionale Spalte '{status}' nicht gefunden")

    if not stacks:
        warnings.append("Board hat keine Spalten (Stacks)")

    return BoardSuitabilityResult(
        board_id=board_id,
        board_title=board_title,
        resolvable=resolvable,
        missing_required=missing_required,
        warnings=warnings,
    )
