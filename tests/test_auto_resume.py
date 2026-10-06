"""Auto-Resume (R1–R7): idle-Warteschleifen verhindern.

Abgedeckt:
  - Config-Defaults & Overrides (auto_resume, max_auto_resumes_per_card).
  - State-Budget: Cap, Fingerprint-Reset (neuer Zyklus), Persistenz.
  - Kandidaten-Erkennung (Priorität 1 active_work, Priorität 2 review_approved,
    Ausnahmen: Backlog/Blocked/Done, veraltete Freigabe, laufende Turns).
  - Pass-Verhalten: nur wenn idle, WIP-Batch, Cap-Erschöpfung, Fehler-Handling.
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

# Clear any cached gateway imports to ensure adapter uses its fallback classes
for _mod in (
    "gateway",
    "gateway.config",
    "gateway.platforms",
    "gateway.platforms.base",
):
    if _mod in sys.modules:
        del sys.modules[_mod]

from adapter import NextcloudDeckPlatform  # noqa: E402
from ingestion import (  # noqa: E402
    KIND_ACTIVE_WORK,
    KIND_REVIEW_APPROVED,
    auto_resume_pass,
    find_resume_candidates,
)
from state import DeckStateManager  # noqa: E402


def _make_adapter(extra: dict[str, Any] | None = None) -> NextcloudDeckPlatform:
    """Adapter im Minimal-Setup (kein Netzwerk, Board 1 konfiguriert)."""
    config = SimpleNamespace(
        extra={
            "base_url": "https://cloud.example.org",
            "username": "hermes",
            "app_password": "secret",
            "hermes_user_id": "hermes",
            "boards": [{"board_id": "1"}],
            **(extra or {}),
        }
    )
    adapter = NextcloudDeckPlatform(config)
    # Test-Isolation: FRESCHER In-Memory-State — der Adapter lädt sonst die
    # shared ~/.hermes/nextcloud-deck/state.json, und Budget-Einträge aus
    # früheren Testläufen würden die Auto-Resume-Budgets verunreinigen.
    adapter.state = DeckStateManager()
    # Identity: deterministische assigned-uids (kein Netzwerk/On-Behalf).
    adapter.identity = SimpleNamespace(
        assigned_uids=lambda card: [
            str(u.get("uid")) for u in (card.get("assignedUsers") or []) if u.get("uid")
        ]
    )
    return adapter


def _card(
    card_id: str,
    assign_hermes: bool = True,
    approved: bool = False,
    description: str = "Beschreibung",
) -> dict[str, Any]:
    labels: list[dict[str, str]] = []
    if approved:
        labels.append({"title": "hermes/approval:approved"})
    return {
        "id": card_id,
        "title": f"Karte {card_id}",
        "description": description,
        "assignedUsers": [{"uid": "hermes"}] if assign_hermes else [],
        "labels": labels,
    }


def _stack(stack_id: str, title: str, cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"id": stack_id, "title": title, "cards": cards or []}


def _wire_client(
    adapter: NextcloudDeckPlatform,
    stacks_by_board: dict[str, list[dict[str, Any]]],
    comments: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    adapter.client = SimpleNamespace(
        get_boards=AsyncMock(return_value=[{"id": "1"}]),
        get_stacks=AsyncMock(
            side_effect=lambda board_id: stacks_by_board.get(str(board_id), [])
        ),
        get_card_comments=AsyncMock(
            side_effect=lambda card_id: (comments or {}).get(str(card_id), [])
        ),
    )


class TestAutoResumeConfig(unittest.TestCase):
    def test_defaults(self):
        adapter = _make_adapter()
        self.assertTrue(adapter.runtime.auto_resume)
        self.assertEqual(adapter.runtime.max_auto_resumes_per_card, 3)

    def test_extra_false_wins_over_default_true(self):
        # Bewusstes ``auto_resume: false`` in der Config muss das Default
        # überstimmen (falsy-Value darf NICHT als "nicht gesetzt" gelten).
        adapter = _make_adapter({"auto_resume": False})
        self.assertFalse(adapter.runtime.auto_resume)

    def test_extra_zero_cap(self):
        adapter = _make_adapter({"max_auto_resumes_per_card": 0})
        self.assertEqual(adapter.runtime.max_auto_resumes_per_card, 0)

    def test_invalid_cap_falls_back_to_default(self):
        adapter = _make_adapter({"max_auto_resumes_per_card": "bogus"})
        self.assertEqual(adapter.runtime.max_auto_resumes_per_card, 3)

    def test_env_false_disables(self):
        old = os.environ.get("NEXTCLOUD_DECK_AUTO_RESUME")
        os.environ["NEXTCLOUD_DECK_AUTO_RESUME"] = "false"
        try:
            adapter = _make_adapter()
            self.assertFalse(adapter.runtime.auto_resume)
        finally:
            if old is None:
                os.environ.pop("NEXTCLOUD_DECK_AUTO_RESUME", None)
            else:
                os.environ["NEXTCLOUD_DECK_AUTO_RESUME"] = old


class TestAutoResumeStateBudget(unittest.TestCase):
    def test_fresh_budget_equals_cap(self):
        mgr = DeckStateManager()
        self.assertEqual(mgr.auto_resume_budget_left("1", "100", 3, "fp-A"), 3)

    def test_budget_decreases_with_attempts(self):
        mgr = DeckStateManager()
        for _ in range(3):
            mgr.record_auto_resume("1", "100", "fp-A")
        self.assertEqual(mgr.auto_resume_budget_left("1", "100", 3, "fp-A"), 0)

    def test_fingerprint_change_resets_budget(self):
        # Neuer Zyklus (neue Freigabe/Edit/Move) = frisches Budget.
        mgr = DeckStateManager()
        for _ in range(3):
            mgr.record_auto_resume("1", "100", "fp-A")
        self.assertEqual(mgr.auto_resume_budget_left("1", "100", 3, "fp-A"), 0)
        self.assertEqual(mgr.auto_resume_budget_left("1", "100", 3, "fp-B"), 3)

    def test_zero_cap_disables(self):
        mgr = DeckStateManager()
        self.assertEqual(mgr.auto_resume_budget_left("1", "100", 0, "fp-A"), 0)

    def test_budget_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "state.json")
            mgr = DeckStateManager(state_file=state_file)
            mgr.record_auto_resume("1", "100", "fp-A")
            mgr.record_auto_resume("1", "100", "fp-A")
            reloaded = DeckStateManager(state_file=state_file)
            self.assertEqual(reloaded.auto_resume_budget_left("1", "100", 3, "fp-A"), 1)


class TestFindResumeCandidates(unittest.IsolatedAsyncioTestCase):
    async def test_active_work_and_review_priority(self):
        adapter = _make_adapter()
        stacks = [
            _stack("10", "Running", [_card("100")]),  # Prio 1
            _stack("20", "Review", [_card("110", approved=True)]),  # Prio 2
        ]
        found = find_resume_candidates(adapter, [{"id": "1"}], {"1": stacks})
        self.assertEqual(
            [(c[3], str(c[2]["id"])) for c in found],
            [(KIND_ACTIVE_WORK, "100"), (KIND_REVIEW_APPROVED, "110")],
        )

    async def test_unassigned_card_is_no_candidate(self):
        adapter = _make_adapter()
        stacks = [_stack("10", "Running", [_card("100", assign_hermes=False)])]
        found = find_resume_candidates(adapter, [{"id": "1"}], {"1": stacks})
        self.assertEqual(found, [])

    async def test_stale_approval_is_no_candidate(self):
        # Freigabe bereits in der Baseline (letzter Lauf) → veraltet.
        adapter = _make_adapter()
        adapter.state._last_labels["1:110"] = ["hermes/approval:approved"]
        stacks = [_stack("20", "Review", [_card("110", approved=True)])]
        found = find_resume_candidates(adapter, [{"id": "1"}], {"1": stacks})
        self.assertEqual(found, [])

    async def test_blocked_and_done_are_no_candidates(self):
        adapter = _make_adapter()
        stacks = [
            _stack("30", "Blocked", [_card("120", approved=True)]),
            _stack("40", "Done", [_card("130", approved=True)]),
        ]
        found = find_resume_candidates(adapter, [{"id": "1"}], {"1": stacks})
        self.assertEqual(found, [])

    async def test_backlog_is_no_candidate(self):
        adapter = _make_adapter()
        stacks = [_stack("50", "Backlog", [_card("140")])]
        found = find_resume_candidates(adapter, [{"id": "1"}], {"1": stacks})
        self.assertEqual(found, [])

    async def test_running_turns_are_excluded(self):
        adapter = _make_adapter()
        adapter._active_turns.add("100")
        stacks = [_stack("10", "Running", [_card("100")])]
        found = find_resume_candidates(adapter, [{"id": "1"}], {"1": stacks})
        self.assertEqual(found, [])


class TestAutoResumePass(unittest.IsolatedAsyncioTestCase):
    async def test_starts_active_work_card_with_force_resume(self):
        adapter = _make_adapter()
        stacks = [_stack("10", "Running", [_card("100")])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()
        started = await auto_resume_pass(adapter)
        self.assertEqual(started, 1)
        adapter._process_card.assert_awaited_once()
        _, kwargs = adapter._process_card.call_args
        self.assertTrue(kwargs.get("force_resume"))

    async def test_no_candidates_no_start(self):
        adapter = _make_adapter()
        _wire_client(adapter, {"1": []})
        adapter._process_card = AsyncMock()
        self.assertEqual(await auto_resume_pass(adapter), 0)
        adapter._process_card.assert_not_awaited()

    async def test_respects_wip_limit(self):
        # max_in_progress=2 → max. 2 der 3 Kandidaten pro Pass.
        adapter = _make_adapter({"max_in_progress": 2})
        stacks = [
            _stack("10", "Running", [_card("100"), _card("101"), _card("102")]),
        ]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()
        self.assertEqual(await auto_resume_pass(adapter), 2)

    async def test_unlimited_wip_starts_one_per_pass(self):
        # max_in_progress=0 (unbegrenzt) → Ollama-Schutz: 1 pro Pass.
        adapter = _make_adapter()
        stacks = [_stack("10", "Running", [_card("100"), _card("101"), _card("102")])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()
        self.assertEqual(await auto_resume_pass(adapter), 1)

    async def test_skips_when_turn_already_running(self):
        adapter = _make_adapter()
        adapter._active_turns.add("999")  # ein anderer Turn läuft
        stacks = [_stack("10", "Running", [_card("100")])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()
        self.assertEqual(await auto_resume_pass(adapter), 0)
        adapter._process_card.assert_not_awaited()

    async def test_disabled_feature_starts_nothing(self):
        adapter = _make_adapter({"auto_resume": False})
        stacks = [_stack("10", "Running", [_card("100")])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()
        self.assertEqual(await auto_resume_pass(adapter), 0)
        adapter._process_card.assert_not_awaited()

    async def test_budget_exhaustion_blocks_further_passes(self):
        # Cap=1: erster Pass startet, zweiter Pass ist budget-erschöpft.
        adapter = _make_adapter({"max_auto_resumes_per_card": 1})
        stacks = [_stack("10", "Running", [_card("100")])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()
        self.assertEqual(await auto_resume_pass(adapter), 1)
        self.assertEqual(await auto_resume_pass(adapter), 0)
        self.assertEqual(adapter._process_card.await_count, 1)

    async def test_new_fingerprint_resets_budget(self):
        # Beschreibung geändert = neuer Zyklus = frisches Budget.
        adapter = _make_adapter({"max_auto_resumes_per_card": 1})
        stacks = [_stack("10", "Running", [_card("100")])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()
        self.assertEqual(await auto_resume_pass(adapter), 1)
        # Zyklus wechseln (Karte wurde bearbeitet/verändert):
        stacks = [_stack("10", "Running", [_card("100", description="Neuer Stand")])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()
        self.assertEqual(await auto_resume_pass(adapter), 1)

    async def test_failed_start_is_caught_and_counts_budget(self):
        adapter = _make_adapter({"max_auto_resumes_per_card": 1})
        stacks = [_stack("10", "Running", [_card("100")])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock(side_effect=RuntimeError("API down"))
        # Fehlgeschlagener Start wird abgefangen (Polling-Loop bleibt lebendig)
        # und zählt ins Budget (Loop-Schutz).
        self.assertEqual(await auto_resume_pass(adapter), 0)
        self.assertEqual(await auto_resume_pass(adapter), 0)
        self.assertEqual(adapter._process_card.await_count, 1)


if __name__ == "__main__":
    unittest.main()
