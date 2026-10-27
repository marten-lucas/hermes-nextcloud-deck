"""Gateway restart-resume and Deck review dispatch must stay separate.

Gateway-owned ``resume_pending`` sessions are resumed by Hermes with their
original transcript. The Deck polling pass only dispatches newly approved
Review cards; normal event deduplication prevents self-trigger loops.
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
    KIND_REVIEW_APPROVED,
    find_review_approval_candidates,
    review_approval_pass,
)
from platform_wip import finish_platform_turn, start_platform_turn  # noqa: E402
from state import DeckCardSnapshot, DeckStateManager  # noqa: E402


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
    adapter: Any = NextcloudDeckPlatform(config)
    adapter.state = DeckStateManager()
    adapter.identity = SimpleNamespace(
        assigned_uids=lambda card: [
            str(user.get("uid"))
            for user in (card.get("assignedUsers") or [])
            if user.get("uid")
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
    adapter: Any,
    stacks_by_board: dict[str, list[dict[str, Any]]],
) -> None:
    adapter.client = SimpleNamespace(
        get_boards=AsyncMock(return_value=[{"id": "1"}]),
        get_stacks=AsyncMock(
            side_effect=lambda board_id: stacks_by_board.get(str(board_id), [])
        ),
    )


class TestReviewDispatchConfig(unittest.TestCase):
    def test_defaults_enabled(self):
        adapter = _make_adapter()
        self.assertTrue(adapter.runtime.auto_start_approved_reviews)

    def test_explicit_false_disables_review_dispatch(self):
        adapter = _make_adapter({"auto_start_approved_reviews": False})
        self.assertFalse(adapter.runtime.auto_start_approved_reviews)

    def test_legacy_setting_remains_a_review_dispatch_alias(self):
        adapter = _make_adapter({"auto_resume": False})
        self.assertFalse(adapter.runtime.auto_start_approved_reviews)

    def test_env_setting_disables_review_dispatch(self):
        old = os.environ.get("NEXTCLOUD_DECK_AUTO_START_APPROVED_REVIEWS")
        os.environ["NEXTCLOUD_DECK_AUTO_START_APPROVED_REVIEWS"] = "false"
        try:
            adapter = _make_adapter()
            self.assertFalse(adapter.runtime.auto_start_approved_reviews)
        finally:
            if old is None:
                os.environ.pop("NEXTCLOUD_DECK_AUTO_START_APPROVED_REVIEWS", None)
            else:
                os.environ["NEXTCLOUD_DECK_AUTO_START_APPROVED_REVIEWS"] = old


class TestDedupLoopProtection(unittest.TestCase):
    def test_processed_agent_revision_does_not_retrigger_but_human_change_does(self):
        state = DeckStateManager()
        agent_revision = DeckCardSnapshot(
            board_id="1",
            stack_id="10",
            card_id="100",
            title="Test",
            description="Plan",
            labels=["hermes/phase:plan"],
            last_comment_id="42",
            last_author="hermes",
            last_comment_message="Plan aktualisiert",
        )
        state.mark_processed(agent_revision)
        self.assertFalse(state.should_process(agent_revision))

        human_revision = DeckCardSnapshot(
            board_id="1",
            stack_id="10",
            card_id="100",
            title="Test",
            description="Plan",
            labels=["hermes/phase:plan", "hermes/approval:approved"],
            last_comment_id="43",
            last_author="marten",
            last_comment_message="Freigegeben",
        )
        self.assertTrue(state.should_process(human_revision))

    def test_processed_baseline_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "state.json")
            state = DeckStateManager(state_file=state_file)
            revision = DeckCardSnapshot(
                board_id="1", stack_id="10", card_id="100",
                title="Test", description="Plan", labels=["hermes/phase:plan"],
            )
            state.mark_processed(revision)
            reloaded = DeckStateManager(state_file=state_file)
            self.assertFalse(reloaded.should_process(revision))


class TestReviewApprovalCandidates(unittest.TestCase):
    def test_only_new_review_approval_is_a_candidate(self):
        adapter = _make_adapter()
        stacks = [
            _stack("10", "Running", [_card("100")]),
            _stack("20", "Review", [_card("110", approved=True)]),
        ]
        candidates = find_review_approval_candidates(
            adapter, [{"id": "1"}], {"1": stacks}
        )
        self.assertEqual(
            [(candidate[3], str(candidate[2]["id"])) for candidate in candidates],
            [(KIND_REVIEW_APPROVED, "110")],
        )

    def test_already_consumed_approval_is_not_a_candidate(self):
        adapter = _make_adapter()
        adapter.state._last_labels["1:110"] = ["hermes/approval:approved"]
        stacks = [_stack("20", "Review", [_card("110", approved=True)])]
        self.assertEqual(
            find_review_approval_candidates(adapter, [{"id": "1"}], {"1": stacks}),
            [],
        )

    def test_blocked_done_and_backlog_are_not_candidates(self):
        adapter = _make_adapter()
        stacks = [
            _stack("20", "Blocked", [_card("120", approved=True)]),
            _stack("30", "Done", [_card("130", approved=True)]),
            _stack("40", "Backlog", [_card("140", approved=True)]),
        ]
        self.assertEqual(
            find_review_approval_candidates(adapter, [{"id": "1"}], {"1": stacks}),
            [],
        )


class TestReviewApprovalPass(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_new_approval_bypassing_normal_trigger_filters(self):
        adapter = _make_adapter()
        stacks = [_stack("20", "Review", [_card("110", approved=True)])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()

        self.assertEqual(await review_approval_pass(adapter), 1)
        adapter._process_card.assert_awaited_once()
        _, kwargs = adapter._process_card.call_args
        self.assertTrue(kwargs.get("force_process"))

    async def test_never_restarts_arbitrary_assigned_active_work(self):
        adapter = _make_adapter()
        stacks = [_stack("10", "Running", [_card("100")])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()

        self.assertEqual(await review_approval_pass(adapter), 0)
        adapter._process_card.assert_not_awaited()

    async def test_no_candidates_no_start(self):
        adapter = _make_adapter()
        _wire_client(adapter, {"1": []})
        adapter._process_card = AsyncMock()

        self.assertEqual(await review_approval_pass(adapter), 0)
        adapter._process_card.assert_not_awaited()

    async def test_respects_wip_limit(self):
        adapter = _make_adapter({"max_in_progress": 1})
        stacks = [
            _stack("20", "Review", [
                _card("110", approved=True),
                _card("111", approved=True),
            ])
        ]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()

        self.assertEqual(await review_approval_pass(adapter), 1)

    async def test_unlimited_wip_starts_one_per_pass(self):
        adapter = _make_adapter()
        stacks = [_stack("20", "Review", [
            _card("110", approved=True),
            _card("111", approved=True),
        ])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()

        self.assertEqual(await review_approval_pass(adapter), 1)

    async def test_skips_while_talk_turn_is_running(self):
        adapter = _make_adapter({"max_in_progress": 1})
        stacks = [_stack("20", "Review", [_card("110", approved=True)])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()
        start_platform_turn("talk", "room-review-approval-test")
        try:
            self.assertEqual(await review_approval_pass(adapter), 0)
            adapter._process_card.assert_not_awaited()
        finally:
            finish_platform_turn("talk", "room-review-approval-test")

    async def test_disabled_review_dispatch_starts_nothing(self):
        adapter = _make_adapter({"auto_start_approved_reviews": False})
        stacks = [_stack("20", "Review", [_card("110", approved=True)])]
        _wire_client(adapter, {"1": stacks})
        adapter._process_card = AsyncMock()

        self.assertEqual(await review_approval_pass(adapter), 0)
        adapter._process_card.assert_not_awaited()

    async def test_successful_processing_consumes_approval_without_counter(self):
        adapter = _make_adapter()
        card = _card("110", approved=True)
        stacks = [_stack("20", "Review", [card])]
        _wire_client(adapter, {"1": stacks})

        async def process_and_rebaseline(board, stack, processed_card, **kwargs):
            del board, kwargs
            adapter.state.mark_processed(DeckCardSnapshot(
                board_id="1",
                stack_id=str(stack["id"]),
                card_id=str(processed_card["id"]),
                title=processed_card["title"],
                description=processed_card["description"],
                assigned_users=["hermes"],
                labels=["hermes/approval:approved"],
            ))

        adapter._process_card = AsyncMock(side_effect=process_and_rebaseline)
        self.assertEqual(await review_approval_pass(adapter), 1)
        self.assertEqual(await review_approval_pass(adapter), 0)
        self.assertEqual(adapter._process_card.await_count, 1)


if __name__ == "__main__":
    unittest.main()
