import os
import sys
import unittest
from types import SimpleNamespace

# Clear any cached gateway imports to ensure adapter uses its fallback classes
if 'gateway' in sys.modules:
    del sys.modules['gateway']
if 'gateway.config' in sys.modules:
    del sys.modules['gateway.config']
if 'gateway.platforms' in sys.modules:
    del sys.modules['gateway.platforms']
if 'gateway.platforms.base' in sys.modules:
    del sys.modules['gateway.platforms.base']

# Now import adapter - it will use the fallback classes since gateway is not available
from adapter import (
    NextcloudDeckPlatform,
    env_enablement,
    validate_deck_config_from_env,
)
from identity import DeckIdentityResolver
from state import DeckCardSnapshot, DeckStateManager


class TestNextcloudDeckPlatform(unittest.TestCase):
    def test_identity_resolver_bot_assigned(self):
        resolver = DeckIdentityResolver(bot_user_id="hermes")
        card = {"assignedUsers": [{"uid": "hermes"}]}
        actor, groups = resolver.resolve_card_actor(card, comment_author="alice")
        self.assertEqual(actor, "hermes")
        self.assertEqual(groups, [])

    def test_identity_resolver_comment_author_fallback(self):
        resolver = DeckIdentityResolver(bot_user_id="hermes")
        card = {"assignedUsers": [{"uid": "bob"}]}
        actor, _ = resolver.resolve_card_actor(card, comment_author="alice")
        self.assertEqual(actor, "alice")

    def test_state_manager_deduplication_and_change_detection(self):
        mgr = DeckStateManager()
        base = dict(board_id="1", stack_id="10", card_id="100", title="Test", description="Desc")
        self.assertTrue(mgr.should_process(DeckCardSnapshot(**base)))
        self.assertFalse(mgr.should_process(DeckCardSnapshot(**base)))
        changed = dict(base, description="Changed")
        self.assertTrue(mgr.should_process(DeckCardSnapshot(**changed)))

    def test_validate_deck_config_from_env(self):
        old = {k: os.environ.get(k) for k in (
            "NEXTCLOUD_DECK_BASE_URL",
            "NEXTCLOUD_DECK_USERNAME",
            "NEXTCLOUD_DECK_APP_PASSWORD",
        )}
        try:
            os.environ["NEXTCLOUD_DECK_BASE_URL"] = "https://cloud.example.org"
            os.environ["NEXTCLOUD_DECK_USERNAME"] = "hermes"
            os.environ["NEXTCLOUD_DECK_APP_PASSWORD"] = "secret"
            self.assertTrue(validate_deck_config_from_env())
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_env_enablement_contract(self):
        os.environ["NEXTCLOUD_DECK_BASE_URL"] = "https://cloud.example.org"
        os.environ["NEXTCLOUD_DECK_USERNAME"] = "hermes"
        os.environ["NEXTCLOUD_DECK_APP_PASSWORD"] = "secret"
        result = env_enablement()
        self.assertIsInstance(result, dict)
        self.assertEqual(result["base_url"], "https://cloud.example.org")

    def test_runtime_requires_explicit_boards_for_ingestion(self):
        config = SimpleNamespace(
            extra={
                "base_url": "https://cloud.example.org",
                "username": "hermes",
                "app_password": "secret",
                "hermes_user_id": "hermes",
            }
        )
        adapter = NextcloudDeckPlatform(config)
        self.assertEqual(adapter.runtime.boards, {})
        self.assertEqual(
            adapter._card_id_from_target("deck:board:1:card:100"),
            "100",
        )


if __name__ == "__main__":
    unittest.main()
