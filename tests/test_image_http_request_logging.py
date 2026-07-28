import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock
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


class CanvasImageTaskPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tasks_file = os.path.join(self.temp.name, "canvas_image_tasks.json")
        self.file_patch = patch.object(main, "CANVAS_IMAGE_TASKS_FILE", self.tasks_file)
        self.file_patch.start()
        self.snapshot_state = dict(main._canvas_image_snapshot_state)
        self.last_persist_at = main._canvas_image_last_persist_at
        main._canvas_image_snapshot_state.update({"scheduled": 0, "written": 0})
        main._canvas_image_last_persist_at = 0.0
        with main.CANVAS_TASK_LOCK:
            self.original_tasks = dict(main.CANVAS_TASKS)
            main.CANVAS_TASKS.clear()

    def tearDown(self):
        with main.CANVAS_TASK_LOCK:
            main.CANVAS_TASKS.clear()
            main.CANVAS_TASKS.update(self.original_tasks)
        main._canvas_image_snapshot_state.update(self.snapshot_state)
        main._canvas_image_last_persist_at = self.last_persist_at
        self.file_patch.stop()
        self.temp.cleanup()

    async def test_async_snapshot_writes_cannot_overwrite_newer_generation(self):
        first = {
            "canvas_img_ordered": {
                "id": "canvas_img_ordered",
                "type": "online-image",
                "status": "queued",
                "updated_at": 1,
            },
        }
        second = {
            "canvas_img_ordered": {
                "id": "canvas_img_ordered",
                "type": "online-image",
                "status": "succeeded",
                "updated_at": 2,
            },
        }
        scheduled = []

        class _Loop:
            def create_task(self, coro):
                scheduled.append(coro)
                return mock.Mock()

        async def immediate_to_thread(callback):
            callback()

        with (
            patch.object(main.asyncio, "get_running_loop", return_value=_Loop()),
            patch.object(main.asyncio, "to_thread", side_effect=immediate_to_thread),
        ):
            main.schedule_canvas_image_snapshot_write(first)
            main.schedule_canvas_image_snapshot_write(second)

        await scheduled[1]
        await scheduled[0]
        with open(self.tasks_file, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["canvas_img_ordered"]["status"], "succeeded")

    async def test_persist_path_and_async_update_keep_latest_snapshot(self):
        first = {
            "canvas_img_existing": {
                "id": "canvas_img_existing",
                "type": "online-image",
                "status": "queued",
                "updated_at": 1,
            },
        }
        with main.CANVAS_TASK_LOCK:
            main.CANVAS_TASKS.update(first)

        main.persist_canvas_image_tasks()
        main.update_canvas_image_task("canvas_img_existing", {"status": "succeeded", "result": {"images": ["/assets/output/latest.png"]}})
        await asyncio.sleep(0.05)

        with open(self.tasks_file, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["canvas_img_existing"]["status"], "succeeded")

    async def test_create_and_complete_task_persist_full_lifecycle(self):
        payload = main.OnlineImageRequest(
            prompt="常驻日志测试",
            provider_id="custom-image",
            model="gpt-image-2",
        )
        started = []

        def capture_task(coro):
            started.append(coro)
            return mock.Mock()

        with (
            patch.object(main.asyncio, "create_task", side_effect=capture_task),
            patch.object(main, "persist_canvas_image_tasks", side_effect=lambda: main.write_canvas_image_tasks_snapshot_serialized(main.canvas_image_task_snapshot_unlocked())),
            patch.object(main, "schedule_canvas_image_snapshot_write", side_effect=main.write_canvas_image_tasks_snapshot),
        ):
            response = await main.create_canvas_image_task(payload)
            task_id = response["task_id"]
            with open(self.tasks_file, encoding="utf-8") as handle:
                stored = json.load(handle)
            self.assertEqual(stored[task_id]["status"], "queued")
            self.assertEqual(stored[task_id]["request"]["prompt"], "常驻日志测试")

            async def fake_build(_payload, progress=None):
                progress({"status": "polling", "upstream_task_id": "upstream-image-1"})
                return {"images": ["/assets/output/generated.png"]}

            with patch.object(main, "build_online_image_result", side_effect=fake_build):
                await started[0]

            with open(self.tasks_file, encoding="utf-8") as handle:
                stored = json.load(handle)
            self.assertEqual(stored[task_id]["status"], "succeeded")
            self.assertEqual(stored[task_id]["upstream_task_id"], "upstream-image-1")
            self.assertEqual(stored[task_id]["result"]["images"], ["/assets/output/generated.png"])

    async def test_get_task_lazy_loads_persisted_snapshot(self):
        task_id = "canvas_img_persisted"
        with open(self.tasks_file, "w", encoding="utf-8") as handle:
            json.dump({task_id: {
                "id": task_id,
                "type": "online-image",
                "status": "succeeded",
                "updated_at": 10,
                "result": {"images": ["/assets/output/persisted.png"]},
            }}, handle)

        task = await main.get_canvas_image_task(task_id)

        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(task["result"]["images"], ["/assets/output/persisted.png"])

    async def test_startup_resumes_only_tasks_with_upstream_id(self):
        resumable_id = "canvas_img_resumable"
        unsafe_id = "canvas_img_without_upstream"
        with open(self.tasks_file, "w", encoding="utf-8") as handle:
            json.dump({
                resumable_id: {
                    "id": resumable_id,
                    "type": "online-image",
                    "status": "polling",
                    "provider_id": "custom-image",
                    "upstream_task_id": "upstream-image-2",
                    "updated_at": 20,
                },
                unsafe_id: {
                    "id": unsafe_id,
                    "type": "online-image",
                    "status": "running",
                    "provider_id": "custom-image",
                    "updated_at": 10,
                },
            }, handle)
        scheduled = []

        def capture_task(coro):
            scheduled.append(coro)
            coro.close()
            return mock.Mock()

        with (
            patch.object(main.asyncio, "create_task", side_effect=capture_task),
            patch.object(main, "schedule_canvas_image_snapshot_write", side_effect=main.write_canvas_image_tasks_snapshot),
        ):
            await main.resume_canvas_image_tasks_on_startup()

        self.assertEqual(len(scheduled), 1)
        with main.CANVAS_TASK_LOCK:
            self.assertEqual(main.CANVAS_TASKS[resumable_id]["status"], "polling")
            self.assertEqual(main.CANVAS_TASKS[unsafe_id]["status"], "failed")
            self.assertIn("避免重复扣费", main.CANVAS_TASKS[unsafe_id]["error"])


class ImageHttpRequestLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_modelscope_task_query_uses_modelscope_endpoint(self):
        provider = {
            "id": "modelscope",
            "name": "ModelScope",
            "base_url": "https://api-inference.modelscope.cn/v1",
        }

        class ModelScopeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"task_status": "SUCCEED", "output_images": ["https://cdn.example.com/modelscope.png"]}

        class ModelScopeClient:
            def __init__(self):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return ModelScopeResponse()

        client = ModelScopeClient()
        with (
            patch.object(main, "get_api_provider", return_value=provider),
            patch.object(main, "modelscope_api_key", return_value="test-token"),
            patch.object(main.httpx, "AsyncClient", return_value=client),
            patch.object(main, "save_ai_image_to_output", return_value="/assets/output/modelscope.png"),
            patch.object(main, "save_to_history"),
        ):
            result = await main.query_image_task(main.ImageTaskQueryRequest(provider_id="modelscope", task_id="task/modelscope"))

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["images"], ["/assets/output/modelscope.png"])
        self.assertEqual(client.calls[0][0], "https://api-inference.modelscope.cn/v1/tasks/task%2Fmodelscope")
        self.assertEqual(client.calls[0][1]["headers"]["X-ModelScope-Task-Type"], "image_generation")

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
