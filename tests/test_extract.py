"""Regression checks for refreshes reporting success without classifications."""
import sys
import json
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import anthropic
import openai

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import extract


class ExtractionFailureTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {
            "LLM_PROVIDER": "openai", "OPENAI_MODEL": "gpt-4.1-mini",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.load_env_patch = patch.object(extract.common, "load_env")
        self.load_env_patch.start()
        self.addCleanup(self.load_env_patch.stop)

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
            patch.object(extract.openai, "OpenAI"),
            patch.object(extract, "classify_openai", return_value={}),
            patch.object(extract.common, "write_json") as write,
        ):
            with self.assertRaisesRegex(RuntimeError, "Classification incomplete"):
                extract.main()
            write.assert_not_called()

    def test_auth_preflight_does_not_read_or_write_post_data(self):
        with (
            patch.object(sys, "argv", ["extract.py", "--check-auth"]),
            patch.object(extract.common, "require_env"),
            patch.object(extract.openai, "OpenAI") as client,
            patch.object(extract.common, "read_json") as read,
            patch.object(extract.common, "write_json") as write,
        ):
            self.assertEqual(extract.main(), 0)
            client.return_value.models.retrieve.assert_called_once_with("gpt-4.1-mini")
            read.assert_not_called()
            write.assert_not_called()

    def test_anthropic_remains_explicitly_selectable(self):
        with (
            patch.object(sys, "argv", ["extract.py", "--provider", "anthropic", "--check-auth"]),
            patch.object(extract.common, "require_env") as key,
            patch.object(extract.anthropic, "Anthropic") as client,
            patch.object(extract.openai, "OpenAI") as other,
        ):
            self.assertEqual(extract.main(), 0)
            key.assert_called_once_with("ANTHROPIC_API_KEY")
            client.return_value.models.retrieve.assert_called_once_with(extract.MODEL)
            other.assert_not_called()

    def test_openai_posts_and_roundup_venues_use_same_schema(self):
        client = Mock()
        verdict = {"is_restaurant": True, "categories": ["咖啡"]}
        client.responses.create.return_value = Mock(
            status="completed", output_text=json.dumps(verdict))
        schema = extract.build_schema(["咖啡"])
        header = {"name": "Cafe", "tagline": "coffee", "dishes": []}
        self.assertEqual(extract.classify_openai(
            client, [("p", {}, header)], schema, "test-model"), {"p": verdict})
        venues = extract.classify_venues(
            client, {"p": {"caption": "Two cafes"}},
            {"p": {"pin_count": 2, "venues": [{"name": "Cafe"}]}},
            ["咖啡"], {}, False, "openai", "test-model")
        self.assertEqual(venues, {extract.venue_key("Cafe"): verdict})
        self.assertEqual(client.responses.create.call_count, 2)
        for call in client.responses.create.call_args_list:
            self.assertEqual(call.kwargs["model"], "test-model")
            self.assertEqual(call.kwargs["text"]["format"]["schema"], schema)
            self.assertTrue(call.kwargs["text"]["format"]["strict"])
            self.assertFalse(call.kwargs["store"])

    def test_openai_refusals_incomplete_and_invalid_results_fail(self):
        client = Mock()
        for status, text in [
            ("incomplete", '{"is_restaurant":true,"categories":[]}'),
            ("completed", ""),
            ("completed", '{"is_restaurant":true,"categories":["invented"]}'),
            ("completed", '{"is_restaurant":"yes","categories":[]}'),
        ]:
            with self.subTest(status=status, text=text):
                client.responses.create.return_value = Mock(status=status, output_text=text)
                with self.assertRaises(RuntimeError):
                    extract.openai_verdict(client, "system", "prompt", extract.build_schema([]), "m")

    def test_openai_authentication_error_propagates(self):
        client = Mock()
        client.responses.create.side_effect = openai.AuthenticationError(
            "invalid key", response=Mock(status_code=401, headers={}), body=None)
        with self.assertRaises(openai.AuthenticationError):
            extract.openai_verdict(client, "system", "prompt", extract.build_schema([]), "m")


if __name__ == "__main__":
    unittest.main()
