import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("INFINITE_CANVAS_SKIP_STATIC_SYNC", "1")

import main
from plugins.video_plugins import common


class _ProbeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {"content-type": "image/png"}
        self.is_redirect = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ProbeClient:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _ProbeResponse(405 if method == "HEAD" else 206)


class PublicHttpProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_head_not_supported_falls_back_to_range_get(self):
        client = _ProbeClient()
        public = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with (
            patch("plugins.video_plugins.common.socket.getaddrinfo", return_value=public),
            patch("plugins.video_plugins.common.httpx.AsyncClient", return_value=client),
        ):
            result = await common.public_http_probe("https://files.example/material.png")

        self.assertEqual(result["status_code"], 206)
        self.assertEqual([call[0] for call in client.calls], ["HEAD", "GET"])
        self.assertEqual(client.calls[1][2]["headers"]["Range"], "bytes=0-0")
        self.assertEqual(client.calls[1][2]["headers"]["Host"], "files.example")
        self.assertEqual(
            client.calls[1][2]["extensions"]["sni_hostname"],
            "files.example",
        )


class VideoMaterialPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_materials_are_uploaded_before_video_submission(self):
        cases = (
            ("image", "/api/storage-files/upload/reference.png", "https://files.example/reference.png"),
            ("video", "/api/storage-files/generated/reference.mp4", "https://files.example/reference.mp4"),
            ("audio", "/api/storage-files/local/reference.mp3", "https://files.example/reference.mp3"),
        )
        for kind, local_url, public_url in cases:
            with self.subTest(kind=kind):
                material = main.CanvasVideoMaterialPreflightItem(
                    url=local_url,
                    source_url=local_url,
                    kind=kind,
                )
                upload = AsyncMock(return_value={
                    "url": public_url,
                    "source": local_url,
                    "service": "litterbox",
                })
                with (
                    patch("main.upload_local_video_to_cloud", new=upload),
                    patch("main.public_http_probe", new=AsyncMock(return_value={"status_code": 200})),
                ):
                    result = await main.preflight_canvas_video_material(material)

                self.assertTrue(result["refreshed"])
                self.assertEqual(result["url"], public_url)
                self.assertEqual(result["kind"], kind)
                upload.assert_awaited_once_with(local_url, "auto")

    async def test_valid_public_material_keeps_existing_url(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="https://files.example/material.png",
            source_url="/assets/material.png",
            kind="image",
        )
        with (
            patch("main.public_http_probe", new=AsyncMock(return_value={"status_code": 200})),
            patch("main.upload_local_video_to_cloud", new=AsyncMock()) as upload,
        ):
            result = await main.preflight_canvas_video_material(material)

        self.assertFalse(result["refreshed"])
        self.assertEqual(result["url"], material.url)
        upload.assert_not_awaited()

    async def test_expired_material_is_reuploaded_from_local_copy(self):
        old_url = "https://files.example/expired.png"
        new_url = "https://files.example/refreshed.png"
        material = main.CanvasVideoMaterialPreflightItem(
            url=old_url,
            source_url="/assets/material.png",
            kind="image",
        )
        probe = AsyncMock(side_effect=[
            {"status_code": 404},
            {"status_code": 200},
        ])
        upload = AsyncMock(return_value={
            "url": new_url,
            "source": material.source_url,
            "service": "sudashui",
        })
        with (
            patch("main.public_http_probe", new=probe),
            patch("main.upload_local_video_to_cloud", new=upload),
        ):
            result = await main.preflight_canvas_video_material(material)

        self.assertTrue(result["refreshed"])
        self.assertEqual(result["url"], new_url)
        upload.assert_awaited_once_with(material.source_url, "auto")
        self.assertEqual(probe.await_count, 2)

    async def test_expired_material_without_local_copy_stops_submission(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="https://files.example/expired.png",
            kind="image",
        )
        with patch(
            "main.public_http_probe",
            new=AsyncMock(return_value={"status_code": 404}),
        ):
            with self.assertRaises(main.HTTPException) as caught:
                await main.preflight_canvas_video_material(material)

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(
            caught.exception.detail["code"],
            "material_local_source_missing",
        )

    async def test_trusted_asset_uri_skips_public_probe(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="asset://asset-123",
            kind="image",
        )
        with patch("main.public_http_probe", new=AsyncMock()) as probe:
            result = await main.preflight_canvas_video_material(material)

        self.assertEqual(result["service"], "asset")
        self.assertFalse(result["refreshed"])
        probe.assert_not_awaited()

    async def test_request_preflight_normalizes_all_material_kinds(self):
        payload = main.CanvasVideoRequest(
            prompt="test",
            images=[main.AIReference(
                url="/api/storage-files/upload/reference.png",
                name="reference.png",
            )],
            videos=["/api/storage-files/generated/reference.mp4"],
            audios=["/api/storage-files/local/reference.mp3"],
        )
        public_urls = [
            "https://files.example/reference.png",
            "https://files.example/reference.mp4",
            "https://files.example/reference.mp3",
        ]
        prepared = [
            {
                "url": url,
                "source": material.url,
                "kind": material.kind,
                "refreshed": True,
            }
            for material, url in zip(
                [
                    main.CanvasVideoMaterialPreflightItem(
                        url="/api/storage-files/upload/reference.png",
                        kind="image",
                    ),
                    main.CanvasVideoMaterialPreflightItem(
                        url="/api/storage-files/generated/reference.mp4",
                        kind="video",
                    ),
                    main.CanvasVideoMaterialPreflightItem(
                        url="/api/storage-files/local/reference.mp3",
                        kind="audio",
                    ),
                ],
                public_urls,
            )
        ]
        with patch(
            "main.preflight_canvas_video_materials",
            new=AsyncMock(return_value=prepared),
        ) as preflight:
            result = await main.preflight_canvas_video_request(payload)

        self.assertEqual(result.images[0].url, public_urls[0])
        self.assertEqual(
            result.images[0].originalLocalUrl,
            "/api/storage-files/upload/reference.png",
        )
        self.assertEqual(result.videos, [public_urls[1]])
        self.assertEqual(result.audios, [public_urls[2]])
        self.assertEqual(result.images[0].name, "reference.png")
        materials = preflight.await_args.args[0]
        self.assertEqual([item.kind for item in materials], ["image", "video", "audio"])

    async def test_sync_endpoint_preflights_before_upstream_submission(self):
        payload = main.CanvasVideoRequest(prompt="test")
        failure = main.canvas_video_material_error(
            "素材不可用",
            "Material unavailable",
        )
        with (
            patch(
                "main.preflight_canvas_video_request",
                new=AsyncMock(side_effect=failure),
            ),
            patch("main.build_canvas_video_result", new=AsyncMock()) as build,
        ):
            with self.assertRaises(main.HTTPException):
                await main.canvas_video(payload)

        build.assert_not_awaited()

    async def test_task_endpoint_preflights_before_task_persistence(self):
        payload = main.CanvasVideoRequest(prompt="test")
        failure = main.canvas_video_material_error(
            "素材不可用",
            "Material unavailable",
        )
        task_ids_before = set(main.CANVAS_TASKS)
        with (
            patch(
                "main.preflight_canvas_video_request",
                new=AsyncMock(side_effect=failure),
            ),
            patch("main.persist_canvas_video_tasks") as persist,
            patch("main.asyncio.create_task") as create_task,
        ):
            with self.assertRaises(main.HTTPException):
                await main.create_canvas_video_task(payload)

        self.assertEqual(set(main.CANVAS_TASKS), task_ids_before)
        persist.assert_not_called()
        create_task.assert_not_called()

    async def test_local_material_reuses_persisted_upload_cache(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="/assets/reference.png",
            source_url="/assets/reference.png",
            kind="image",
        )
        uploaded_url = "https://files.example/reference.png"
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = os.path.join(temp_dir, "reference.png")
            cache_path = os.path.join(temp_dir, "canvas_video_upload_cache.json")
            with open(media_path, "wb") as handle:
                handle.write(b"image")
            upload = AsyncMock(return_value={
                "url": uploaded_url,
                "source": material.source_url,
                "service": "litterbox",
                "expires": "72h",
            })
            with (
                patch("main.CANVAS_VIDEO_UPLOAD_CACHE_FILE", cache_path),
                patch("main.local_media_path_for_cloud_upload", return_value=media_path),
                patch("main.upload_local_video_to_cloud", new=upload),
                patch(
                    "main.public_http_probe",
                    new=AsyncMock(return_value={"status_code": 200}),
                ),
            ):
                first = await main.preflight_canvas_video_material(material)
                second = await main.preflight_canvas_video_material(material)

        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["url"], uploaded_url)
        upload.assert_awaited_once_with(material.source_url, "auto")

    async def test_local_file_change_invalidates_upload_cache(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="/assets/reference.png",
            source_url="/assets/reference.png",
            kind="image",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = os.path.join(temp_dir, "reference.png")
            cache_path = os.path.join(temp_dir, "canvas_video_upload_cache.json")
            with open(media_path, "wb") as handle:
                handle.write(b"first")
            upload = AsyncMock(side_effect=[
                {
                    "url": "https://files.example/first.png",
                    "service": "litterbox",
                    "expires": "72h",
                },
                {
                    "url": "https://files.example/second.png",
                    "service": "litterbox",
                    "expires": "72h",
                },
            ])
            with (
                patch("main.CANVAS_VIDEO_UPLOAD_CACHE_FILE", cache_path),
                patch("main.local_media_path_for_cloud_upload", return_value=media_path),
                patch("main.upload_local_video_to_cloud", new=upload),
                patch(
                    "main.public_http_probe",
                    new=AsyncMock(return_value={"status_code": 200}),
                ),
            ):
                first = await main.preflight_canvas_video_material(material)
                with open(media_path, "ab") as handle:
                    handle.write(b"-changed")
                second = await main.preflight_canvas_video_material(material)

        self.assertEqual(first["url"], "https://files.example/first.png")
        self.assertEqual(second["url"], "https://files.example/second.png")
        self.assertEqual(upload.await_count, 2)

    async def test_unreachable_cached_url_is_reuploaded(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="/assets/reference.png",
            source_url="/assets/reference.png",
            kind="image",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = os.path.join(temp_dir, "reference.png")
            cache_path = os.path.join(temp_dir, "canvas_video_upload_cache.json")
            with open(media_path, "wb") as handle:
                handle.write(b"image")
            upload = AsyncMock(side_effect=[
                {
                    "url": "https://files.example/expired.png",
                    "service": "litterbox",
                    "expires": "72h",
                },
                {
                    "url": "https://files.example/refreshed.png",
                    "service": "litterbox",
                    "expires": "72h",
                },
            ])
            probe = AsyncMock(side_effect=[
                {"status_code": 200},
                {"status_code": 404},
                {"status_code": 200},
            ])
            with (
                patch("main.CANVAS_VIDEO_UPLOAD_CACHE_FILE", cache_path),
                patch("main.local_media_path_for_cloud_upload", return_value=media_path),
                patch("main.upload_local_video_to_cloud", new=upload),
                patch("main.public_http_probe", new=probe),
            ):
                await main.preflight_canvas_video_material(material)
                result = await main.preflight_canvas_video_material(material)

        self.assertEqual(result["url"], "https://files.example/refreshed.png")
        self.assertEqual(upload.await_count, 2)

    async def test_batch_preflight_limits_public_probe_concurrency(self):
        materials = [
            main.CanvasVideoMaterialPreflightItem(
                url=f"https://files.example/reference-{index}.png",
                kind="image",
            )
            for index in range(8)
        ]
        active = 0
        max_active = 0

        async def probe(_url):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"status_code": 200}

        with (
            patch("main.VIDEO_MATERIAL_PROBE_CONCURRENCY", 3),
            patch("main.public_http_probe", new=probe),
        ):
            result = await main.preflight_canvas_video_materials(materials)

        self.assertEqual(len(result), len(materials))
        self.assertGreater(max_active, 1)
        self.assertLessEqual(max_active, 3)

    async def test_batch_preflight_deduplicates_and_preserves_order(self):
        first = main.CanvasVideoMaterialPreflightItem(
            url="https://files.example/first.png",
            kind="image",
        )
        second = main.CanvasVideoMaterialPreflightItem(
            url="https://files.example/second.png",
            kind="image",
        )
        probe = AsyncMock(return_value={"status_code": 200})
        with patch("main.public_http_probe", new=probe):
            result = await main.preflight_canvas_video_materials([
                first,
                second,
                first,
            ])

        self.assertEqual(
            [item["url"] for item in result],
            [first.url, second.url, first.url],
        )
        self.assertEqual(probe.await_count, 2)

    async def test_batch_preflight_limits_upload_concurrency(self):
        materials = [
            main.CanvasVideoMaterialPreflightItem(
                url=f"/assets/reference-{index}.png",
                source_url=f"/assets/reference-{index}.png",
                kind="image",
            )
            for index in range(6)
        ]
        active = 0
        max_active = 0

        async def upload(source_url, _service):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {
                "url": f"https://files.example/{os.path.basename(source_url)}",
                "source": source_url,
                "service": "litterbox",
            }

        with (
            patch("main.VIDEO_MATERIAL_UPLOAD_CONCURRENCY", 2),
            patch("main.cached_canvas_video_upload", return_value=None),
            patch(
                "main.remember_canvas_video_upload",
                side_effect=lambda source_url, _service, uploaded: uploaded,
            ),
            patch("main.upload_local_video_to_cloud", new=upload),
            patch(
                "main.public_http_probe",
                new=AsyncMock(return_value={"status_code": 200}),
            ),
        ):
            result = await main.preflight_canvas_video_materials(materials)

        self.assertEqual(len(result), len(materials))
        self.assertGreater(max_active, 1)
        self.assertLessEqual(max_active, 2)

    async def test_public_material_rejects_mismatched_content_type(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="https://files.example/material.png",
            kind="image",
        )
        with patch(
            "main.public_http_probe",
            new=AsyncMock(return_value={
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
            }),
        ):
            with self.assertRaises(main.HTTPException) as caught:
                await main.preflight_canvas_video_material(material)

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(
            caught.exception.detail["code"],
            "material_content_type_mismatch",
        )
        self.assertIn("text/html", caught.exception.detail["message"])

    async def test_public_material_accepts_generic_binary_content_type(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="https://files.example/material.mp4",
            kind="video",
        )
        with patch(
            "main.public_http_probe",
            new=AsyncMock(return_value={
                "status_code": 200,
                "content_type": "application/octet-stream",
            }),
        ):
            result = await main.preflight_canvas_video_material(material)

        self.assertEqual(result["url"], material.url)
        self.assertFalse(result["refreshed"])

    async def test_mismatched_public_material_reuploads_local_copy(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="https://files.example/material.mp4",
            source_url="/assets/material.mp4",
            kind="video",
        )
        upload = AsyncMock(return_value={
            "url": "https://files.example/refreshed.mp4",
            "service": "litterbox",
        })
        probe = AsyncMock(side_effect=[
            {"status_code": 200, "content_type": "image/png"},
            {"status_code": 200, "content_type": "video/mp4"},
        ])
        with (
            patch("main.cached_canvas_video_upload", return_value=None),
            patch("main.upload_local_video_to_cloud", new=upload),
            patch(
                "main.remember_canvas_video_upload",
                side_effect=lambda source_url, _service, uploaded: uploaded,
            ),
            patch("main.public_http_probe", new=probe),
        ):
            result = await main.preflight_canvas_video_material(material)

        self.assertTrue(result["refreshed"])
        self.assertEqual(result["url"], "https://files.example/refreshed.mp4")
        upload.assert_awaited_once_with(material.source_url, "auto")

    async def test_uploaded_material_rejects_mismatched_content_type(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="/assets/material.mp3",
            source_url="/assets/material.mp3",
            kind="audio",
        )
        upload = AsyncMock(return_value={
            "url": "https://files.example/material.mp3",
            "service": "litterbox",
        })
        with (
            patch("main.cached_canvas_video_upload", return_value=None),
            patch("main.upload_local_video_to_cloud", new=upload),
            patch(
                "main.public_http_probe",
                new=AsyncMock(return_value={
                    "status_code": 200,
                    "content_type": "application/json",
                }),
            ),
        ):
            with self.assertRaises(main.HTTPException) as caught:
                await main.preflight_canvas_video_material(material)

        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(
            caught.exception.detail["code"],
            "material_content_type_mismatch",
        )

    async def test_mismatched_cached_content_type_forces_new_upload(self):
        material = main.CanvasVideoMaterialPreflightItem(
            url="/assets/material.mp4",
            source_url="/assets/material.mp4",
            kind="video",
        )
        upload = AsyncMock(return_value={
            "url": "https://files.example/refreshed.mp4",
            "service": "litterbox",
        })
        forget = Mock()
        probe = AsyncMock(side_effect=[
            {"status_code": 200, "content_type": "text/html"},
            {"status_code": 200, "content_type": "video/mp4"},
        ])
        with (
            patch("main.cached_canvas_video_upload", return_value={
                "url": "https://files.example/cached.mp4",
                "cache_hit": True,
                "cache_key": "cached-key",
            }),
            patch("main.forget_canvas_video_upload", new=forget),
            patch("main.upload_local_video_to_cloud", new=upload),
            patch(
                "main.remember_canvas_video_upload",
                side_effect=lambda source_url, _service, uploaded: uploaded,
            ),
            patch("main.public_http_probe", new=probe),
        ):
            result = await main.preflight_canvas_video_material(material)

        self.assertEqual(result["url"], "https://files.example/refreshed.mp4")
        forget.assert_called_once_with("cached-key")
        upload.assert_awaited_once_with(material.source_url, "auto")


if __name__ == "__main__":
    unittest.main()
