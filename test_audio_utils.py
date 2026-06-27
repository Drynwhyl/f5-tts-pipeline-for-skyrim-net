import tempfile
import unittest
from pathlib import Path

from pydub import AudioSegment
from pydub.generators import Sine

from audio_utils import trim_audio_to_sentence_boundary


class AudioTrimTests(unittest.TestCase):
    def test_short_first_segment_falls_back_to_full_reference_window(self):
        first_phrase = Sine(220).to_audio_segment(duration=1450).apply_gain(-18)
        silence = AudioSegment.silent(duration=1100)
        long_phrase = Sine(240).to_audio_segment(duration=11050).apply_gain(-18)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reference.wav"
            with path.open("wb") as output:
                (first_phrase + silence + long_phrase).export(output, format="wav")

            message = trim_audio_to_sentence_boundary(str(path), max_ms=12000)
            with path.open("rb") as source:
                duration_ms = len(AudioSegment.from_file(source))

        self.assertIn("Audio trimmed", message)
        self.assertGreaterEqual(duration_ms, 11900)
        self.assertLessEqual(duration_ms, 12000)


if __name__ == "__main__":
    unittest.main()
