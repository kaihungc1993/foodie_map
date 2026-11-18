"""Regression checks for refreshes reporting success without classifications."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import extract


class ExtractionFailureTests(unittest.TestCase):
    def test_sync_authentication_error_is_not_swallowed(self):
        response = Mock(status_code=401, headers={})
        client = Mock()
        client.messages.create.side_effect = anthropic.AuthenticationError(
            "invalid x-api-key", response=response, body=None)
        with self.assertRaises(anthropic.AuthenticationError):
            extract.classify_sync(
                client, [("post1", {}, {"name": "Cafe", "tagline": "", "dishes": []})], {})

    def test_incomplete_classification_does_not_write_cache(self):
        posts = {"post1": {"ownerUsername": "born2eat_taiwan"}}
        with (
            patch.object(sys, "argv", ["extract.py"]),
            patch.object(extract.common, "require_env"),
            patch.object(extract.common, "read_json", side_effect=[posts, {}]),
            patch.object(extract.common, "categories", return_value=[]),
            patch.object(extract.parsers, "parse", return_value={"name": "Cafe"}),
            patch.object(extract.anthropic, "Anthropic"),
            patch.object(extract, "classify_sync", return_value={}),
            patch.object(extract.common, "write_json") as write,
        ):
            with self.assertRaisesRegex(RuntimeError, "Classification incomplete"):
                extract.main()
            write.assert_not_called()

    def test_auth_preflight_does_not_read_or_write_post_data(self):
        with (
            patch.object(sys, "argv", ["extract.py", "--check-auth"]),
            patch.object(extract.common, "require_env"),
            patch.object(extract.anthropic, "Anthropic") as client,
            patch.object(extract.common, "read_json") as read,
            patch.object(extract.common, "write_json") as write,
        ):
            self.assertEqual(extract.main(), 0)
            client.return_value.models.list.assert_called_once_with(limit=1)
            read.assert_not_called()
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
