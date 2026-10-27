import unittest

from adapter import NextcloudDeckPlatform


class TestPlatformContract(unittest.TestCase):
    def test_adapter_implements_get_chat_info(self):
        self.assertIn("get_chat_info", NextcloudDeckPlatform.__dict__)
        self.assertFalse(getattr(NextcloudDeckPlatform, "__abstractmethods__", set()))

    def test_restart_resume_is_non_interactive(self):
        self.assertFalse(NextcloudDeckPlatform.interactive_resume)


if __name__ == "__main__":
    unittest.main()
