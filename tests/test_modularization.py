import ast
import os
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

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_NAMES = ("wip", "ingestion", "formatting", "execution")

from adapter import NextcloudDeckPlatform
import wip
import ingestion
import formatting
import execution


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


class TestModuleBoundaries(unittest.TestCase):
    """Import-Richtung: Die Kern-Module importieren NICHT aus ``adapter``
    (Adapter-Instanz wird als Argument übergeben — keine Zyklen)."""

    def test_modules_importable(self):
        for name in MODULE_NAMES:
            module = sys.modules.get(name)
            self.assertIsNotNone(module, f"Modul {name} ist nicht importierbar")
            self.assertTrue(os.path.exists(os.path.join(PLUGIN_DIR, f"{name}.py")))

    def test_no_adapter_import_in_modules(self):
        for name in MODULE_NAMES:
            with open(os.path.join(PLUGIN_DIR, f"{name}.py"), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            offenders = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == "adapter" or alias.name.startswith("adapter.") for alias in node.names):
                        offenders.append(node.lineno)
                elif isinstance(node, ast.ImportFrom):
                    if node.module and (node.module == "adapter" or node.module.startswith("adapter.")):
                        offenders.append(node.lineno)
            self.assertEqual(offenders, [], f"{name}.py importiert aus adapter (Zeilen {offenders})")


class TestContractExposure(unittest.TestCase):
    """Der Adapter bleibt der schlanke Platform-Adapter: alle Contract- und
    Delegator-Namen bleiben auf der Klasse sichtbar (Tests + Gateway hängen
    an diesen Namen)."""

    REQUIRED_NAMES = (
        # Platform-Contract (Gateway)
        "send", "send_typing", "stop_typing", "send_or_update_status",
        "mark_turn_started", "mark_turn_finished", "get_chat_info",
        "connect", "disconnect", "check_board_suitability", "setup_test_card",
        "is_connected",
        # Core-Logik (delegiert)
        "poll_once", "_polling_loop", "_process_card", "_rebaseline_card",
        "_ensure_backlog_template", "_run_speed_heartbeat", "_fetch_speed",
        "_speed_suffix", "_card_status_label", "_map_progress_status",
        "_auto_block_card", "_run_has_failure_signal",
        "_run_has_review_intent", "_run_has_block_intent",
        # WIP-/Trigger-Basis
        "_active_turn", "_count_active_cards", "_card_is_triggered",
        # Label-/User-/Move-Operationen
        "_update_card_description", "_move_card_to_status", "_xor_group_of",
        "_remove_xor_siblings", "_normalize_label_conflicts",
        "_apply_label_to_card", "_remove_label_from_card",
        "_assign_user_to_card", "_unassign_user_from_card",
        "_locate_card", "_resolve_target_stack_id",
    )

    def test_adapter_exposes_required_names(self):
        missing = [n for n in self.REQUIRED_NAMES if n not in NextcloudDeckPlatform.__dict__]
        self.assertEqual(missing, [], f"Adapter-Name fehlt: {missing}")


class TestDelegationEquivalencePure(unittest.TestCase):
    """Dünne Delegatoren: Adapter-Methode ≡ Kern-Modul-Funktion (same-in,
    same-out) — für die reinen (synchronen) Funktionen."""

    def test_card_label_titles(self):
        card = {"labels": [{"title": "Zeta"}, {"title": "Alpha"}, {"id": 9}]}
        self.assertEqual(
            NextcloudDeckPlatform._card_label_titles(card),
            ingestion.card_label_titles(card),
        )
        self.assertEqual(NextcloudDeckPlatform._card_label_titles(card), ["Alpha", "Zeta"])

    def test_last_comment_author(self):
        comment = {"actor": {"uid": "user42"}, "message": "hi"}
        self.assertEqual(
            NextcloudDeckPlatform._last_comment_author(comment),
            ingestion.last_comment_author(comment),
        )
        self.assertEqual(NextcloudDeckPlatform._last_comment_author(comment), "user42")
        self.assertIsNone(NextcloudDeckPlatform._last_comment_author({"message": "x"}))

    def test_comment_id(self):
        adapter = _make_adapter()
        for comment in ({"id": "123"}, {"id": 7}, {"id": "abc"}, {}):
            self.assertEqual(
                adapter._comment_id(comment),
                ingestion.comment_id(comment),
            )
        self.assertEqual(adapter._comment_id({"id": "123"}), 123)
        self.assertIsNone(adapter._comment_id({"id": "abc"}))

    def test_speed_suffix(self):
        samples = [
            {"phase": "generate", "tg": 12.5},
            {"phase": "generate"},
            {"phase": "prompt", "prompt_progress": 0.42, "prompt_tps": 130.0},
            {"phase": "prompt"},
            {"phase": "idle"},
        ]
        for sample in samples:
            self.assertEqual(
                NextcloudDeckPlatform._speed_suffix(sample),
                formatting.speed_suffix(sample),
            )
        self.assertEqual(NextcloudDeckPlatform._speed_suffix({"phase": "generate", "tg": 12.5}), "12.5 t/s")
        self.assertEqual(
            NextcloudDeckPlatform._speed_suffix({"phase": "prompt", "prompt_progress": 0.42, "prompt_tps": 130.0}),
            "Kontext 42% · ⏱ 130 t/s",
        )
        self.assertEqual(NextcloudDeckPlatform._speed_suffix({"phase": "idle"}), "")

    def test_map_progress_status(self):
        samples = [
            ("", "loading context"),
            ("_thinking", ""),
            ("_generating", ""),
            ("tool.deck_card_action", ""),
            ("_unknown", "ein langer Status"),
            ("", ""),
        ]
        for status_key, content in samples:
            self.assertEqual(
                NextcloudDeckPlatform._map_progress_status(status_key, content),
                formatting.map_progress_status(status_key, content),
            )
        self.assertEqual(NextcloudDeckPlatform._map_progress_status("_thinking", ""), ("Denkt nach", "🤔"))
        self.assertEqual(NextcloudDeckPlatform._map_progress_status("", ""), (None, None))

    def test_run_intent_signals(self):
        failure_marker = NextcloudDeckPlatform._RUN_FAILURE_MARKERS[0]
        review_marker = NextcloudDeckPlatform._REVIEW_INTENT_MARKERS[0]
        block_marker = NextcloudDeckPlatform._BLOCK_INTENT_MARKERS[0]
        samples = (
            (NextcloudDeckPlatform._run_has_failure_signal, execution.run_has_failure_signal, "unverwandt"),
            (NextcloudDeckPlatform._run_has_failure_signal, execution.run_has_failure_signal, failure_marker),
            (NextcloudDeckPlatform._run_has_failure_signal, execution.run_has_failure_signal, {"text": f"…{failure_marker}…"}),
            (NextcloudDeckPlatform._run_has_review_intent, execution.run_has_review_intent, review_marker),
            (NextcloudDeckPlatform._run_has_review_intent, execution.run_has_review_intent, ""),
            (NextcloudDeckPlatform._run_has_block_intent, execution.run_has_block_intent, block_marker),
            (NextcloudDeckPlatform._run_has_block_intent, execution.run_has_block_intent, "unverwandt"),
        )
        for adapter_fn, module_fn, result in samples:
            self.assertEqual(adapter_fn(result), module_fn(NextcloudDeckPlatform, result))
        self.assertTrue(NextcloudDeckPlatform._run_has_failure_signal(failure_marker))
        self.assertTrue(NextcloudDeckPlatform._run_has_review_intent(review_marker))
        self.assertTrue(NextcloudDeckPlatform._run_has_block_intent(block_marker))
        self.assertFalse(NextcloudDeckPlatform._run_has_failure_signal("unverwandt"))

    def test_card_is_triggered(self):
        adapter = _make_adapter()
        card = {"description": "bitte @hermes prüfen", "labels": []}
        comments = [{"message": "fertig? @hermes"}]
        self.assertEqual(
            adapter._card_is_triggered(card, comments),
            wip.card_is_triggered(adapter, card, comments),
        )
        self.assertTrue(adapter._card_is_triggered(card, comments))
        self.assertFalse(adapter._card_is_triggered({"description": "nix hier"}, []))

    def test_new_comments_since_baseline(self):
        adapter = _make_adapter()
        comments = [{"id": "1"}, {"id": "3"}, {"id": "x"}, {"id": "2"}]
        self.assertEqual(
            adapter._new_comments_since_baseline("b1", "c1", comments),
            ingestion.new_comments_since_baseline(adapter, "b1", "c1", comments),
        )


class TestDelegationEquivalenceAsync(unittest.IsolatedAsyncioTestCase):
    """Dünne Delegatoren: WIP-Context-Manager und Zählung bleiben Verhalten-
    identisch zur Kern-Modul-Variante (FUNC-002-Semantik erhalten)."""

    async def test_active_turn_lifecycle(self):
        adapter = _make_adapter()
        adapter.runtime.speed_enabled = False
        adapter._active_turns.clear()
        async with adapter._active_turn("cardA"):
            self.assertIn("cardA", adapter._active_turns)
        self.assertNotIn("cardA", adapter._active_turns)
        # Direkte Kern-Modul-Variante: gleiche Semantik.
        async with wip.active_turn(adapter, "cardB"):
            self.assertIn("cardB", adapter._active_turns)
        self.assertNotIn("cardB", adapter._active_turns)

    async def test_count_active_cards(self):
        adapter = _make_adapter()
        adapter.runtime.max_in_progress = 2
        adapter._active_turns = {"a", "b", "c"}
        self.assertEqual(
            await adapter._count_active_cards(),
            await wip.count_active_cards(adapter),
        )
        self.assertEqual(
            await adapter._count_active_cards(exclude_card_id="b"),
            await wip.count_active_cards(adapter, exclude_card_id="b"),
        )
        self.assertEqual(await adapter._count_active_cards(exclude_card_id="b"), 2)


if __name__ == "__main__":
    unittest.main()
