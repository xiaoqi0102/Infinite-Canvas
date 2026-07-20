import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("INFINITE_CANVAS_SKIP_STATIC_SYNC", "1")

import main


BASE_URL = "https://api.example.com"
API_KEY = "test-key"


def _response(method, url, status_code, payload):
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request(method, url),
    )


class _QueuedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, **kwargs):
        return await self._request("GET", url, kwargs)

    async def post(self, url, **kwargs):
        return await self._request("POST", url, kwargs)

    async def _request(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"没有为 {method} {url} 准备响应")
        return self.responses.pop(0)


class VolcengineTaskProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_json_404_does_not_prove_task_endpoint(self):
        url = main.volcengine_task_probe_url(BASE_URL)
        client = AsyncMock()
        client.get.return_value = _response("GET", url, 404, {"detail": "Not Found"})

        ok, probe = await main.probe_volcengine_task_endpoint(client, BASE_URL, API_KEY)

        self.assertFalse(ok)
        self.assertEqual(probe["status"], 404)
        self.assertEqual(probe["raw"], {"detail": "Not Found"})
        self.assertIn("普通 404", probe["message"])

    async def test_explicit_task_errors_prove_task_endpoint(self):
        cases = (
            (
                400,
                {
                    "error": {
                        "type": "BadRequest",
                        "code": "InvalidParameter",
                        "message": "Invalid task ID: healthcheck_probe_do_not_submit",
                    }
                },
            ),
            (422, {"detail": "task_id is invalid"}),
            (
                404,
                {
                    "error": {
                        "type": "NotFound",
                        "code": "NotFound.ID",
                        "message": "The specified content generation task is not found",
                        "param": "id",
                    }
                },
            ),
        )
        url = main.volcengine_task_probe_url(BASE_URL)

        for status_code, payload in cases:
            with self.subTest(status_code=status_code):
                client = AsyncMock()
                client.get.return_value = _response("GET", url, status_code, payload)
                ok, probe = await main.probe_volcengine_task_endpoint(client, BASE_URL, API_KEY)
                self.assertTrue(ok)
                self.assertEqual(probe["status"], status_code)
                self.assertEqual(probe["raw"], payload)

    async def test_success_response_proves_task_endpoint(self):
        url = main.volcengine_task_probe_url(BASE_URL)
        client = AsyncMock()
        payload = {"id": "probe", "status": "queued"}
        client.get.return_value = _response("GET", url, 200, payload)

        ok, probe = await main.probe_volcengine_task_endpoint(client, BASE_URL, API_KEY)

        self.assertTrue(ok)
        self.assertEqual(probe["status"], 200)
        self.assertEqual(probe["raw"], payload)

    async def test_plain_chat_json_404_is_not_a_volcengine_signal(self):
        url = f"{BASE_URL}/v1/chat/completions"
        client = AsyncMock()
        client.post.return_value = _response("POST", url, 404, {"detail": "Not Found"})

        ok, probe = await main.probe_openai_compat_bearer_endpoint(client, BASE_URL, API_KEY)

        self.assertFalse(ok)
        self.assertEqual(probe["status"], 404)


class ProviderProbeFlowTests(unittest.IsolatedAsyncioTestCase):
    def _payload(self):
        return main.TestConnectionPayload(
            base_url=BASE_URL,
            api_key=API_KEY,
            protocol="openai",
            image_request_mode="aicost-image",
        )

    async def test_verify_protocol_keeps_openai_after_plain_json_404s(self):
        client = _QueuedClient([
            _response(
                "GET",
                f"{BASE_URL}/v1/tasks/healthcheck_probe_do_not_submit",
                404,
                {"detail": "Not Found"},
            ),
            _response("GET", f"{BASE_URL}/v1/models", 404, {"detail": "Not Found"}),
            _response("POST", f"{BASE_URL}/v1/chat/completions", 404, {"detail": "Not Found"}),
            _response("GET", main.volcengine_task_probe_url(BASE_URL), 404, {"detail": "Not Found"}),
            _response("POST", f"{BASE_URL}/v1/chat/completions", 404, {"detail": "Not Found"}),
        ])

        with patch.object(main.httpx, "AsyncClient", return_value=client):
            result = await main.probe_async_endpoint(self._payload())

        self.assertFalse(result["ok"])
        self.assertEqual(result["protocol"], "openai")
        self.assertEqual(result["image_request_mode"], "aicost-image")
        self.assertEqual(len(client.calls), 5)

    async def test_verify_address_does_not_switch_to_volcengine_on_plain_json_404(self):
        client = _QueuedClient([
            _response("GET", f"{BASE_URL}/v1/models", 404, {"detail": "Not Found"}),
            _response("GET", main.volcengine_task_probe_url(BASE_URL), 404, {"detail": "Not Found"}),
            _response("POST", f"{BASE_URL}/v1/chat/completions", 404, {"detail": "Not Found"}),
        ])

        with patch.object(main.httpx, "AsyncClient", return_value=client):
            result = await main.test_provider_connection(self._payload())

        self.assertFalse(result["ok"])
        self.assertNotEqual(result.get("protocol"), "volcengine")
        self.assertEqual(len(client.calls), 3)

    async def test_fetch_models_does_not_switch_to_volcengine_on_plain_json_404(self):
        client = _QueuedClient([
            _response("GET", f"{BASE_URL}/v1/models", 404, {"detail": "Not Found"}),
            _response("GET", main.volcengine_task_probe_url(BASE_URL), 404, {"detail": "Not Found"}),
            _response("POST", f"{BASE_URL}/v1/chat/completions", 404, {"detail": "Not Found"}),
        ])

        with patch.object(main.httpx, "AsyncClient", return_value=client):
            with self.assertRaises(main.HTTPException) as caught:
                await main.fetch_upstream_models_from_payload(self._payload())

        self.assertEqual(caught.exception.status_code, 404)
        self.assertNotIn("方舟", str(caught.exception.detail))
        self.assertEqual(len(client.calls), 3)


if __name__ == "__main__":
    unittest.main()
