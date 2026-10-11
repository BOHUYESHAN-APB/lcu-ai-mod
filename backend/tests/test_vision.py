import base64
import io
import unittest
from unittest import mock

from PIL import Image

from agent.llm_service import LLMService
from agent.vision import ScreenCapture, VisionInterpreter


class FakeLLM:
    def __init__(self, response="一只僵尸在正前方", api_key="test-key", error=None):
        self.api_key = api_key
        self._response = response
        self._error = error
        self.vision_calls = []

    def chat_vision(self, prompt, image_jpeg, agent="vision", **kwargs):
        self.vision_calls.append({"prompt": prompt, "size": len(image_jpeg), "agent": agent})
        if self._error:
            raise self._error
        return {"content": self._response}


def big_image(width=1280, height=720):
    return Image.new("RGB", (width, height), (12, 34, 56))


class ScreenCaptureTests(unittest.TestCase):
    def test_grab_jpeg_downscales_and_encodes(self):
        capture = ScreenCapture(grabber=big_image)

        data = capture.grab_jpeg()

        self.assertIsNotNone(data)
        self.assertTrue(data.startswith(b"\xff\xd8"))  # JPEG 魔数
        self.assertEqual(Image.open(io.BytesIO(data)).width, 640)

    def test_unavailable_backend_degrades_silently(self):
        with mock.patch("agent.vision._load_backend", return_value=(None, None)):
            capture = ScreenCapture()

        self.assertFalse(capture.available)
        self.assertIsNone(capture.grab_jpeg())

    def test_grabber_exception_does_not_propagate(self):
        def boom():
            raise RuntimeError("capture exploded")

        capture = ScreenCapture(grabber=boom)

        self.assertIsNone(capture.grab_jpeg())


class VisionInterpreterTests(unittest.TestCase):
    def _interpreter(self, llm, grabber=big_image):
        return VisionInterpreter(llm, capture=ScreenCapture(grabber=grabber))

    def test_refresh_stores_description_under_vision_agent(self):
        llm = FakeLLM()

        text = self._interpreter(llm).refresh()

        self.assertEqual(text, "一只僵尸在正前方")

    def test_latest_exposes_description_without_blocking(self):
        llm = FakeLLM()
        interpreter = self._interpreter(llm)
        interpreter.refresh()

        self.assertEqual(interpreter.latest(), "一只僵尸在正前方")
        self.assertEqual(llm.vision_calls[0]["agent"], "vision")

    def test_vlm_failure_keeps_previous_result_and_records_error(self):
        interpreter = self._interpreter(FakeLLM(error=RuntimeError("vlm down")))

        self.assertIsNone(interpreter.refresh())
        self.assertEqual(interpreter.latest(), "")
        self.assertIn("vlm down", interpreter.get_status()["error"])

    def test_unavailable_without_api_key(self):
        interpreter = self._interpreter(FakeLLM(api_key=None))

        self.assertFalse(interpreter.available)
        self.assertFalse(interpreter.start())
        self.assertFalse(interpreter.get_status()["available"])

    def test_empty_capture_returns_none(self):
        interpreter = self._interpreter(FakeLLM(), grabber=lambda: None)

        self.assertIsNone(interpreter.refresh())


class ChatVisionTests(unittest.TestCase):
    def test_chat_vision_builds_image_data_uri(self):
        service = LLMService()
        captured = {}

        def fake_post(endpoint, data, config=None, timeout=None, agent=None):
            captured["data"] = data
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

        service._post = fake_post
        service.chat_vision("描述画面", b"\xff\xd8jpegbytes", agent="vision")

        parts = captured["data"]["messages"][0]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[0]["text"], "描述画面")
        self.assertEqual(parts[1]["type"], "image_url")
        url = parts[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), b"\xff\xd8jpegbytes")


if __name__ == "__main__":
    unittest.main()
