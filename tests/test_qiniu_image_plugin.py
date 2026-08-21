import unittest
from unittest.mock import AsyncMock, patch

import httpx

from plugins.image_plugins.qiniu import (
    QiniuImageProtocolError,
    generate_qiniu_image,
    is_qiniu_image_official_provider,
    query_qiniu_image_task,
)


AUTH_HEADERS = {"Authorization": "Bearer test-key"}


def _response(method, url, status_code, payload):
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request(method, url),
    )


class _RecordingClient:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append({
            "method": str(method).upper(),
            "url": url,
            "headers": dict(kwargs.get("headers") or {}),
            "json": kwargs.get("json"),
        })
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


class QiniuImageTestCase(unittest.IsolatedAsyncioTestCase):
    base_url = "https://api.qnaigc.com/v1"

    async def _generate(self, request, client, **overrides):
        kwargs = {
            "base_url": self.base_url,
            "headers": AUTH_HEADERS,
            "request_timeout": 30,
            "poll_timeout": 30,
            "poll_interval": 0.5,
        }
        kwargs.update(overrides)
        with patch(
            "plugins.image_plugins.qiniu.httpx.AsyncClient",
            return_value=_AsyncClientContext(client),
        ):
            return await generate_qiniu_image(request, **kwargs)


class QiniuImageUrlTests(QiniuImageTestCase):
    def test_official_provider_detection_uses_exact_hostname(self):
        self.assertTrue(is_qiniu_image_official_provider({"base_url": "https://api.qnaigc.com"}))
        self.assertTrue(is_qiniu_image_official_provider({"base_url": "https://api.modelink.ai/v1"}))
        self.assertFalse(is_qiniu_image_official_provider({"base_url": "https://api.qnaigc.com.evil.test"}))

    async def test_base_url_strips_repeated_v1_and_uses_key_auth(self):
        submit = "https://api.qnaigc.com/queue/openai/gpt-image-2"
        status = f"{submit}/requests/request-1/status"
        result = f"{submit}/requests/request-1"
        client = _RecordingClient([
            _response("POST", submit, 200, {"status": "IN_QUEUE", "request_id": "request-1"}),
            _response("GET", status, 200, {"status": "COMPLETED", "request_id": "request-1"}),
            _response("GET", result, 200, {"images": [{"url": "https://cdn.example.com/image.png"}]}),
        ])

        image, _ = await self._generate(
            {"model": "gpt-image-2", "prompt": "测试", "size": "1024x1024"},
            client,
            base_url="https://api.qnaigc.com/v1/v1/",
        )

        self.assertEqual(image, {"type": "url", "value": "https://cdn.example.com/image.png"})
        self.assertEqual(client.calls[0]["headers"]["Authorization"], "Key test-key")
        self.assertEqual(client.calls[0]["url"], submit)


class QiniuImagePayloadTests(QiniuImageTestCase):
    async def test_gpt_generation_maps_known_size_to_fal_preset(self):
        submit = "https://api.qnaigc.com/queue/openai/gpt-image-2"
        status = f"{submit}/requests/gpt-1/status"
        client = _RecordingClient([
            _response("POST", submit, 200, {"status": "IN_QUEUE", "request_id": "gpt-1"}),
            _response(
                "GET",
                status,
                200,
                {"status": "COMPLETED", "request_id": "gpt-1", "result": {"images": [{"url": "https://cdn.example.com/gpt.png"}]}},
            ),
        ])

        await self._generate(
            {"model": "gpt-image-2", "prompt": "海报", "size": "1536x864", "quality": "medium"},
            client,
        )

        self.assertEqual(client.calls[0]["json"], {
            "prompt": "海报",
            "output_format": "png",
            "image_size": "landscape_16_9",
            "quality": "medium",
            "num_images": 1,
        })

    async def test_gpt_two_k_and_four_k_use_explicit_dimensions(self):
        for size, expected in (
            ("2048x2048", {"width": 2048, "height": 2048}),
            ("3840x2160", {"width": 3840, "height": 2160}),
            ("4096x4096", {"width": 2880, "height": 2880}),
        ):
            submit = "https://api.qnaigc.com/queue/openai/gpt-image-2"
            status = f"{submit}/requests/gpt-size/status"
            client = _RecordingClient([
                _response("POST", submit, 200, {"status": "IN_QUEUE", "request_id": "gpt-size"}),
                _response(
                    "GET",
                    status,
                    200,
                    {"status": "COMPLETED", "result": {"images": [{"url": "https://cdn.example.com/gpt.png"}]}},
                ),
            ])

            with self.subTest(size=size):
                await self._generate(
                    {"model": "gpt-image-2", "prompt": "海报", "size": size},
                    client,
                )
                self.assertEqual(client.calls[0]["json"]["image_size"], expected)

    async def test_gemini_generation_maps_size_to_ratio_and_resolution(self):
        submit = "https://api.qnaigc.com/queue/fal-ai/gemini-3-pro-image-preview"
        status = f"{submit}/requests/gemini-1/status"
        client = _RecordingClient([
            _response("POST", submit, 200, {"status": "IN_QUEUE", "request_id": "gemini-1"}),
            _response(
                "GET",
                status,
                200,
                {"status": "COMPLETED", "result": {"images": [{"url": "https://cdn.example.com/gemini.png"}]}},
            ),
        ])

        await self._generate(
            {"model": "gemini-3-pro-image-preview", "prompt": "产品图", "size": "3072x1728"},
            client,
        )

        self.assertEqual(client.calls[0]["json"], {
            "prompt": "产品图",
            "output_format": "png",
            "aspect_ratio": "16:9",
            "resolution": "2K",
        })

    async def test_gemini_four_k_size_maps_to_four_k_resolution(self):
        submit = "https://api.qnaigc.com/queue/fal-ai/gemini-3.1-flash-image-preview"
        status = f"{submit}/requests/gemini-4k/status"
        client = _RecordingClient([
            _response("POST", submit, 200, {"status": "IN_QUEUE", "request_id": "gemini-4k"}),
            _response(
                "GET",
                status,
                200,
                {"status": "COMPLETED", "result": {"images": [{"url": "https://cdn.example.com/4k.png"}]}},
            ),
        ])

        await self._generate(
            {"model": "gemini-3.1-flash-image-preview", "prompt": "4K", "size": "3840x2160"},
            client,
        )

        self.assertEqual(client.calls[0]["json"]["resolution"], "4K")

    async def test_gemini_explicit_resolution_overrides_size_heuristic(self):
        submit = "https://api.qnaigc.com/queue/fal-ai/gemini-3-pro-image-preview"
        status = f"{submit}/requests/gemini-explicit/status"
        client = _RecordingClient([
            _response("POST", submit, 200, {"status": "IN_QUEUE", "request_id": "gemini-explicit"}),
            _response(
                "GET",
                status,
                200,
                {"status": "COMPLETED", "result": {"images": [{"url": "https://cdn.example.com/explicit.png"}]}},
            ),
        ])

        await self._generate(
            {
                "model": "gemini-3-pro-image-preview",
                "prompt": "显式 4K",
                "size": "1024x1024",
                "resolution": "4k",
            },
            client,
        )

        self.assertEqual(client.calls[0]["json"]["resolution"], "4K")

    async def test_gemini_two_k_canvas_preset_maps_to_two_k_resolution(self):
        submit = "https://api.qnaigc.com/queue/fal-ai/gemini-3.1-flash-image-preview"
        status = f"{submit}/requests/gemini-2k/status"
        client = _RecordingClient([
            _response("POST", submit, 200, {"status": "IN_QUEUE", "request_id": "gemini-2k"}),
            _response(
                "GET",
                status,
                200,
                {"status": "COMPLETED", "result": {"images": [{"url": "https://cdn.example.com/2k.png"}]}},
            ),
        ])

        await self._generate(
            {"model": "gemini-3.1-flash-image-preview", "prompt": "2K", "size": "2048x1152"},
            client,
        )

        self.assertEqual(client.calls[0]["json"]["resolution"], "2K")

    async def test_reference_images_use_edit_endpoint_and_public_urls(self):
        submit = "https://api.qnaigc.com/queue/openai/gpt-image-2/edit"
        status = "https://api.qnaigc.com/queue/openai/gpt-image-2/requests/edit-1/status"
        client = _RecordingClient([
            _response("POST", submit, 200, {"status": "IN_QUEUE", "request_id": "edit-1"}),
            _response(
                "GET",
                status,
                200,
                {"status": "COMPLETED", "result": {"images": [{"url": "https://cdn.example.com/edit.png"}]}},
            ),
        ])

        await self._generate(
            {
                "model": "gpt-image-2",
                "prompt": "修改背景",
                "size": "1024x1024",
                "reference_images": [
                    {"url": "https://cdn.example.com/ref.png"},
                    {"url": "https://cdn.example.com/mask.png", "role": "mask"},
                ],
            },
            client,
        )

        self.assertEqual(client.calls[0]["url"], submit)
        self.assertEqual(client.calls[0]["json"]["image_urls"], ["https://cdn.example.com/ref.png"])
        self.assertEqual(client.calls[0]["json"]["mask_url"], "https://cdn.example.com/mask.png")

    async def test_local_reference_url_is_rejected_by_plugin(self):
        client = _RecordingClient([])
        with self.assertRaises(QiniuImageProtocolError) as captured:
            await self._generate(
                {
                    "model": "gpt-image-2",
                    "prompt": "修改",
                    "size": "1024x1024",
                    "reference_images": [{"url": "/assets/local.png"}],
                },
                client,
            )
        self.assertEqual(captured.exception.status_code, 400)
        self.assertIn("公网", captured.exception.detail)


class QiniuImagePollingTests(QiniuImageTestCase):
    async def test_polling_uses_status_then_result_endpoint(self):
        route = "https://api.qnaigc.com/queue/fal-ai/gemini-3.1-flash-image-preview"
        status = f"{route}/requests/request%2Fid/status"
        result = f"{route}/requests/request%2Fid"
        client = _RecordingClient([
            _response("GET", status, 200, {"status": "COMPLETED", "request_id": "request/id"}),
            _response("GET", result, 200, {"images": [{"url": "https://cdn.example.com/final.webp"}]}),
        ])
        with patch(
            "plugins.image_plugins.qiniu.httpx.AsyncClient",
            return_value=_AsyncClientContext(client),
        ):
            raw = await query_qiniu_image_task(
                "request/id",
                model="gemini-3.1-flash-image-preview",
                base_url=self.base_url,
                headers=AUTH_HEADERS,
                request_timeout=30,
            )

        self.assertEqual(raw["images"][0]["url"], "https://cdn.example.com/final.webp")
        self.assertEqual([call["url"] for call in client.calls], [status, result])

    async def test_failed_status_preserves_request_id(self):
        route = "https://api.qnaigc.com/queue/openai/gpt-image-2"
        status = f"{route}/requests/failed-1/status"
        client = _RecordingClient([
            _response("POST", route, 200, {"status": "IN_QUEUE", "request_id": "failed-1"}),
            _response(
                "GET",
                status,
                200,
                {"status": "FAILED", "request_id": "failed-1", "detail": {"msg": "content rejected"}},
            ),
        ])
        with patch("plugins.image_plugins.qiniu.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(QiniuImageProtocolError) as captured:
                await self._generate(
                    {"model": "gpt-image-2", "prompt": "失败", "size": "1024x1024"},
                    client,
                )
        self.assertEqual(captured.exception.upstream_task_id, "failed-1")
        self.assertIn("content rejected", captured.exception.detail)


if __name__ == "__main__":
    unittest.main()
