import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("INFINITE_CANVAS_SKIP_STATIC_SYNC", "1")

import main


class TudouImageSupportTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_ai_image_preserves_upstream_tudou_arguments(self):
        provider = {
            "id": "tudou",
            "name": "土豆API",
            "base_url": "https://api.ai-tudou.net",
            "protocol": "openai",
            "image_request_mode": "tudou-async",
        }
        generated = AsyncMock(return_value=({"type": "url", "value": "https://example.com/image.png"}, {}))

        with (
            patch.object(main, "get_api_provider", return_value=provider),
            patch.object(main, "generate_tudou_async_image", new=generated),
        ):
            await main.generate_ai_image(
                "测试",
                "2048x1152",
                "high",
                "gpt-image-2",
                [],
                "tudou",
                "16:9",
                "2k",
            )

        generated.assert_awaited_once_with(
            "测试",
            "2048x1152",
            "high",
            "gpt-image-2-1k",
            [],
            provider,
            "16:9",
            "2k",
        )

    async def test_tudou_rejects_unreadable_reference_instead_of_submitting_edit(self):
        provider = {
            "id": "tudou",
            "name": "土豆API",
            "base_url": "https://api.ai-tudou.net",
            "protocol": "openai",
            "image_request_mode": "tudou-async",
        }

        with patch.object(main, "reference_to_data_url", return_value=""):
            with self.assertRaises(main.HTTPException) as captured:
                await main.generate_tudou_async_image(
                    "测试",
                    "1024x1024",
                    "medium",
                    "gpt-image-2-1k",
                    [{"url": "/api/storage-files/generated/missing.png", "kind": "image"}],
                    provider,
                )

        self.assertEqual(captured.exception.status_code, 400)
        self.assertIn("参考图无法读取", captured.exception.detail)

    def test_online_image_request_keeps_tudou_resolution_fields(self):
        payload = main.OnlineImageRequest(
            prompt="测试",
            aspect_ratio="16:9",
            resolution="4k",
            references=[main.AIReference(url="/api/storage-files/generated/reference.png", kind="image")],
        )

        self.assertEqual(payload.aspect_ratio, "16:9")
        self.assertEqual(payload.resolution, "4k")
        self.assertEqual(len(payload.references), 1)
        self.assertEqual(main.canvas_image_request_meta(payload)["reference_image_count"], 1)


if __name__ == "__main__":
    unittest.main()
