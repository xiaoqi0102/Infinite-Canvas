import base64
import io
import json
import unittest

import httpx

from plugins.video_plugins.common import (
    submit_video_http_request,
    video_http_preview_files,
    video_http_preview_headers,
    video_http_preview_url,
    video_http_preview_value,
)


AUTH_SECRET = "sk-auth-secret-1234"
API_SECRET = "sk-api-secret-5678"
COOKIE_SECRET = "session=cookie-secret-9012"
TOKEN_SECRET = "plain-token-secret-3456"
QUERY_SECRET = "signed-query-secret-7890"
FILE_SECRET = b"private-file-content-should-never-be-logged"
BASE64_SECRET = base64.b64encode(FILE_SECRET * 20).decode("ascii")


class _Response:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _RecordingClient:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [_Response()])
        self.error = error
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


class _ProgressRecorder:
    def __init__(self):
        self.snapshots = []
        self.serialized = []

    def __call__(self, patch):
        encoded = json.dumps(patch, ensure_ascii=False, sort_keys=True)
        self.serialized.append(encoded)
        self.snapshots.append(json.loads(encoded))

    @property
    def details(self):
        return self.snapshots[-1]["request_details"]


def _serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class VideoHttpPreviewTests(unittest.TestCase):
    def test_url_preview_removes_userinfo_query_fragment_and_embedded_data(self):
        self.assertEqual(
            video_http_preview_url(
                f"https://user:password@media.example.com/input.png?token={QUERY_SECRET}#frame"
            ),
            "https://media.example.com/input.png",
        )
        data_url = f"data:image/png;base64,{BASE64_SECRET}"
        data_preview = video_http_preview_url(data_url)
        self.assertIn("data:image/png", data_preview)
        self.assertNotIn(BASE64_SECRET, data_preview)
        self.assertNotIn(FILE_SECRET.decode("ascii"), data_preview)
        self.assertNotIn("blob:private-object-id", video_http_preview_url("blob:private-object-id"))

    def test_headers_and_nested_values_hide_all_sensitive_fields(self):
        preview_headers, secrets = video_http_preview_headers(
            {
                "Authorization": f"Bearer {AUTH_SECRET}",
                "Proxy-Authorization": f"Basic {API_SECRET}",
                "X-Api-Key": API_SECRET,
                "Cookie": COOKIE_SECRET,
                "Content-Type": "application/json",
            }
        )
        self.assertEqual(preview_headers["Authorization"], "Bearer YOUR_API_KEY")
        self.assertEqual(preview_headers["Content-Type"], "application/json")
        self.assertNotIn(AUTH_SECRET, _serialized(preview_headers))
        self.assertNotIn(API_SECRET, _serialized(preview_headers))
        self.assertNotIn(COOKIE_SECRET, _serialized(preview_headers))
        self.assertIn(AUTH_SECRET, secrets)

        value = {
            "authorization": f"Bearer {AUTH_SECRET}",
            "apiKey": API_SECRET,
            "cookie": COOKIE_SECRET,
            "token": TOKEN_SECRET,
            "nested": {
                "access_token": TOKEN_SECRET,
                "refresh_token": TOKEN_SECRET,
                "image_url": (
                    f"https://user:password@media.example.com/input.png"
                    f"?signature={QUERY_SECRET}#preview"
                ),
            },
            "data_url": f"data:image/png;base64,{BASE64_SECRET}",
            "blob_url": "blob:private-object-id",
            "image_base64": BASE64_SECRET,
        }
        preview = video_http_preview_value(
            value,
            secret_values=(AUTH_SECRET, API_SECRET, COOKIE_SECRET, TOKEN_SECRET),
        )
        encoded = _serialized(preview)
        for secret in (
            AUTH_SECRET,
            API_SECRET,
            COOKIE_SECRET,
            TOKEN_SECRET,
            QUERY_SECRET,
            BASE64_SECRET,
            "private-object-id",
            "user:password",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(
            preview["nested"]["image_url"],
            "https://media.example.com/input.png",
        )
        self.assertIn("已省略", preview["data_url"])
        self.assertIn("已省略", preview["blob_url"])
        self.assertIn("已省略", preview["image_base64"])
        json.dumps(preview, ensure_ascii=False)

    def test_multipart_preview_records_metadata_without_bytes_or_moving_cursor(self):
        stream = io.BytesIO(FILE_SECRET)
        stream.seek(7)
        initial_position = stream.tell()
        files = [
            ("model", (None, "videos-mini")),
            ("api_key", (None, API_SECRET)),
            (
                "input_reference",
                (r"C:\Users\Admin\reference.png", stream, "image/png"),
            ),
            ("audio_reference", ("voice.mp3", FILE_SECRET, "audio/mpeg")),
        ]

        preview = video_http_preview_files(files)
        self.assertEqual(stream.tell(), initial_position)
        encoded = _serialized(preview)
        self.assertNotIn(API_SECRET, encoded)
        self.assertNotIn(FILE_SECRET.decode("ascii"), encoded)
        self.assertNotIn(repr(FILE_SECRET), encoded)
        self.assertEqual(preview[0], {"field": "model", "value": "videos-mini"})
        self.assertNotEqual(preview[1]["value"], API_SECRET)
        self.assertEqual(preview[2]["size"], len(FILE_SECRET))
        self.assertEqual(preview[3]["size"], len(FILE_SECRET))
        json.dumps(preview, ensure_ascii=False)


class SubmitVideoHttpRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {AUTH_SECRET}",
            "X-Api-Key": API_SECRET,
            "Cookie": COOKIE_SECRET,
            "Content-Type": "application/json",
        }
        self.url = (
            f"https://user:password@api.example.com/v1/videos"
            f"?token={QUERY_SECRET}#submit"
        )

    def assert_no_secrets(self, value):
        encoded = _serialized(value)
        for secret in (
            AUTH_SECRET,
            API_SECRET,
            COOKIE_SECRET,
            TOKEN_SECRET,
            QUERY_SECRET,
            BASE64_SECRET,
            FILE_SECRET.decode("ascii"),
            "user:password",
        ):
            self.assertNotIn(secret, encoded)

    async def test_success_records_real_request_and_response_without_mutating_outbound_data(self):
        body = {
            "model": "videos-mini",
            "prompt": "测试真实请求",
            "api_key": API_SECRET,
            "token": TOKEN_SECRET,
            "referenceImages": [
                f"https://media.example.com/person.jpg?signature={QUERY_SECRET}"
            ],
            "inline": f"data:image/png;base64,{BASE64_SECRET}",
            "image_base64": BASE64_SECRET,
        }
        response = _Response(
            status_code=201,
            payload={
                "id": "task-1",
                "access_token": TOKEN_SECRET,
                "video_url": (
                    f"https://cdn.example.com/output.mp4?signature={QUERY_SECRET}"
                ),
                "echo": AUTH_SECRET,
            },
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": "request-1",
                "Set-Cookie": COOKIE_SECRET,
                "Authorization": f"Bearer {AUTH_SECRET}",
            },
        )
        client = _RecordingClient([response])
        progress = _ProgressRecorder()

        actual = await submit_video_http_request(
            client,
            progress=progress,
            url=self.url,
            headers=self.headers,
            json_body=body,
            context={"provider_id": "test", "api_key": API_SECRET},
        )

        self.assertIs(actual, response)
        self.assertEqual(client.calls, [(self.url, {"headers": self.headers, "json": body})])
        self.assertEqual(body["api_key"], API_SECRET)
        details = progress.details
        self.assertEqual(details["transport"], "backend_http")
        self.assertEqual(len(details["attempts"]), 1)
        exchange = details["attempts"][0]
        self.assertEqual(exchange["request"]["url"], "https://api.example.com/v1/videos")
        self.assertEqual(exchange["request"]["headers"]["Authorization"], "Bearer YOUR_API_KEY")
        self.assertEqual(exchange["response"]["status_code"], 201)
        self.assertEqual(exchange["response"]["headers"]["X-Request-ID"], "request-1")
        self.assertNotIn("Set-Cookie", exchange["response"]["headers"])
        self.assertEqual(
            exchange["response"]["body"]["video_url"],
            "https://cdn.example.com/output.mp4",
        )
        self.assert_no_secrets(details)
        self.assertGreaterEqual(len(progress.snapshots), 2)

    async def test_http_failure_response_is_recorded_and_returned_for_caller_policy(self):
        response = _Response(
            status_code=401,
            payload={
                "error": "invalid credentials",
                "authorization": f"Bearer {AUTH_SECRET}",
                "token": TOKEN_SECRET,
                "documentation_url": (
                    f"https://docs.example.com/error?token={QUERY_SECRET}"
                ),
            },
            headers={
                "Content-Type": "application/json",
                "Retry-After": "30",
                "Set-Cookie": COOKIE_SECRET,
            },
        )
        client = _RecordingClient([response])
        progress = _ProgressRecorder()

        actual = await submit_video_http_request(
            client,
            progress=progress,
            url=self.url,
            headers=self.headers,
            json_body={"model": "videos-mini"},
        )

        self.assertIs(actual, response)
        exchange = progress.details["attempts"][0]
        self.assertTrue(exchange["response"]["received"])
        self.assertEqual(exchange["response"]["status_code"], 401)
        self.assertEqual(exchange["response"]["headers"]["Retry-After"], "30")
        self.assertNotIn("Set-Cookie", exchange["response"]["headers"])
        self.assertEqual(
            exchange["response"]["body"]["documentation_url"],
            "https://docs.example.com/error",
        )
        self.assert_no_secrets(progress.details)

    async def test_transport_error_is_reraised_after_sanitized_attempt_is_reported(self):
        request = httpx.Request("POST", self.url)
        error = httpx.ConnectError(
            f"connection failed for Bearer {AUTH_SECRET}",
            request=request,
        )
        client = _RecordingClient(error=error)
        progress = _ProgressRecorder()

        with self.assertRaises(httpx.ConnectError):
            await submit_video_http_request(
                client,
                progress=progress,
                url=self.url,
                headers=self.headers,
                json_body={"model": "videos-mini", "token": TOKEN_SECRET},
            )

        exchange = progress.details["attempts"][0]
        self.assertFalse(exchange["response"]["received"])
        self.assertEqual(exchange["response"]["error_type"], "ConnectError")
        self.assert_no_secrets(progress.details)
        self.assertGreaterEqual(len(progress.snapshots), 2)

    async def test_shared_attempts_preserve_failed_candidate_and_successful_retry(self):
        attempts = []
        progress = _ProgressRecorder()
        first_client = _RecordingClient(
            [_Response(status_code=404, payload={"error": "not found"})]
        )
        second_client = _RecordingClient(
            [_Response(status_code=200, payload={"id": "task-2"})]
        )

        await submit_video_http_request(
            first_client,
            progress=progress,
            url=f"https://api.example.com/v1/videos?token={QUERY_SECRET}",
            headers=self.headers,
            json_body={"model": "videos-mini"},
            attempts=attempts,
        )
        await submit_video_http_request(
            second_client,
            progress=progress,
            url=f"https://api.example.com/v2/videos?token={QUERY_SECRET}",
            headers=self.headers,
            json_body={"model": "videos-mini"},
            attempts=attempts,
        )

        details = progress.details
        self.assertEqual(len(details["attempts"]), 2)
        self.assertEqual(
            [item["response"]["status_code"] for item in details["attempts"]],
            [404, 200],
        )
        self.assertEqual(
            [item["request"]["url"] for item in details["attempts"]],
            [
                "https://api.example.com/v1/videos",
                "https://api.example.com/v2/videos",
            ],
        )
        self.assert_no_secrets(details)
        json.dumps(details, ensure_ascii=False)

    async def test_multipart_logs_form_and_file_metadata_without_consuming_stream(self):
        stream = io.BytesIO(FILE_SECRET)
        stream.seek(5)
        initial_position = stream.tell()
        form = {
            "model": "grok-video",
            "prompt": "图片参考",
            "token": TOKEN_SECRET,
        }
        files = [
            ("input_reference", ("reference.png", stream, "image/png")),
            ("audio_reference", ("voice.mp3", FILE_SECRET, "audio/mpeg")),
        ]
        response = _Response(status_code=200, payload={"id": "multipart-task"})
        client = _RecordingClient([response])
        progress = _ProgressRecorder()

        await submit_video_http_request(
            client,
            progress=progress,
            url=self.url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {AUTH_SECRET}",
            },
            form=form,
            files=files,
        )

        self.assertEqual(stream.tell(), initial_position)
        self.assertEqual(
            client.calls,
            [
                (
                    self.url,
                    {
                        "headers": {
                            "Accept": "application/json",
                            "Authorization": f"Bearer {AUTH_SECRET}",
                        },
                        "data": form,
                        "files": files,
                    },
                )
            ],
        )
        request_preview = progress.details["attempts"][0]["request"]
        self.assertEqual(request_preview["format"], "multipart")
        self.assertNotEqual(request_preview["form"]["token"], TOKEN_SECRET)
        self.assertEqual(request_preview["files"][0]["size"], len(FILE_SECRET))
        self.assertEqual(request_preview["files"][1]["size"], len(FILE_SECRET))
        self.assert_no_secrets(progress.details)
        json.dumps(progress.details, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
