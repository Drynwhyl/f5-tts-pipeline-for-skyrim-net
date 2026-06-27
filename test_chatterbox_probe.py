import io
import json
import unittest
import wave

from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatterbox_probe import (
    CHATTERBOX_DATA_FIELDS,
    PROBE_AUDIO_BYTES,
    ChatterboxProbeState,
    decode_chatterbox_data,
    parse_chatterbox_tags,
    router,
    set_audio_generator,
)


class ChatterboxProbeTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self):
        set_audio_generator(None)

    def test_decodes_all_known_payload_fields(self):
        data = list(range(len(CHATTERBOX_DATA_FIELDS)))
        data[1] = "[happy] This tag must survive."
        data[2] = "ru"

        decoded = decode_chatterbox_data(data)

        self.assertEqual(decoded["text"], "[happy] This tag must survive.")
        self.assertEqual(decoded["language"], "ru")
        self.assertEqual(decoded["seed"], 26)
        self.assertEqual(decoded["entity_uuid"], 27)
        self.assertNotIn("extra_fields", decoded)

    def test_probe_state_expires_events(self):
        state = ChatterboxProbeState(ttl_seconds=-1)
        event_id, _ = state.create_event({"data": []}, [])

        self.assertIsNone(state.get_event(event_id))

    def test_parses_emotion_and_strips_all_chatterbox_tags(self):
        emotion, text, tags = parse_chatterbox_tags(
            "[happy] Ну наконец-то. [chuckle]",
            {"happy": "happy"},
        )

        self.assertEqual(emotion, "happy")
        self.assertEqual(text, "Ну наконец-то.")
        self.assertEqual(tags, ["happy", "chuckle"])

    def test_full_gradio_probe_flow(self):
        async def generate_audio(_decoded):
            return PROBE_AUDIO_BYTES

        set_audio_generator(generate_audio)
        upload = self.client.post(
            "/gradio_api/upload",
            files={"files": ("voice.wav", PROBE_AUDIO_BYTES, "audio/wav")},
        )
        self.assertEqual(upload.status_code, 200)
        uploaded_path = upload.json()[0]
        self.assertTrue(uploaded_path.startswith("/tmp/gradio/"))
        uploaded_audio = self.client.get(f"/gradio_api/file={uploaded_path}")
        self.assertEqual(uploaded_audio.status_code, 200)
        self.assertEqual(uploaded_audio.content, PROBE_AUDIO_BYTES)

        data = [None] * len(CHATTERBOX_DATA_FIELDS)
        data[1] = "[whispering] Тихая проверка."
        data[2] = "ru"
        data[3] = {
            "path": uploaded_path,
            "orig_name": "voice.wav",
            "mime_type": "audio/wav",
            "meta": {"_type": "gradio.FileData"},
        }
        data[26] = 1234
        data[27] = 824914275390249349

        submit = self.client.post(
            "/gradio_api/call/generate_audio",
            json={"data": data},
        )
        self.assertEqual(submit.status_code, 200)
        event_id = submit.json()["event_id"]

        result = self.client.get(f"/gradio_api/call/generate_audio/{event_id}")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.headers["content-type"].startswith("text/event-stream"))
        payload_line = next(
            line for line in result.text.splitlines() if line.startswith("data: ")
        )
        result_payload = json.loads(payload_line.removeprefix("data: "))
        self.assertEqual(result_payload[1], 1234)
        self.assertEqual(result_payload[0]["size"], len(PROBE_AUDIO_BYTES))

        audio = self.client.get(f"/gradio_api/file={result_payload[0]['path']}")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.headers["content-type"], "audio/wav")
        with wave.open(io.BytesIO(audio.content), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 24000)
            self.assertEqual(wav.getnframes(), 2400)


if __name__ == "__main__":
    unittest.main()
