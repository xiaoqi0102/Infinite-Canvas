import os
import unittest
from unittest.mock import AsyncMock, patch

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


if __name__ == "__main__":
    unittest.main()
