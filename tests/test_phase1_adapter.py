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
    def __init__(self, payload, status: int = 200):
        self.payload = payload
        self.status = status
        self.closed = False
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return FakeResponse(self.payload, status=self.status)

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
        fake_session = FakeSession(
            [
                {"id": 1, "title": "Board One"},
                {"id": 2, "title": "Board Two"},
            ]
        )
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
        fake_session = FakeSession([{"id": 7, "title": "Infra"}])
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
