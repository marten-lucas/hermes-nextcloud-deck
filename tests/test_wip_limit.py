import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
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
from platform_wip import (
    acquire_platform_turn,
    active_platform_turns,
    active_presence_status,
    active_presence_turns,
    configure_platform_turn_limit,
    finish_platform_turn,
    presence_scope,
    register_presence_turn,
    set_presence_turn_status,
    start_platform_turn,
    try_acquire_platform_turn,
    unregister_presence_turn,
)  # noqa: E402


def _make_adapter() -> NextcloudDeckPlatform:
    """Adapter im Minimal-Setup (kein Netzwerk, keine Board-Konfiguration)."""
    config = SimpleNamespace(
        extra={
            "base_url": "https://cloud.example.org",
            "username": "hermes",
            "app_password": "secret",
            "hermes_user_id": "hermes",
        }
    )
    return NextcloudDeckPlatform(config)


class TestActiveTurnLifecycle(unittest.IsolatedAsyncioTestCase):
    """FUNC-002: Der in-flight-Turn wird UNABHÄNGIG vom Speed-Feature
    registriert (add vor dem Run) und beim Verlassen entfernt (discard) —
    auch wenn ein Run fehlschlägt (kein WIP-Leak)."""

    async def test_registers_card_even_when_speed_disabled(self):
        adapter = _make_adapter()
        adapter.runtime.speed_enabled = False  # Default-Konfiguration
        adapter._active_turns.clear()
        async with adapter._active_turn("cardA"):
            self.assertIn("cardA", adapter._active_turns)
        # Nach dem Block ist die Karte nicht mehr in-flight (kein Leak).
        self.assertNotIn("cardA", adapter._active_turns)

    async def test_discards_card_when_run_raises(self):
        adapter = _make_adapter()
        adapter.runtime.speed_enabled = False
        adapter._active_turns.clear()
        with self.assertRaises(RuntimeError):
            async with adapter._active_turn("cardA"):
                raise RuntimeError("agent crashed")
        # Trotz Exception wurde der Discard ausgeführt (finally-Garantie).
        self.assertNotIn("cardA", adapter._active_turns)

    async def test_reentry_is_idempotent(self):
        # Set-Add ist idempotent: doppelte Registrierung derselben Karte
        # (z. B. send_typing UND _process_card) darf nicht zweimal zählen.
        adapter = _make_adapter()
        adapter._active_turns.clear()
        async with adapter._active_turn("cardA"):
            adapter._active_turns.add("cardA")
            self.assertEqual(len(adapter._active_turns), 1)
        self.assertEqual(len(adapter._active_turns), 0)


class TestGatewayProcessingHooks(unittest.IsolatedAsyncioTestCase):
    async def test_talk_plugin_copy_obeys_deck_shared_turn_admission(self):
        talk_path = Path(__file__).resolve().parents[2] / "nextcloudtalk-hermes-channel" / "platform_wip.py"
        if not talk_path.is_file():
            self.skipTest("sibling Nextcloud Talk plugin is not checked out")
        spec = importlib.util.spec_from_file_location("talk_platform_wip_contract", talk_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        account = presence_scope("https://cloud.example.org", "hermes")
        configure_platform_turn_limit(1)
        talk_token = "talk-contract-turn"
        deck_token = "deck-contract-turn"
        try:
            await acquire_platform_turn("deck", deck_token)
            self.assertFalse(module.try_acquire_platform_turn("talk", talk_token))
            finish_platform_turn("deck", deck_token)
            await module.acquire_platform_turn("talk", talk_token)
            self.assertEqual(active_platform_turns(), 1)

            register_presence_turn(account, "deck", deck_token)
            set_presence_turn_status(account, "deck", deck_token, "Deck arbeitet", "🎴")
            module.register_presence_turn(account, "talk", talk_token)
            module.set_presence_turn_status(account, "talk", talk_token, "Talk antwortet", "💬")
            self.assertEqual(active_presence_turns(account), 2)
            self.assertEqual(active_presence_status(account), ("Talk antwortet", "💬"))
            module.unregister_presence_turn(account, "talk", talk_token)
            self.assertEqual(active_presence_status(account), ("Deck arbeitet", "🎴"))
            unregister_presence_turn(account, "deck", deck_token)
            self.assertEqual(active_presence_turns(account), 0)
        finally:
            finish_platform_turn("deck", deck_token)
            module.finish_platform_turn("talk", talk_token)
            unregister_presence_turn(account, "deck", deck_token)
            module.unregister_presence_turn(account, "talk", talk_token)
            configure_platform_turn_limit(0)

    async def test_wip_reservation_lives_until_processing_completion(self):
        adapter = _make_adapter()
        configure_platform_turn_limit(1)
        adapter.presence_mgr.set_busy = AsyncMock()
        adapter.presence_mgr.clear_busy = AsyncMock()
        adapter.presence_mgr.set_presence_status = AsyncMock()
        adapter.presence_mgr.clear_custom_status_message = AsyncMock()
        adapter._rebaseline_card = AsyncMock()
        event = SimpleNamespace(
            source=SimpleNamespace(chat_id="deck:board:1:card:100"),
            raw_message={"_hermes_deck_turn": {
                "board_id": "1", "stack_id": "10", "card_id": "100",
                "phase": "execute", "risk": "low", "prompt_comment_id": 5,
                "dispatch_fingerprint": "revision-a",
            }},
        )

        await adapter.on_processing_start(event)
        token = adapter._deck_event_tokens[id(event)]
        self.assertIn("100", adapter._active_turns)
        self.assertEqual(active_platform_turns(), 1)
        self.assertFalse(try_acquire_platform_turn("talk", "room-while-deck-runs"))

        await adapter.on_processing_complete(event, "success")
        self.assertNotIn("100", adapter._active_turns)
        self.assertEqual(active_platform_turns(), 0)
        adapter._rebaseline_card.assert_awaited_once()
        self.assertNotIn(token, adapter._deck_turns)

    async def test_processing_start_waits_for_talk_slot_and_releases_after_completion(self):
        adapter = _make_adapter()
        configure_platform_turn_limit(1)
        adapter.presence_mgr.set_busy = AsyncMock()
        adapter.presence_mgr.clear_busy = AsyncMock()
        adapter.presence_mgr.set_presence_status = AsyncMock()
        adapter.presence_mgr.clear_custom_status_message = AsyncMock()
        adapter._rebaseline_card = AsyncMock()
        adapter._apply_label_to_card = AsyncMock(return_value=(True, None))
        adapter._remove_label_from_card = AsyncMock(return_value=True)
        start_platform_turn("talk", "room-blocker")
        event = SimpleNamespace(
            source=SimpleNamespace(chat_id="deck:board:1:card:101"),
            raw_message={"_hermes_deck_turn": {
                "board_id": "1", "stack_id": "10", "card_id": "101",
                "phase": "plan", "risk": "low", "prompt_comment_id": None,
                "dispatch_fingerprint": "revision-b",
            }},
        )
        task = asyncio.create_task(adapter.on_processing_start(event))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        self.assertEqual(active_platform_turns(), 1)

        finish_platform_turn("talk", "room-blocker")
        await asyncio.wait_for(task, timeout=1.0)
        self.assertEqual(active_platform_turns(), 1)
        adapter._apply_label_to_card.assert_awaited_once()
        adapter._remove_label_from_card.assert_awaited_once()
        await adapter.on_processing_complete(event, "cancelled")
        self.assertEqual(active_platform_turns(), 0)
        adapter._rebaseline_card.assert_not_awaited()


class TestCountActiveCards(unittest.IsolatedAsyncioTestCase):
    """FUNC-002: _count_active_cards ist die Zähl-Basis des WIP-Guards —
    zählt in-flight Turns und respektiert max_in_progress + exclude_card_id."""

    async def test_counts_inflight_turns(self):
        adapter = _make_adapter()
        adapter.runtime.max_in_progress = 2
        adapter._active_turns = {"a", "b", "c"}
        self.assertEqual(await adapter._count_active_cards(), 3)

    async def test_excludes_current_card(self):
        adapter = _make_adapter()
        adapter.runtime.max_in_progress = 2
        adapter._active_turns = {"a", "b", "c"}
        self.assertEqual(await adapter._count_active_cards(exclude_card_id="b"), 2)

    async def test_talk_turn_counts_against_deck_wip_limit(self):
        adapter = _make_adapter()
        adapter.runtime.max_in_progress = 1
        adapter._active_turns.clear()
        start_platform_turn("talk", "room-123")
        try:
            self.assertEqual(await adapter._count_active_cards(), 1)
        finally:
            finish_platform_turn("talk", "room-123")
        self.assertEqual(await adapter._count_active_cards(), 0)

    async def test_returns_zero_when_limit_disabled(self):
        # max_in_progress == 0 (Default) => WIP-Limit deaktiviert => immer 0.
        adapter = _make_adapter()
        adapter.runtime.max_in_progress = 0
        adapter._active_turns = {"a", "b"}
        self.assertEqual(await adapter._count_active_cards(), 0)


if __name__ == "__main__":
    unittest.main()
