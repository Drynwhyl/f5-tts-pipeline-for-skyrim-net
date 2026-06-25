import tempfile
import unittest
from pathlib import Path

from unknown_emotions import (
    append_emotion_alias,
    is_unknown_emotion_word,
    load_unknown_emotions,
    record_unknown_emotion,
    resolve_unknown_emotion,
)


class UnknownEmotionTests(unittest.TestCase):
    def test_unknown_detection_ignores_core_and_aliases(self):
        cfg = {"emotion_map": {"aggressive": ["furious", "angry"]}}

        self.assertFalse(is_unknown_emotion_word("happy", cfg))
        self.assertFalse(is_unknown_emotion_word("furious", cfg))
        self.assertFalse(is_unknown_emotion_word("aggressive", cfg))
        self.assertTrue(is_unknown_emotion_word("irritated", cfg))

    def test_record_updates_count_and_seen_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unknown_emotion_aliases.json"
            cfg = {"emotion_map": {"happy": ["joyful"]}}

            self.assertTrue(record_unknown_emotion(path, "gleeful", cfg, voice="lidia", input_text="[gleeful] Hello"))
            self.assertTrue(record_unknown_emotion(path, "Gleeful", cfg, voice="lidia", input_text="[gleeful] Again"))

            entry = load_unknown_emotions(path)["pending"]["gleeful"]
            self.assertEqual(entry["count"], 2)
            self.assertEqual(entry["word"], "gleeful")
            self.assertEqual(entry["last_voice"], "lidia")
            self.assertIn("first_seen", entry)
            self.assertIn("last_seen", entry)
            self.assertIn("Again", entry["last_input_excerpt"])

    def test_ignored_word_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unknown_emotion_aliases.json"
            cfg = {"emotion_map": {}}

            self.assertTrue(record_unknown_emotion(path, "stern", cfg))
            self.assertTrue(resolve_unknown_emotion(path, "stern", "ignore"))
            self.assertFalse(record_unknown_emotion(path, "stern", cfg))

            queue = load_unknown_emotions(path)
            self.assertNotIn("stern", queue["pending"])
            self.assertIn("stern", queue["ignored"])

    def test_mapping_updates_emotion_map_without_losing_config(self):
        cfg = {
            "emotion_tag": {"open": "[", "close": "]"},
            "emotion_map": {"happy": ["joyful"], "sad": ["wistful"]},
            "default_nfe_step": 16,
        }

        self.assertTrue(append_emotion_alias(cfg, "happy", "wistful"))

        self.assertEqual(cfg["default_nfe_step"], 16)
        self.assertEqual(cfg["emotion_tag"]["open"], "[")
        self.assertIn("joyful", cfg["emotion_map"]["happy"])
        self.assertIn("wistful", cfg["emotion_map"]["happy"])
        self.assertNotIn("sad", cfg["emotion_map"])


if __name__ == "__main__":
    unittest.main()
