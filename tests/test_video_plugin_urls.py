import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("INFINITE_CANVAS_SKIP_STATIC_SYNC", "1")

import main
from plugins.video_plugins import aicost, geeknow, megabyai, sudashui, tudou
from plugins.video_plugins.common import (
    canonical_video_api_root,
    is_public_http_url,
    public_http_get,
    resolve_video_download_url,
    video_http_preview_url,
)


class _Response:
    status_code = 200
    text = ""
    headers = {}

    def json(self):
        return {
            "id": "task/id",
            "status": "SUCCESS",
            "download_url": "/v1/videos/task%2Fid/content",
        }


class _RecordingClient:
    def __init__(self):
        self.posts = []
        self.post_requests = []
        self.gets = []

    async def post(self, url, **kwargs):
        self.posts.append(url)
        self.post_requests.append((url, kwargs))
        return _Response()

    async def get(self, url, **kwargs):
        self.gets.append(url)
        return _Response()


class _SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.gets = []

    async def get(self, url, **kwargs):
        self.gets.append(url)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _AsyncClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _PinnedResponse:
    is_redirect = False


class _PinnedClient:
    def __init__(self):
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return _PinnedResponse()


class VideoApiRootTests(unittest.TestCase):
    def test_canonical_root_matrix(self):
        cases = {
            "https://api.example.com": "https://api.example.com",
            "https://api.example.com/": "https://api.example.com",
            "https://api.example.com//v1": "https://api.example.com",
            "https://api.example.com/v1/v1": "https://api.example.com",
            "https://api.example.com/gateway//v2/v1/": "https://api.example.com/gateway",
            "https://api.example.com/gateway/v10": "https://api.example.com/gateway/v10",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(canonical_video_api_root(raw), expected)

    def test_main_create_and_query_paths_contain_one_v1(self):
        expected_paths = {
            main.AICOST_VIDEO_REQUEST_MODE: ("/v1/videos", "/v1/videos/task-id"),
            main.TUDOU_VIDEO_REQUEST_MODE: ("/v1/videos", "/v1/videos/task-id"),
            main.GEEKNOW_VIDEO_REQUEST_MODE: ("/v1/videos", "/v1/videos/task-id"),
            main.MEGABYAI_VIDEO_REQUEST_MODE: ("/v1/videos", "/v1/videos/task-id"),
            main.SUDASHUI_VIDEO_REQUEST_MODE: (
                "/v1/video/generations",
                "/v1/video/generations/task-id",
            ),
        }
        for raw in ("https://api.example.com/", "https://api.example.com//v1", "https://api.example.com/v1/v1"):
            for mode, (submit_path, task_path) in expected_paths.items():
                provider = {"base_url": raw, "video_request_mode": mode}
                root = main.video_api_root(provider)
                with self.subTest(raw=raw, mode=mode):
                    self.assertEqual(main.video_submit_url_candidates(provider, root)[0], f"https://api.example.com{submit_path}")
                    self.assertEqual(main.video_task_url_candidates(provider, root, "task-id")[0], f"https://api.example.com{task_path}")

    def test_plugin_roots_share_the_same_rules(self):
        root_functions = (
            aicost._provider_root,
            tudou._api_root,
            geeknow._provider_root,
            megabyai._provider_root,
            sudashui._provider_root,
        )
        for function in root_functions:
            with self.subTest(function=function.__module__):
                self.assertEqual(function("https://api.example.com//v1/v1/"), "https://api.example.com")

    def test_saved_value_is_idempotent_and_keeps_v1(self):
        raw = {
            "id": "test-provider",
            "name": "Test Provider",
            "base_url": " https://api.example.com/v1/// ",
        }
        first = main.normalize_provider(raw)
        second = main.normalize_provider(first)
        self.assertEqual(first["base_url"], "https://api.example.com/v1")
        self.assertEqual(second["base_url"], first["base_url"])

    def test_aicost_saved_values_are_idempotent(self):
        for raw_base_url, expected_base_url in (
            ("https://www.aicost.xyz", "https://www.aicost.xyz"),
            ("https://www.aicost.xyz/", "https://www.aicost.xyz"),
            ("https://www.aicost.xyz/v1", "https://www.aicost.xyz/v1"),
            ("https://www.aicost.xyz/v1/", "https://www.aicost.xyz/v1"),
        ):
            raw = {
                "id": "aicost-test",
                "name": "AICost",
                "base_url": raw_base_url,
                "video_request_mode": "openai-videos-generations",
            }
            first = main.normalize_provider(raw)
            second = main.normalize_provider(first)
            with self.subTest(base_url=raw_base_url):
                self.assertEqual(first["base_url"], expected_base_url)
                self.assertEqual(first["video_request_mode"], main.AICOST_VIDEO_REQUEST_MODE)
                self.assertEqual(second, first)

    def test_aicost_candidates_only_use_videos_api(self):
        provider = {
            "base_url": "https://www.aicost.xyz/v1/",
            "video_request_mode": main.AICOST_VIDEO_REQUEST_MODE,
        }
        root = main.video_api_root(provider)
        self.assertEqual(main.video_submit_url_candidates(provider, root), ["https://www.aicost.xyz/v1/videos"])
        self.assertEqual(
            main.video_task_url_candidates(provider, root, "task/id"),
            ["https://www.aicost.xyz/v1/videos/task%2Fid"],
        )

    def test_aicost_task_id_accepts_create_response_identifiers(self):
        self.assertEqual(aicost._task_id({"id": "bare-task"}), "bare-task")
        self.assertEqual(aicost._task_id({"request_id": "request-task"}), "request-task")
        self.assertEqual(aicost._task_id({"data": {"requestId": "nested-task"}}), "nested-task")
        self.assertEqual(aicost._task_id({"metadata": {"id": "not-a-task"}}), "")
        self.assertEqual(aicost._task_id({"metadata": {"request_id": "trace-id"}}), "")
        self.assertEqual(
            aicost._task_id({"request_id": "trace-id", "data": {"task_id": "real-task"}}),
            "real-task",
        )


    def test_legacy_megabyai_query_failure_is_resumable_without_business_failure(self):
        task = {
            "status": "failed",
            "provider_id": "megabyai-test",
            "upstream_task_id": "videos-mini_task-id",
            "error": "MegabyAI 视频任务查询失败：temporary gateway error",
            "raw_last": {"status": "in_progress", "progress": 30, "error": None},
        }
        provider = {
            "id": "megabyai-test",
            "base_url": "https://cn.megabyai.cc",
            "video_request_mode": main.MEGABYAI_VIDEO_REQUEST_MODE,
        }
        with patch.object(main, "get_api_provider_exact", return_value=provider):
            self.assertTrue(main.canvas_video_failed_task_resumable(task))

        task["status_code"] = 401
        with patch.object(main, "get_api_provider_exact", return_value=provider):
            self.assertFalse(main.canvas_video_failed_task_resumable(task))

        task["status_code"] = 502
        task["raw_last"] = {
            "status": "failed",
            "error": {"code": "generation_failed", "message": "generation failed"},
        }
        with patch.object(main, "get_api_provider_exact", return_value=provider):
            self.assertFalse(main.canvas_video_failed_task_resumable(task))


class VideoDownloadUrlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved_urls = []

    async def _public_url(self, value):
        return value

    async def _save_video(self, url):
        self.saved_urls.append(url)
        return "/output/video.mp4"

    @staticmethod
    def _local_path(_value):
        return None

    @staticmethod
    def _content_type(_value):
        return "application/octet-stream"

    def test_relative_download_urls_are_consistent(self):
        base_url = "https://api.example.com/gateway/v1/"
        payload = {"status": "SUCCESS", "download_url": "/v1/videos/task-id/content"}
        extractors = (
            aicost._video_urls,
            tudou._video_urls,
            geeknow._video_urls,
            megabyai._video_urls,
            sudashui._video_urls,
        )
        expected = ["https://api.example.com/gateway/v1/videos/task-id/content"]
        for extractor in extractors:
            with self.subTest(extractor=extractor.__module__):
                self.assertEqual(extractor(payload, base_url), expected)

    def test_download_url_resolution_boundaries(self):
        base_url = "https://api.example.com/gateway/v1/"
        self.assertEqual(
            resolve_video_download_url("v2/videos/task-id/content", base_url),
            "https://api.example.com/gateway/v2/videos/task-id/content",
        )
        self.assertEqual(
            resolve_video_download_url("//cdn.example.com/video.mp4", base_url),
            "https://cdn.example.com/video.mp4",
        )
        self.assertEqual(resolve_video_download_url("/output/video.mp4", base_url), "/output/video.mp4")
        self.assertEqual(resolve_video_download_url("../secret", base_url), "")
        self.assertEqual(resolve_video_download_url("ftp://example.com/video.mp4", base_url), "")

    async def test_constructed_content_download_paths_use_canonical_root(self):
        captured = []

        async def save_video(url):
            captured.append(url)
            return "/output/video.mp4"

        hostile_base = "https://api.example.com//v1/v1/"
        await aicost._save_result({}, "aicost-id", "seedance2.0-mini", hostile_base, save_video)
        await tudou._save_result({}, "tudou-id", tudou.GROK_MODEL, hostile_base, save_video)
        await geeknow._save_result({}, "geeknow-id", hostile_base, save_video)
        self.assertEqual(
            captured,
            [
                "https://api.example.com/v1/videos/aicost-id/content",
                "https://api.example.com/v1/videos/tudou-id/content",
                "https://api.example.com/v1/videos/geeknow-id/content",
            ],
        )

    async def test_aicost_content_fallback_requires_local_output(self):
        async def passthrough(url):
            return url

        with self.assertRaisesRegex(aicost.AICostProtocolError, "未能保存到本地输出目录"):
            await aicost._save_result(
                {},
                "task-id",
                "seedance2.0-mini",
                "https://www.aicost.xyz",
                passthrough,
            )

    async def test_aicost_grok_preview_submission_matches_protocol(self):
        client = _RecordingClient()
        image = "data:image/png;base64,aQ=="
        fixed_uuid = type("FixedUuid", (), {"hex": "grok-request-id"})()

        with patch.object(aicost.uuid, "uuid4", return_value=fixed_uuid):
            await aicost.generate_aicost_video(
                client,
                {
                    "model": "grok-imagine-video-1.5-preview",
                    "prompt": "test",
                    "duration": 10,
                    "aspect_ratio": "16:9",
                    "resolution": "1080p",
                    "images": [{"url": image}],
                },
                base_url="https://www.aicost.xyz",
                headers={"Authorization": "Bearer test"},
                progress=None,
                resolve_local_path=self._local_path,
                content_type_for_path=self._content_type,
                public_reference_url=self._public_url,
                save_video=self._save_video,
                poll_timeout=1,
                poll_interval=0,
            )

        _, request = client.post_requests[0]
        self.assertEqual(request["headers"]["X-Request-ID"], "grok-request-id")
        self.assertEqual(
            request["json"],
            {
                "model": "grok-imagine-video-1.5-preview",
                "prompt": "test",
                "input_reference": image,
                "seconds": "10",
                "size": "1280x720",
                "resolution": "1080p",
                "resolution_name": "1080p",
            },
        )

    async def test_aicost_seedance_submission_matches_public_contract(self):
        client = _RecordingClient()
        image_data = "data:image/png;base64,aQ=="
        audio_data = "data:audio/mpeg;base64,aQ=="

        await aicost.generate_aicost_video(
            client,
            {
                "model": "seedance2.0-fast",
                "prompt": "test",
                "duration": 5,
                "aspect_ratio": "9:16",
                "resolution": "720p",
                "images": [
                    {"url": "https://cdn.example.com/reference.png"},
                    {"url": image_data},
                ],
                "audios": ["https://cdn.example.com/reference.mp3", audio_data],
                "videos": ["https://cdn.example.com/reference.mp4"],
            },
            base_url="https://www.aicost.xyz",
            headers={"Authorization": "Bearer test"},
            progress=None,
            resolve_local_path=self._local_path,
            content_type_for_path=self._content_type,
            public_reference_url=self._public_url,
            save_video=self._save_video,
            poll_timeout=1,
            poll_interval=0,
        )

        _, request = client.post_requests[0]
        self.assertEqual(
            request["json"],
            {
                "model": "seedance2.0-fast",
                "prompt": "test",
                "duration": 5,
                "aspect_ratio": "9:16",
                "resolution": "720p",
                "image_urls": ["https://cdn.example.com/reference.png"],
                "images_base64": [image_data],
                "audio_urls": ["https://cdn.example.com/reference.mp3"],
                "audios_base64": [audio_data],
                "video_urls": ["https://cdn.example.com/reference.mp4"],
            },
        )

    async def test_public_generate_entries_use_canonical_create_urls(self):
        base_url = "https://api.example.com//v1/v1/"
        cases = []

        client = _RecordingClient()
        await aicost.generate_aicost_video(
            client,
            {"model": "seedance2.0-mini", "prompt": "x", "duration": 5, "aspect_ratio": "16:9", "resolution": "720p"},
            base_url=base_url,
            headers={},
            progress=None,
            resolve_local_path=self._local_path,
            content_type_for_path=self._content_type,
            public_reference_url=self._public_url,
            save_video=self._save_video,
            poll_timeout=1,
            poll_interval=0,
        )
        cases.append((client.posts, "https://api.example.com/v1/videos"))

        client = _RecordingClient()
        await tudou.generate_tudou_video(
            client,
            {"model": tudou.GROK_MODEL, "prompt": "x", "duration": 6, "aspect_ratio": "9:16", "resolution": "720p"},
            base_url=base_url,
            headers={},
            progress=None,
            resolve_local_path=self._local_path,
            content_type_for_path=self._content_type,
            public_reference_url=self._public_url,
            save_video=self._save_video,
            poll_timeout=1,
        )
        cases.append((client.posts, "https://api.example.com/v1/videos"))

        client = _RecordingClient()
        await geeknow.generate_geeknow_video(
            client,
            {"model": "grok-video-1.5", "prompt": "x", "duration": 10, "aspect_ratio": "1:1", "resolution": "1080p"},
            base_url=base_url,
            headers={},
            progress=None,
            resolve_local_path=self._local_path,
            content_type_for_path=self._content_type,
            public_reference_url=self._public_url,
            save_video=self._save_video,
            poll_timeout=1,
            poll_interval=0,
        )
        cases.append((client.posts, "https://api.example.com/v1/videos"))

        client = _RecordingClient()
        await megabyai.generate_megabyai_video(
            client,
            {"model": "videos-mini", "prompt": "x", "duration": 5, "aspect_ratio": "16:9", "resolution": "720p"},
            base_url=base_url,
            headers={},
            progress=None,
            public_reference_url=self._public_url,
            save_video=self._save_video,
            poll_timeout=1,
            poll_interval=0,
        )
        cases.append((client.posts, "https://api.example.com/v1/videos"))

        client = _RecordingClient()
        await sudashui.generate_sudashui_video(
            client,
            {"model": "veo3-fast", "prompt": "x", "duration": 5, "aspect_ratio": "16:9"},
            base_url=base_url,
            headers={},
            progress=None,
            resolve_local_path=self._local_path,
            content_type_for_path=self._content_type,
            save_video=self._save_video,
            poll_timeout=1,
            poll_interval=0,
        )
        cases.append((client.posts, "https://api.example.com/v1/video/generations"))

        for actual, expected in cases:
            self.assertEqual(actual, [expected])

    async def test_megabyai_preserves_local_image_source_for_publication(self):
        client = _RecordingClient()
        reference = {
            "url": "https://temp.sh/example/reference.png",
            "originalLocalUrl": "/assets/reference.png",
        }
        received = []

        async def publish(value):
            received.append(value)
            return "https://litter.catbox.moe/example.jpg"

        await megabyai.generate_megabyai_video(
            client,
            {
                "model": "videos-mini",
                "prompt": "x",
                "duration": 5,
                "aspect_ratio": "16:9",
                "resolution": "720p",
                "images": [reference],
            },
            base_url="https://api.example.com",
            headers={},
            progress=None,
            public_reference_url=publish,
            save_video=self._save_video,
            poll_timeout=1,
            poll_interval=0,
        )

        self.assertEqual(received, [reference])
        self.assertEqual(
            client.post_requests[0][1]["json"]["referenceImages"],
            ["https://litter.catbox.moe/example.jpg"],
        )

    async def test_megabyai_reports_redacted_upstream_request_details(self):
        client = _RecordingClient()
        patches = []

        async def publish(value):
            raw = value.get("url") if isinstance(value, dict) else value
            return f"https://media.example.com/{str(raw).rsplit('/', 1)[-1]}?token=signed-secret"

        await megabyai.generate_megabyai_video(
            client,
            {
                "model": "videos-mini",
                "prompt": "图片和音频参考",
                "duration": 5,
                "aspect_ratio": "16:9",
                "resolution": "720p",
                "images": [{"url": "/assets/person.jpg"}],
                "videos": ["/assets/action.mp4"],
                "audios": ["/assets/voice.mp3"],
            },
            base_url="https://api.example.com/v1",
            headers={"Authorization": "Bearer real-secret"},
            progress=patches.append,
            public_reference_url=publish,
            save_video=self._save_video,
            poll_timeout=1,
            poll_interval=0,
        )

        details = next(item["request_details"] for item in patches if "request_details" in item)
        self.assertEqual(details["method"], "POST")
        self.assertEqual(details["url"], "https://api.example.com/v1/videos")
        self.assertEqual(details["headers"]["Authorization"], "Bearer YOUR_API_KEY")
        self.assertNotIn("real-secret", str(details))
        self.assertEqual(
            video_http_preview_url("https://user:password@media.example.com/person.jpg?token=secret#frame"),
            "https://media.example.com/person.jpg",
        )
        self.assertEqual(
            details["body"],
            {
                "model": "videos-mini",
                "prompt": "图片和音频参考",
                "duration": 5,
                "ratio": "16:9",
                "resolution": "720p",
                "referenceImages": ["https://media.example.com/person.jpg"],
                "referenceVideos": ["https://media.example.com/action.mp4"],
                "referenceAudios": ["https://media.example.com/voice.mp3"],
            },
        )


    async def test_megabyai_retries_transport_error_until_completed(self):
        request = httpx.Request("GET", "https://api.example.com/v1/videos/task-id")
        client = _SequenceClient([
            httpx.ConnectError("temporary disconnect", request=request),
            httpx.Response(200, json={"status": "in_progress", "progress": 30}),
            httpx.Response(200, json={
                "status": "completed",
                "video_url": "https://api.example.com/v1/videos/task-id/content",
            }),
        ])
        patches = []

        result = await megabyai.resume_megabyai_video(
            client,
            "task-id",
            base_url="https://api.example.com",
            headers={},
            progress=patches.append,
            save_video=self._save_video,
            poll_timeout=1,
            poll_interval=0,
        )

        self.assertEqual(result["videos"], ["/output/video.mp4"])
        self.assertEqual(len(client.gets), 3)
        self.assertTrue(any("自动重试" in item.get("message", "") for item in patches))
        self.assertFalse(any(item.get("status") == "failed" for item in patches))

    async def test_megabyai_retries_503_without_retry_after(self):
        client = _SequenceClient([
            httpx.Response(503, json={"error": "gateway unavailable"}),
            httpx.Response(200, json={
                "status": "completed",
                "metadata": {"content_url": "https://api.example.com/v1/videos/task-id/content"},
            }),
        ])

        result = await megabyai.resume_megabyai_video(
            client,
            "task-id",
            base_url="https://api.example.com",
            headers={},
            progress=None,
            save_video=self._save_video,
            poll_timeout=1,
            poll_interval=0,
        )

        self.assertEqual(result["videos"], ["/output/video.mp4"])
        self.assertEqual(len(client.gets), 2)

    async def test_megabyai_does_not_retry_unauthorized_query(self):
        client = _SequenceClient([
            httpx.Response(401, json={"error": "invalid token"}),
            httpx.Response(200, json={"status": "completed"}),
        ])

        with self.assertRaises(megabyai.MegabyAIProtocolError) as captured:
            await megabyai.resume_megabyai_video(
                client,
                "task-id",
                base_url="https://api.example.com",
                headers={},
                progress=None,
                save_video=self._save_video,
                poll_timeout=1,
                poll_interval=0,
            )

        self.assertEqual(captured.exception.status_code, 401)
        self.assertEqual(len(client.gets), 1)


class PublicReferenceSafetyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _local_path(_value):
        return None

    @staticmethod
    def _content_type(_value):
        return "image/png"

    async def test_private_and_mixed_dns_results_are_rejected(self):
        private = [(2, 1, 6, "", ("10.0.0.8", 443))]
        mixed = [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (2, 1, 6, "", ("169.254.169.254", 443)),
        ]
        public = [(2, 1, 6, "", ("8.8.8.8", 443))]
        https_fake_ip = [(2, 1, 6, "", ("198.18.0.93", 443))]

        self.assertFalse(await is_public_http_url("http://127.0.0.1/image.png"))
        with patch("plugins.video_plugins.common.socket.getaddrinfo", return_value=private):
            self.assertFalse(await is_public_http_url("https://private.example/image.png"))
        with patch("plugins.video_plugins.common.socket.getaddrinfo", return_value=mixed):
            self.assertFalse(await is_public_http_url("https://mixed.example/image.png"))
        with patch("plugins.video_plugins.common.socket.getaddrinfo", return_value=public):
            self.assertTrue(await is_public_http_url("https://public.example/image.png"))
        with patch("plugins.video_plugins.common.socket.getaddrinfo", return_value=https_fake_ip):
            self.assertTrue(await is_public_http_url("https://proxy.example/image.png"))
            self.assertFalse(await is_public_http_url("http://proxy.example/image.png"))
        self.assertFalse(await is_public_http_url("https://198.18.0.93/image.png"))

    async def test_plugin_downloaders_reject_private_dns_before_request(self):
        client = _RecordingClient()
        private = [(2, 1, 6, "", ("192.168.1.10", 443))]
        with patch("plugins.video_plugins.common.socket.getaddrinfo", return_value=private):
            with self.assertRaises(geeknow.GeekNowProtocolError):
                await geeknow._reference_file(
                    client,
                    "https://private.example/image.png",
                    self._local_path,
                    self._content_type,
                )
            with self.assertRaises(tudou.TudouProtocolError):
                await tudou._reference_file(
                    client,
                    "https://private.example/image.png",
                    self._local_path,
                    self._content_type,
                )
            with self.assertRaises(aicost.AICostProtocolError):
                await aicost._media_data_url(
                    client,
                    "https://private.example/image.png",
                    "图片",
                    self._local_path,
                    self._content_type,
                )
        self.assertEqual(client.gets, [])

    async def test_public_download_pins_validated_ip_and_preserves_host_and_sni(self):
        public = [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (2, 1, 6, "", ("1.1.1.1", 443)),
        ]
        pinned_client = _PinnedClient()
        with (
            patch("plugins.video_plugins.common.socket.getaddrinfo", return_value=public),
            patch(
                "plugins.video_plugins.common.httpx.AsyncClient",
                return_value=_AsyncClientContext(pinned_client),
            ) as async_client,
        ):
            response = await public_http_get(
                "https://public.example/image.png?size=large"
            )

        self.assertIsInstance(response, _PinnedResponse)
        async_client.assert_called_once()
        self.assertFalse(async_client.call_args.kwargs["trust_env"])
        self.assertEqual(len(pinned_client.requests), 1)
        method, url, kwargs = pinned_client.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://1.1.1.1/image.png?size=large")
        self.assertEqual(kwargs["headers"]["Host"], "public.example")
        self.assertEqual(
            kwargs["extensions"]["sni_hostname"],
            "public.example",
        )
        self.assertFalse(kwargs["follow_redirects"])

    async def test_megabyai_republishes_temp_link_from_original_local_image(self):
        uploaded = {"url": "https://litter.catbox.moe/example.jpg"}
        with (
            patch(
                "main.output_file_from_url",
                side_effect=lambda value: (
                    "D:/tmp/reference.png" if value == "/assets/reference.png" else None
                ),
            ),
            patch("main.local_asset_public_url", return_value=""),
            patch("main.upload_video_to_litterbox", new=AsyncMock(return_value=uploaded)) as upload,
            patch(
                "main.public_http_get",
                new=AsyncMock(return_value=type("ImageResponse", (), {
                    "status_code": 200,
                    "headers": {"content-type": "image/jpeg"},
                })()),
            ),
        ):
            result = await main.megabyai_image_public_reference_url({
                "url": "https://temp.sh/example/reference.png",
                "originalLocalUrl": "/assets/reference.png",
            })

        self.assertEqual(result, uploaded["url"])
        upload.assert_awaited_once_with("D:/tmp/reference.png", "/assets/reference.png")

    async def test_megabyai_keeps_existing_image_host_without_reupload(self):
        with patch("main.upload_video_to_litterbox", new=AsyncMock()) as upload:
            result = await main.megabyai_image_public_reference_url({
                "url": "https://litter.catbox.moe/example.jpg",
                "originalLocalUrl": "/assets/reference.png",
            })

        self.assertEqual(result, "https://litter.catbox.moe/example.jpg")
        upload.assert_not_awaited()

    async def test_megabyai_rejects_uploaded_link_with_non_image_content_type(self):
        response = type("FileResponse", (), {
            "status_code": 200,
            "headers": {"content-type": "application/octet-stream"},
        })()
        with (
            patch(
                "main.output_file_from_url",
                return_value="D:/tmp/reference.png",
            ),
            patch("main.local_asset_public_url", return_value=""),
            patch(
                "main.upload_video_to_litterbox",
                new=AsyncMock(return_value={"url": "https://litter.catbox.moe/example.png"}),
            ),
            patch("main.public_http_get", new=AsyncMock(return_value=response)),
        ):
            with self.assertRaisesRegex(main.HTTPException, "application/octet-stream"):
                await main.megabyai_image_public_reference_url({
                    "url": "https://temp.sh/example/reference.png",
                    "originalLocalUrl": "/assets/reference.png",
                })

    async def test_megabyai_rejects_temp_link_without_local_image(self):
        with patch("main.output_file_from_url", return_value=None):
            with self.assertRaisesRegex(main.HTTPException, "temp.sh"):
                await main.megabyai_image_public_reference_url(
                    {"url": "https://temp.sh/example/reference.png"}
                )

    async def test_megabyai_reference_failure_happens_before_submit(self):
        client = _RecordingClient()

        async def reject_reference(_value):
            raise main.HTTPException(status_code=502, detail="invalid image content type")

        with self.assertRaises(megabyai.MegabyAIProtocolError) as captured:
            await megabyai.generate_megabyai_video(
                client,
                {
                    "model": "videos-mini",
                    "prompt": "x",
                    "duration": 5,
                    "aspect_ratio": "16:9",
                    "resolution": "720p",
                    "images": [{"url": "https://temp.sh/example/reference.png"}],
                },
                base_url="https://api.example.com",
                headers={},
                progress=None,
                public_reference_url=reject_reference,
                save_video=AsyncMock(return_value="/output/video.mp4"),
                poll_timeout=1,
                poll_interval=0,
            )

        self.assertEqual(captured.exception.status_code, 502)
        self.assertEqual(client.posts, [])


class CanvasVideoTaskPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved_urls = []

    async def _save_video(self, url):
        self.saved_urls.append(url)
        return "/output/video.mp4"

    async def test_effective_default_model_is_reported_for_resume(self):
        provider = {
            "id": "aicost-test",
            "name": "aicost",
            "base_url": "https://www.aicost.xyz",
            "video_request_mode": main.AICOST_VIDEO_REQUEST_MODE,
        }
        payload = main.CanvasVideoRequest(provider_id=provider["id"], prompt="test")
        progress = []
        client = _RecordingClient()

        with (
            patch.object(main, "get_api_provider", return_value=provider),
            patch.object(main, "provider_env_key_value", return_value="test-key"),
            patch.object(main.httpx, "AsyncClient", return_value=_AsyncClientContext(client)),
            patch.object(main, "generate_aicost_video", new=AsyncMock(return_value={"videos": ["/output/video.mp4"]})),
        ):
            await main.build_canvas_video_result(payload, progress.append)

        self.assertIn({"model": "veo3-fast"}, progress)

    async def test_public_resume_entries_use_canonical_query_urls(self):
        base_url = "https://api.example.com//v1/v1/"
        cases = []
        with patch("asyncio.sleep", new=AsyncMock()):
            client = _RecordingClient()
            await aicost.resume_aicost_video(
                client,
                "task/id",
                "seedance2.0-mini",
                base_url=base_url,
                headers={},
                progress=None,
                save_video=self._save_video,
                poll_timeout=1,
                poll_interval=0,
            )
            cases.append((client.gets, "https://api.example.com/v1/videos/task%2Fid"))

            client = _RecordingClient()
            await tudou.resume_tudou_video(
                client,
                "task/id",
                tudou.GROK_MODEL,
                base_url=base_url,
                headers={},
                progress=None,
                save_video=self._save_video,
                poll_timeout=1,
            )
            cases.append((client.gets, "https://api.example.com/v1/videos/task%2Fid"))

            client = _RecordingClient()
            await geeknow.resume_geeknow_video(
                client,
                "task/id",
                base_url=base_url,
                headers={},
                progress=None,
                save_video=self._save_video,
                poll_timeout=1,
                poll_interval=0,
            )
            cases.append((client.gets, "https://api.example.com/v1/videos/task%2Fid"))

            client = _RecordingClient()
            await megabyai.resume_megabyai_video(
                client,
                "task/id",
                base_url=base_url,
                headers={},
                progress=None,
                save_video=self._save_video,
                poll_timeout=1,
                poll_interval=0,
            )
            cases.append((client.gets, "https://api.example.com/v1/videos/task%2Fid"))

            client = _RecordingClient()
            await sudashui.resume_sudashui_video(
                client,
                "task/id",
                base_url=base_url,
                headers={},
                progress=None,
                save_video=self._save_video,
                poll_timeout=1,
                poll_interval=0,
            )
            cases.append((client.gets, "https://api.example.com/v1/video/generations/task%2Fid"))

        for actual, expected in cases:
            self.assertEqual(actual, [expected])


if __name__ == "__main__":
    unittest.main()
