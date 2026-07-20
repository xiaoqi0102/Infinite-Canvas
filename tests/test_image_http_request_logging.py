import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("INFINITE_CANVAS_SKIP_STATIC_SYNC", "1")

import main
from plugins.video_plugins.common import video_http_preview_value


class _Response:
    status_code = 200
    text = '{"data":[{"url":"https://media.example.com/generated.png?token=result-secret"}]}'
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": "image-request-1",
        "Set-Cookie": "session=response-secret",
    }

    def json(self):
        return {
            "id": "image-request-1",
            "data": [{"url": "https://media.example.com/generated.png?token=result-secret"}],
        }

    def raise_for_status(self):
        return None


class _RecordingClient:
    def __init__(self, *args, **kwargs):
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response()


class ImageHttpRequestLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_image_submission_reports_redacted_http_exchange(self):
        provider = {
            "id": "custom-image",
            "name": "自定义图片平台",
            "base_url": "https://api.example.com/v1",
            "protocol": "openai",
            "image_request_mode": "openai",
            "image_models": ["gpt-image-2"],
            "enabled": True,
        }
        snapshots = []
        client = _RecordingClient()

        with (
            patch.object(main, "get_api_provider", return_value=provider),
            patch.object(main, "api_headers", return_value={
                "Accept": "application/json",
                "Authorization": "Bearer real-image-secret",
                "Content-Type": "application/json",
            }),
            patch.object(main.httpx, "AsyncClient", return_value=client),
        ):
            image, raw = await main.generate_ai_image(
                "生成一张测试图片",
                "1024x1024",
                "high",
                "gpt-image-2",
                provider_id=provider["id"],
                progress=lambda patch_data: snapshots.append(
                    json.loads(json.dumps(patch_data, ensure_ascii=False))
                ),
                request_attempts=[],
            )

        self.assertEqual(image["value"], "https://media.example.com/generated.png?token=result-secret")
        self.assertEqual(raw["id"], "image-request-1")
        self.assertEqual(len(client.posts), 1)
        details = snapshots[-1]["request_details"]
        self.assertEqual(details["transport"], "backend_http")
        self.assertEqual(details["context"]["provider_id"], provider["id"])
        self.assertEqual(len(details["attempts"]), 1)
        exchange = details["attempts"][0]
        self.assertEqual(exchange["request"]["url"], "https://api.example.com/v1/images/generations")
        self.assertEqual(exchange["request"]["headers"]["Authorization"], "Bearer YOUR_API_KEY")
        self.assertEqual(exchange["response"]["status_code"], 200)
        self.assertEqual(exchange["response"]["headers"]["X-Request-ID"], "image-request-1")
        serialized = json.dumps(details, ensure_ascii=False)
        self.assertNotIn("real-image-secret", serialized)
        self.assertNotIn("result-secret", serialized)
        self.assertNotIn("response-secret", serialized)

    async def test_canvas_image_task_copies_request_details_into_success_result(self):
        task_id = "canvas_img_log_test"
        request_details = {
            "transport": "backend_http",
            "attempts": [{
                "request": {"method": "POST", "url": "https://api.example.com/v1/images/generations"},
                "response": {"received": True, "status_code": 200},
            }],
        }
        payload = main.OnlineImageRequest(
            prompt="测试",
            provider_id="custom-image",
            model="gpt-image-2",
        )

        async def fake_build(_payload, progress=None):
            progress({"request_details": request_details})
            return {"images": ["/assets/output/generated.png"]}

        with main.CANVAS_TASK_LOCK:
            main.CANVAS_TASKS[task_id] = {"id": task_id, "status": "queued"}
        try:
            with patch.object(main, "build_online_image_result", side_effect=fake_build):
                await main.run_canvas_image_task(task_id, payload)
            with main.CANVAS_TASK_LOCK:
                task = dict(main.CANVAS_TASKS[task_id])
            self.assertEqual(task["status"], "succeeded")
            self.assertEqual(task["request_details"], request_details)
            self.assertEqual(task["result"]["request_details"], request_details)
        finally:
            with main.CANVAS_TASK_LOCK:
                main.CANVAS_TASKS.pop(task_id, None)

    async def test_canvas_image_task_keeps_request_details_after_failure(self):
        task_id = "canvas_img_log_failure_test"
        request_details = {
            "transport": "backend_http",
            "attempts": [{
                "request": {"method": "POST", "url": "https://api.example.com/v1/images/generations"},
                "response": {"received": False, "error_type": "ConnectError"},
            }],
        }
        payload = main.OnlineImageRequest(
            prompt="失败测试",
            provider_id="custom-image",
            model="gpt-image-2",
        )

        async def fake_build(_payload, progress=None):
            progress({"request_details": request_details})
            raise main.HTTPException(status_code=502, detail="上游连接失败")

        with main.CANVAS_TASK_LOCK:
            main.CANVAS_TASKS[task_id] = {"id": task_id, "status": "queued"}
        try:
            with patch.object(main, "build_online_image_result", side_effect=fake_build):
                await main.run_canvas_image_task(task_id, payload)
            with main.CANVAS_TASK_LOCK:
                task = dict(main.CANVAS_TASKS[task_id])
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["status_code"], 502)
            self.assertEqual(task["request_details"], request_details)
        finally:
            with main.CANVAS_TASK_LOCK:
                main.CANVAS_TASKS.pop(task_id, None)

    def test_long_inline_data_is_omitted_from_http_preview(self):
        encoded = "A" * 4096
        preview = video_http_preview_value({
            "inlineData": {
                "mimeType": "image/png",
                "data": encoded,
            },
        })
        serialized = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn(encoded, serialized)
        self.assertIn("内嵌数据已省略", serialized)


if __name__ == "__main__":
    unittest.main()
