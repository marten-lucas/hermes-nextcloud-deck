import os
import unittest
from unittest.mock import AsyncMock, MagicMock

from identity import DeckIdentityResolver
from state import DeckCardSnapshot, DeckStateManager
from adapter import NextcloudDeckPlatform, env_enablement, validate_deck_config_from_env


class TestNextcloudDeckPlatform(unittest.TestCase):
    def test_identity_resolver_bot_assigned(self):
        resolver = DeckIdentityResolver(bot_user_id="hermes")
        card = {"assignedUsers": [{"uid": "hermes"}]}
        actor_id, _ = resolver.resolve_card_actor(card, comment_author="alice")
        self.assertEqual(actor_id, "hermes")

    def test_identity_resolver_comment_author_fallback(self):
        resolver = DeckIdentityResolver(bot_user_id="hermes")
        card = {"assignedUsers": [{"uid": "bob"}]}
        actor_id, _ = resolver.resolve_card_actor(card, comment_author="alice")
        self.assertEqual(actor_id, "alice")

    def test_state_manager_deduplication(self):
        mgr = DeckStateManager()
        snap1 = DeckCardSnapshot(
            board_id="1", stack_id="10", card_id="100", title="Test", description="Desc"
        )
        snap2 = DeckCardSnapshot(
            board_id="1", stack_id="10", card_id="100", title="Test", description="Desc"
        )
        self.assertTrue(mgr.should_process(snap1))
        self.assertFalse(mgr.should_process(snap2))

    def test_validate_deck_config_from_env(self):
        os.environ["NEXTCLOUD_DECK_BASE_URL"] = "https://cloud.example.org"
        os.environ["NEXTCLOUD_DECK_USERNAME"] = "hermes"
        os.environ["NEXTCLOUD_DECK_APP_PASSWORD"] = "secret"
        self.assertTrue(validate_deck_config_from_env())

    def test_env_enablement_contract(self):
        os.environ["NEXTCLOUD_DECK_BASE_URL"] = "https://cloud.example.org"
        os.environ["NEXTCLOUD_DECK_USERNAME"] = "hermes"
        os.environ["NEXTCLOUD_DECK_APP_PASSWORD"] = "secret"
        res = env_enablement()
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get("base_url"), "https://cloud.example.org")

        os.environ.pop("NEXTCLOUD_DECK_BASE_URL", None)
        self.assertIsNone(env_enablement())


if __name__ == "__main__":
    unittest.main()