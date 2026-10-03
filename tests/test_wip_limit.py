import sys
import unittest
from types import SimpleNamespace

# Clear any cached gateway imports to ensure adapter uses its fallback classes
for _mod in (
    "gateway",
    "gateway.config",
    "gateway.platforms",
    "gateway.platforms.base",
):
    if _mod in sys.modules:
        del sys.modules[_mod]

from adapter import NextcloudDeckPlatform


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

    async def test_returns_zero_when_limit_disabled(self):
        # max_in_progress == 0 (Default) => WIP-Limit deaktiviert => immer 0.
        adapter = _make_adapter()
        adapter.runtime.max_in_progress = 0
        adapter._active_turns = {"a", "b"}
        self.assertEqual(await adapter._count_active_cards(), 0)


if __name__ == "__main__":
    unittest.main()
