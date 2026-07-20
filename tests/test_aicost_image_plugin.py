import base64
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from plugins.image_plugins.aicost import (
    AICostImageProtocolError,
    generate_aicost_image,
    is_aicost_image_official_provider,
    query_aicost_image_task,
)


AUTH_HEADERS = {"Authorization": "Bearer test-key"}
PNG_BYTES = b"\x89PNG\r\n\x1a\nmock-image-content"
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode("ascii")


def _response(method, url, status_code, payload):
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request(method, url),
    )


def _snapshot_files(files):
    snapshots = []
    for field, raw in list(files or []):
        filename = raw[0] if isinstance(raw, (list, tuple)) and len(raw) > 0 else None
        content = raw[1] if isinstance(raw, (list, tuple)) and len(raw) > 1 else raw
        content_type = raw[2] if isinstance(raw, (list, tuple)) and len(raw) > 2 else ""
        if isinstance(content, bytes):
            body = content
        elif hasattr(content, "read"):
            position = content.tell()
            body = content.read()
            content.seek(position)
        else:
            body = None
        snapshots.append(
            {
                "field": field,
                "filename": filename,
                "content_type": content_type,
                "body": body,
            }
        )
    return snapshots


class _RecordingClient:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []

    async def request(self, method, url, **kwargs):
        call = {
            "method": str(method).upper(),
            "url": url,
            "headers": dict(kwargs.get("headers") or {}),
            "json": kwargs.get("json"),
            "data": dict(kwargs.get("data") or {}),
            "files": _snapshot_files(kwargs.get("files")),
        }
        self.calls.append(call)
        if not self.events:
            raise AssertionError(f"没有为 {method} {url} 准备响应")
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)


class _AsyncClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class AICostImageTestCase(unittest.IsolatedAsyncioTestCase):
    base_url = "https://www.aicost.xyz/v1"

    @staticmethod
    def _resolve_local_path(value):
        return value if os.path.isfile(str(value or "")) else None

    @staticmethod
    def _content_type_for_path(path):
        return "image/jpeg" if str(path).lower().endswith((".jpg", ".jpeg")) else "image/png"

    async def _generate(self, request, client, **overrides):
        kwargs = {
            "base_url": self.base_url,
            "headers": AUTH_HEADERS,
            "resolve_local_path": self._resolve_local_path,
            "content_type_for_path": self._content_type_for_path,
            "request_timeout": 30,
            "poll_timeout": 30,
            "poll_interval": 0.5,
        }
        kwargs.update(overrides)
        with patch(
            "plugins.image_plugins.aicost.httpx.AsyncClient",
            return_value=_AsyncClientContext(client),
        ) as async_client:
            result = await generate_aicost_image(request, **kwargs)
        return result, async_client


class AICostImageUrlTests(AICostImageTestCase):
    def test_official_provider_detection_uses_exact_hostname(self):
        self.assertTrue(
            is_aicost_image_official_provider(
                {"base_url": "https://www.aicost.xyz/v1"}
            )
        )
        self.assertTrue(
            is_aicost_image_official_provider({"base_url": "https://aicost.xyz"})
        )
        self.assertFalse(
            is_aicost_image_official_provider(
                {"base_url": "https://www.aicost.xyz.evil.example/v1"}
            )
        )

    async def test_generation_base_url_matrix_contains_one_v1(self):
        expected = "https://www.aicost.xyz/v1/images/generations"
        for base_url in (
            "https://www.aicost.xyz",
            "https://www.aicost.xyz/",
            "https://www.aicost.xyz/v1",
            "https://www.aicost.xyz/v1/",
            "https://www.aicost.xyz/v1/v1",
        ):
            client = _RecordingClient(
                [_response("POST", expected, 200, {"data": [{"b64_json": PNG_BASE64}]})]
            )
            with self.subTest(base_url=base_url):
                await self._generate(
                    {"model": "gpt-image-2", "prompt": "测试", "size": "1024x1024"},
                    client,
                    base_url=base_url,
                )
                self.assertEqual(client.calls[0]["url"], expected)

    async def test_query_url_quotes_task_id_as_one_path_segment(self):
        expected = "https://www.aicost.xyz/v1/images/generations/task%2Fid"
        payload = {"task_id": "task/id", "status": "pending"}
        client = _RecordingClient([_response("GET", expected, 200, payload)])
        with patch(
            "plugins.image_plugins.aicost.httpx.AsyncClient",
            return_value=_AsyncClientContext(client),
        ) as async_client:
            actual = await query_aicost_image_task(
                "task/id",
                base_url="https://www.aicost.xyz/v1/v1/",
                headers=AUTH_HEADERS,
                request_timeout=30,
            )

        self.assertEqual(actual, payload)
        self.assertEqual(client.calls[0]["url"], expected)
        self.assertFalse(async_client.call_args.kwargs["trust_env"])


class AICostImageGenerationTests(AICostImageTestCase):
    async def test_generation_sends_documented_fields_and_returns_sync_b64(self):
        endpoint = f"{self.base_url}/images/generations"
        payload = {"data": [{"b64_json": PNG_BASE64}]}
        client = _RecordingClient([_response("POST", endpoint, 200, payload)])

        (image, raw), async_client = await self._generate(
            {
                "model": "gpt-image-2",
                "prompt": "电影感未来城市",
                "size": "3072x1728",
                "quality": "high",
                "output_format": "png",
            },
            client,
        )

        self.assertEqual(image["type"], "b64")
        self.assertEqual(image["value"], PNG_BASE64)
        self.assertEqual(raw, payload)
        self.assertEqual(
            client.calls[0]["json"],
            {
                "model": "gpt-image-2",
                "prompt": "电影感未来城市",
                "n": 1,
                "size": "3072x1728",
                "quality": "auto",
                "output_format": "png",
                "moderation": "auto",
            },
        )
        self.assertFalse(async_client.call_args.kwargs["trust_env"])

    async def test_generation_returns_sync_url(self):
        endpoint = f"{self.base_url}/images/generations"
        payload = {"data": [{"url": "https://cdn.example.com/generated.png"}]}
        client = _RecordingClient([_response("POST", endpoint, 201, payload)])

        (image, raw), _ = await self._generate(
            {"model": "gpt-image-2", "prompt": "产品图", "size": "1024x1024"},
            client,
        )

        self.assertEqual(
            image,
            {"type": "url", "value": "https://cdn.example.com/generated.png"},
        )
        self.assertEqual(raw, payload)

    async def test_generation_disconnect_is_not_retried(self):
        endpoint = f"{self.base_url}/images/generations"
        request = httpx.Request("POST", endpoint)
        client = _RecordingClient(
            [
                httpx.RemoteProtocolError(
                    "Server disconnected without sending a response.",
                    request=request,
                ),
                _response("POST", endpoint, 200, {"data": [{"b64_json": PNG_BASE64}]}),
            ]
        )

        with self.assertRaises(AICostImageProtocolError) as captured:
            await self._generate(
                {"model": "gpt-image-2", "prompt": "测试", "size": "1024x1024"},
                client,
            )

        self.assertEqual(len(client.calls), 1)
        self.assertIn("Server disconnected", captured.exception.detail)
        self.assertEqual(captured.exception.upstream_task_id, "")


class AICostImageEditTests(AICostImageTestCase):
    async def _edit(self, client, reference_paths):
        return await self._generate(
            {
                "model": "gpt-image-2",
                "prompt": "保持人物一致",
                "size": "3072x1728",
                "quality": "medium",
                "output_format": "jpeg",
                "reference_images": [{"url": path} for path in reference_paths],
            },
            client,
        )

    async def test_edit_uses_image_array_field_first(self):
        endpoint = f"{self.base_url}/images/edits"
        payload = {"data": [{"b64_json": PNG_BASE64}]}
        with tempfile.TemporaryDirectory() as temp_dir:
            first = os.path.join(temp_dir, "first.png")
            second = os.path.join(temp_dir, "second.jpg")
            with open(first, "wb") as stream:
                stream.write(b"first-image")
            with open(second, "wb") as stream:
                stream.write(b"second-image")
            client = _RecordingClient([_response("POST", endpoint, 200, payload)])

            (image, _raw), _ = await self._edit(client, [first, second])

        self.assertEqual(image["value"], PNG_BASE64)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual([item["field"] for item in client.calls[0]["files"]], ["image[]", "image[]"])
        self.assertEqual([item["body"] for item in client.calls[0]["files"]], [b"first-image", b"second-image"])
        self.assertEqual(
            client.calls[0]["data"],
            {
                "model": "gpt-image-2",
                "prompt": "保持人物一致",
                "n": "1",
                "size": "3072x1728",
                "quality": "auto",
                "output_format": "jpeg",
                "moderation": "auto",
            },
        )

    async def test_edit_falls_back_to_image_after_explicit_422(self):
        endpoint = f"{self.base_url}/images/edits"
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = os.path.join(temp_dir, "reference.png")
            with open(reference, "wb") as stream:
                stream.write(b"reference-image")
            client = _RecordingClient(
                [
                    _response("POST", endpoint, 422, {"error": {"message": "image[] is unsupported"}}),
                    _response("POST", endpoint, 200, {"data": [{"b64_json": PNG_BASE64}]}),
                ]
            )

            await self._edit(client, [reference])

        self.assertEqual(len(client.calls), 2)
        self.assertEqual([item["field"] for item in client.calls[0]["files"]], ["image[]"])
        self.assertEqual([item["field"] for item in client.calls[1]["files"]], ["image"])
        self.assertEqual(client.calls[0]["files"][0]["body"], b"reference-image")
        self.assertEqual(client.calls[1]["files"][0]["body"], b"reference-image")

    async def test_edit_5xx_does_not_fallback(self):
        endpoint = f"{self.base_url}/images/edits"
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = os.path.join(temp_dir, "reference.png")
            with open(reference, "wb") as stream:
                stream.write(b"reference-image")
            client = _RecordingClient(
                [
                    _response("POST", endpoint, 503, {"error": {"message": "temporarily unavailable"}}),
                    _response("POST", endpoint, 200, {"data": [{"b64_json": PNG_BASE64}]}),
                ]
            )

            with self.assertRaises(AICostImageProtocolError) as captured:
                await self._edit(client, [reference])

        self.assertEqual(captured.exception.status_code, 503)
        self.assertEqual(len(client.calls), 1)

    async def test_edit_disconnect_does_not_fallback(self):
        endpoint = f"{self.base_url}/images/edits"
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = os.path.join(temp_dir, "reference.png")
            with open(reference, "wb") as stream:
                stream.write(b"reference-image")
            client = _RecordingClient(
                [
                    httpx.RemoteProtocolError(
                        "Server disconnected without sending a response.",
                        request=httpx.Request("POST", endpoint),
                    ),
                    _response("POST", endpoint, 200, {"data": [{"b64_json": PNG_BASE64}]}),
                ]
            )

            with self.assertRaises(AICostImageProtocolError):
                await self._edit(client, [reference])

        self.assertEqual(len(client.calls), 1)
        self.assertEqual([item["field"] for item in client.calls[0]["files"]], ["image[]"])


class AICostImagePollingTests(AICostImageTestCase):
    async def test_202_task_id_polls_until_b64_success(self):
        submit_url = f"{self.base_url}/images/generations"
        task_url = f"{submit_url}/task-202"
        submitted = {"task_id": "task-202", "status": "pending"}
        completed = {"task_id": "task-202", "status": "completed", "data": [{"b64_json": PNG_BASE64}]}
        client = _RecordingClient(
            [
                _response("POST", submit_url, 202, submitted),
                _response("GET", task_url, 200, {"task_id": "task-202", "status": "processing"}),
                _response("GET", task_url, 200, completed),
            ]
        )

        with patch("plugins.image_plugins.aicost.asyncio.sleep", new=AsyncMock()) as sleep:
            (image, raw), _ = await self._generate(
                {"model": "gpt-image-2", "prompt": "异步", "size": "1024x1024"},
                client,
            )

        self.assertEqual(image["value"], PNG_BASE64)
        self.assertEqual(raw, completed)
        self.assertEqual([call["method"] for call in client.calls], ["POST", "GET", "GET"])
        self.assertEqual([call["url"] for call in client.calls[1:]], [task_url, task_url])
        self.assertTrue(sleep.await_count >= 1)

    async def test_200_pending_with_task_id_polls_until_url_success(self):
        submit_url = f"{self.base_url}/images/generations"
        task_url = f"{submit_url}/task-200"
        completed = {
            "task_id": "task-200",
            "status": "succeeded",
            "data": [{"url": "https://cdn.example.com/async.png"}],
        }
        client = _RecordingClient(
            [
                _response("POST", submit_url, 200, {"task_id": "task-200", "status": "pending"}),
                _response("GET", task_url, 200, completed),
            ]
        )

        with patch("plugins.image_plugins.aicost.asyncio.sleep", new=AsyncMock()):
            (image, raw), _ = await self._generate(
                {"model": "gpt-image-2", "prompt": "异步 URL", "size": "1024x1024"},
                client,
            )

        self.assertEqual(image, {"type": "url", "value": "https://cdn.example.com/async.png"})
        self.assertEqual(raw, completed)

    async def test_failed_poll_statuses_preserve_task_id(self):
        submit_url = f"{self.base_url}/images/generations"
        for status in ("failed", "error", "cancelled", "canceled", "rejected", "content_filter"):
            task_id = f"task-{status}"
            task_url = f"{submit_url}/{task_id}"
            client = _RecordingClient(
                [
                    _response("POST", submit_url, 202, {"task_id": task_id, "status": "pending"}),
                    _response(
                        "GET",
                        task_url,
                        200,
                        {"task_id": task_id, "status": status, "error": {"message": "upstream rejected"}},
                    ),
                ]
            )
            with self.subTest(status=status):
                with patch("plugins.image_plugins.aicost.asyncio.sleep", new=AsyncMock()):
                    with self.assertRaises(AICostImageProtocolError) as captured:
                        await self._generate(
                            {"model": "gpt-image-2", "prompt": "失败", "size": "1024x1024"},
                            client,
                        )
                self.assertEqual(captured.exception.status_code, 502)
                self.assertEqual(captured.exception.upstream_task_id, task_id)
                self.assertIn("upstream rejected", captured.exception.detail)

    async def test_poll_timeout_preserves_task_id(self):
        submit_url = f"{self.base_url}/images/generations"
        task_id = "task-timeout"
        client = _RecordingClient(
            [_response("POST", submit_url, 202, {"task_id": task_id, "status": "pending"})]
        )

        with patch("plugins.image_plugins.aicost.time.monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaises(AICostImageProtocolError) as captured:
                await self._generate(
                    {"model": "gpt-image-2", "prompt": "超时", "size": "1024x1024"},
                    client,
                    poll_timeout=1,
                )

        self.assertEqual(captured.exception.status_code, 504)
        self.assertEqual(captured.exception.upstream_task_id, task_id)


class AICostGeminiImageTests(AICostImageTestCase):
    async def test_gemini_generation_maps_pixel_size_and_returns_inline_data(self):
        model = "gemini-3-pro-image-preview"
        endpoint = f"https://www.aicost.xyz/v1beta/models/{model}:generateContent"
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"inlineData": {"mimeType": "image/png", "data": PNG_BASE64}}
                        ]
                    }
                }
            ]
        }
        client = _RecordingClient([_response("POST", endpoint, 200, payload)])

        (image, raw), _ = await self._generate(
            {"model": model, "prompt": "产品海报", "size": "3072x1728"},
            client,
        )

        self.assertEqual(image["type"], "b64")
        self.assertEqual(image["value"], PNG_BASE64)
        self.assertEqual(image["mime_type"], "image/png")
        self.assertEqual(raw, payload)
        self.assertEqual(client.calls[0]["url"], endpoint)
        self.assertEqual(
            client.calls[0]["json"],
            {
                "contents": [{"role": "user", "parts": [{"text": "产品海报"}]}],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"],
                    "imageConfig": {"imageSize": "2K", "aspectRatio": "16:9"},
                },
            },
        )

    async def test_gemini_reference_image_uses_inline_data(self):
        model = "gemini-3.1-flash-image-preview"
        endpoint = f"https://www.aicost.xyz/v1beta/models/{model}:generateContent"
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"inlineData": {"mimeType": "image/png", "data": PNG_BASE64}}
                        ]
                    }
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = os.path.join(temp_dir, "reference.jpg")
            with open(reference, "wb") as stream:
                stream.write(b"gemini-reference")
            encoded_reference = base64.b64encode(b"gemini-reference").decode("ascii")
            client = _RecordingClient([_response("POST", endpoint, 200, payload)])

            await self._generate(
                {
                    "model": model,
                    "prompt": "保持人物一致",
                    "size": "1728x3072",
                    "reference_images": [{"url": reference}],
                },
                client,
            )

        parts = client.calls[0]["json"]["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": "保持人物一致"})
        self.assertEqual(
            parts[1],
            {"inlineData": {"mimeType": "image/jpeg", "data": encoded_reference}},
        )
        self.assertEqual(
            client.calls[0]["json"]["generationConfig"]["imageConfig"],
            {"imageSize": "2K", "aspectRatio": "9:16"},
        )


if __name__ == "__main__":
    unittest.main()
