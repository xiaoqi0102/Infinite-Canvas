import unittest
from unittest.mock import AsyncMock, patch

import main


class QiniuImageIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_official_qiniu_hosts_and_aliases(self):
        self.assertEqual(main.detect_image_request_mode("https://api.qnaigc.com"), "qiniu-image")
        self.assertEqual(main.detect_image_request_mode("https://api.modelink.ai/v1"), "qiniu-image")
        self.assertEqual(main.normalize_image_request_mode("modelink"), "qiniu-image")

    async def test_generate_preflights_references_before_calling_plugin(self):
        provider = {
            "id": "qiniu-test",
            "name": "七牛测试",
            "base_url": "https://api.qnaigc.com",
            "image_request_mode": "qiniu-image",
        }
        refs = [{
            "url": "/assets/local-reference.png",
            "originalLocalUrl": "/assets/local-reference.png",
        }]
        prepared = [{
            **refs[0],
            "url": "https://temp.example.com/reference.png",
        }]
        with (
            patch.object(main, "get_api_provider", return_value=provider),
            patch.object(main, "api_headers", return_value={"Authorization": "Bearer test-key"}),
            patch.object(main, "prepare_qiniu_image_references", new=AsyncMock(return_value=prepared)) as preflight,
            patch.object(
                main,
                "generate_qiniu_image",
                new=AsyncMock(return_value=(
                    {"type": "url", "value": "https://cdn.example.com/result.png"},
                    {"images": [{"url": "https://cdn.example.com/result.png"}]},
                )),
            ) as generate,
        ):
            image, _ = await main.generate_ai_image(
                "修改背景",
                "2048x2048",
                "high",
                "gpt-image-2",
                refs,
                "qiniu-test",
                "1:1",
                "2k",
            )

        self.assertEqual(image["value"], "https://cdn.example.com/result.png")
        preflight.assert_awaited_once_with(refs)
        request = generate.await_args.args[0]
        self.assertEqual(request["reference_images"], prepared)
        self.assertEqual(request["size"], "2048x2048")
        self.assertEqual(request["aspect_ratio"], "1:1")
        self.assertEqual(request["resolution"], "2k")
        self.assertEqual(generate.await_args.kwargs["base_url"], "https://api.qnaigc.com")

    async def test_prepare_references_reuses_video_material_preflight(self):
        refs = [{
            "url": "/assets/reference.png",
            "originalLocalUrl": "/assets/reference.png",
            "role": "reference",
        }]
        prepared = [{
            "url": "https://temp.example.com/reference.png",
            "source": "/assets/reference.png",
            "kind": "image",
            "refreshed": True,
        }]
        with patch.object(
            main,
            "preflight_canvas_video_materials",
            new=AsyncMock(return_value=prepared),
        ) as preflight:
            result = await main.prepare_qiniu_image_references(refs)

        material = preflight.await_args.args[0][0]
        self.assertEqual(material.url, "/assets/reference.png")
        self.assertEqual(material.source_url, "/assets/reference.png")
        self.assertEqual(material.kind, "image")
        self.assertEqual(result[0]["url"], "https://temp.example.com/reference.png")
        self.assertEqual(result[0]["originalLocalUrl"], "/assets/reference.png")

    async def test_query_passes_model_to_fal_route_lookup(self):
        provider = {
            "id": "qiniu-test",
            "name": "七牛测试",
            "base_url": "https://api.qnaigc.com",
            "image_request_mode": "qiniu-image",
        }
        raw = {"status": "IN_PROGRESS", "request_id": "request-1"}
        with (
            patch.object(main, "get_api_provider", return_value=provider),
            patch.object(main, "api_headers", return_value={"Authorization": "Bearer test-key"}),
            patch.object(main, "query_qiniu_image_task", new=AsyncMock(return_value=raw)) as query,
        ):
            result = await main.query_image_task(main.ImageTaskQueryRequest(
                provider_id="qiniu-test",
                task_id="request-1",
                model="gemini-3.1-flash-image-preview",
            ))

        self.assertEqual(result["status"], "running")
        self.assertEqual(query.await_args.kwargs["model"], "gemini-3.1-flash-image-preview")


if __name__ == "__main__":
    unittest.main()
