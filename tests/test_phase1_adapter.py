from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = REPO_ROOT / "adapter.py"
SPEC = importlib.util.spec_from_file_location("nextcloud_deck_adapter", ADAPTER_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.closed = False
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        for suffix, route in self.routes.items():
            if url.endswith(suffix):
                status = route.get("status", 200)
                payload = route.get("payload")
                return FakeResponse(payload, status=status)
        raise AssertionError(f"Unexpected URL requested: {url}")

    async def close(self):
        self.closed = True


class Phase1AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            extra={
                "base_url": "https://cloud.example.org",
                "username": "hermes",
                "app_password": "secret",
                "hermes_user_id": "hermes-user",
                "poll_interval_seconds": 30,
            },
            token="",
        )

    async def test_fetch_boards_once_discovers_boards(self):
        adapter = MODULE.NextcloudDeckPlatform(self.config)
        fake_session = FakeSession({
            "boards": {
                "payload": [
                    {"id": 1, "title": "Board One"},
                    {"id": 2, "title": "Board Two"},
                ]
            }
        })
        adapter._session = fake_session

        boards = await adapter.fetch_boards_once()

        self.assertEqual(2, len(boards))
        self.assertEqual({"1", "2"}, set(adapter.discovered_boards))
        self.assertEqual(
            "https://cloud.example.org/index.php/apps/deck/api/v1.0/boards",
            fake_session.calls[0]["url"],
        )

    async def test_connect_fetches_boards_and_starts_polling(self):
        adapter = MODULE.NextcloudDeckPlatform(self.config)
        fake_session = FakeSession({"boards": {"payload": [{"id": 7, "title": "Infra"}]}})
        adapter._session = fake_session

        connected = await adapter.connect()

        self.assertTrue(connected)
        self.assertIn("7", adapter.discovered_boards)
        self.assertIsNotNone(adapter._polling_task)
        await adapter.disconnect()
        self.assertTrue(fake_session.closed)

    async def test_send_reports_phase1_not_implemented(self):
        adapter = MODULE.NextcloudDeckPlatform(self.config)

        result = await adapter.send("card-1", "hello")

        self.assertFalse(result.success)
        self.assertIn("phase 1", result.error.lower())

    async def test_poll_once_ingests_assigned_cards_for_configured_boards(self):
        config = SimpleNamespace(
            extra={
                "base_url": "https://cloud.example.org",
                "username": "hermes",
                "app_password": "secret",
                "hermes_user_id": "hermes-user",
                "boards": [{"board_id": "7"}],
            },
            token="",
        )
        adapter = MODULE.NextcloudDeckPlatform(config)
        captured = []

        async def _capture(event):
            captured.append(event)

        adapter.handle_message = _capture
        adapter._session = FakeSession(
            {
                "boards": {"payload": [{"id": 7, "title": "Infra"}]},
                "boards/7/stacks": {
                    "payload": [
                        {
                            "id": 70,
                            "title": "Backlog",
                            "cards": [
                                {
                                    "id": 701,
                                    "title": "Prepare rollout",
                                    "description": "- [ ] write note\n- [x] collect logs",
                                    "assignedUsers": [{"uid": "hermes-user", "displayname": "Hermes"}],
                                }
                            ],
                        }
                    ]
                },
                "cards/701/comments": {
                    "payload": [
                        {
                            "id": 900,
                            "message": "Please handle this first.",
                            "actor": {"uid": "alice", "displayname": "Alice"},
                        }
                    ]
                },
            }
        )

        events = await adapter.poll_once()

        self.assertEqual(1, len(events))
        self.assertEqual(1, len(captured))
        self.assertIn("Prepare rollout", captured[0].text)
        self.assertEqual(
            [
                {"checked": False, "text": "write note"},
                {"checked": True, "text": "collect logs"},
            ],
            captured[0].raw_message["card"]["checklist_items"],
        )

    async def test_poll_once_skips_unchanged_cards(self):
        config = SimpleNamespace(
            extra={
                "base_url": "https://cloud.example.org",
                "username": "hermes",
                "app_password": "secret",
                "hermes_user_id": "hermes-user",
                "boards": [{"board_id": "7"}],
            },
            token="",
        )
        adapter = MODULE.NextcloudDeckPlatform(config)
        captured = []

        async def _capture(event):
            captured.append(event)

        adapter.handle_message = _capture
        adapter._session = FakeSession(
            {
                "boards": {"payload": [{"id": 7, "title": "Infra"}]},
                "boards/7/stacks": {
                    "payload": [
                        {
                            "id": 70,
                            "title": "Backlog",
                            "cards": [
                                {
                                    "id": 701,
                                    "title": "Prepare rollout",
                                    "description": "- [ ] write note",
                                    "assignedUsers": [{"uid": "hermes-user", "displayname": "Hermes"}],
                                }
                            ],
                        }
                    ]
                },
                "cards/701/comments": {"payload": []},
            }
        )

        first = await adapter.poll_once()
        second = await adapter.poll_once()

        self.assertEqual(1, len(first))
        self.assertEqual(0, len(second))
        self.assertEqual(1, len(captured))


class ConfigHelpersTests(unittest.TestCase):
    def test_validate_config_requires_hermes_user_id(self):
        config = SimpleNamespace(
            extra={
                "base_url": "https://cloud.example.org",
                "username": "hermes",
                "app_password": "secret",
            },
            token="",
        )
        self.assertFalse(MODULE.validate_nextcloud_deck_config(config))

    def test_env_enablement_reads_required_vars(self):
        original = {key: os.environ.get(key) for key in (
            "NEXTCLOUD_BASE_URL",
            "NEXTCLOUD_USERNAME",
            "NEXTCLOUD_APP_PASSWORD",
            "NEXTCLOUD_DECK_HERMES_USER_ID",
            "NEXTCLOUD_DECK_POLL_INTERVAL_SECONDS",
        )}
        try:
            os.environ["NEXTCLOUD_BASE_URL"] = "https://cloud.example.org"
            os.environ["NEXTCLOUD_USERNAME"] = "hermes"
            os.environ["NEXTCLOUD_APP_PASSWORD"] = "secret"
            os.environ["NEXTCLOUD_DECK_HERMES_USER_ID"] = "hermes-user"
            os.environ["NEXTCLOUD_DECK_POLL_INTERVAL_SECONDS"] = "15"

            seed = MODULE.env_enablement()

            self.assertEqual("hermes-user", seed["hermes_user_id"])
            self.assertEqual("15", seed["poll_interval_seconds"])
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class RegisterTests(unittest.TestCase):
    def test_register_exposes_expected_platform(self):
        captured = {}

        class Ctx:
            def register_platform(self, **kwargs):
                captured.update(kwargs)

        MODULE.register(Ctx())

        self.assertEqual("nextcloud_deck", captured["name"])
        self.assertIn("NEXTCLOUD_DECK_HERMES_USER_ID", captured["required_env"])
