import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


class MediaPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.assets = root / "assets"
        self.generated = self.assets / "output"
        self.inputs = self.assets / "input"
        self.local = self.assets / "uploads"
        self.data = root / "data"
        self.canvases = self.data / "canvases"
        self.conversations = self.data / "conversations"
        self.previews = self.data / "media_previews"
        for path in (self.generated, self.inputs, self.local, self.canvases, self.conversations, self.previews):
            path.mkdir(parents=True, exist_ok=True)
        self.history = root / "history.json"
        self.asset_library = self.data / "asset_library.json"
        self.video_tasks = self.data / "canvas_video_tasks.json"
        self.history.write_text("[]", encoding="utf-8")
        self.asset_library.write_text("{}", encoding="utf-8")
        self.video_tasks.write_text("{}", encoding="utf-8")
        self.patches = [
            patch.object(main, "ASSETS_DIR", str(self.assets)),
            patch.object(main, "OUTPUT_DIR", str(root / "legacy-output")),
            patch.object(main, "OUTPUT_INPUT_DIR", str(self.inputs)),
            patch.object(main, "OUTPUT_OUTPUT_DIR", str(self.generated)),
            patch.object(main, "LOCAL_UPLOAD_DIR", str(self.local)),
            patch.object(main, "DATA_DIR", str(self.data)),
            patch.object(main, "CANVAS_DIR", str(self.canvases)),
            patch.object(main, "CONVERSATION_DIR", str(self.conversations)),
            patch.object(main, "MEDIA_PREVIEW_DIR", str(self.previews)),
            patch.object(main, "HISTORY_FILE", str(self.history)),
            patch.object(main, "ASSET_LIBRARY_PATH", str(self.asset_library)),
            patch.object(main, "CANVAS_VIDEO_TASKS_FILE", str(self.video_tasks)),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def media_file(self, name="result.png"):
        path = self.generated / name
        image = Image.new("RGB", (2, 2), "red")
        image.save(path, format="PNG")
        return path, main.output_url_for(name)

    def write_canvas(self, canvas_id, value):
        (self.canvases / f"{canvas_id}.json").write_text(json.dumps(value), encoding="utf-8")

    def test_reference_protects_file_and_unreferenced_file_is_removed(self):
        referenced, url = self.media_file("referenced.png")
        self.write_canvas("owner", {"id": "owner", "nodes": [{"url": url}]})
        self.assertEqual(main.delete_media_file_if_unreferenced(str(referenced)), "referenced")
        self.assertTrue(referenced.exists())

        free, _ = self.media_file("free.png")
        self.assertEqual(main.delete_media_file_if_unreferenced(str(free)), "removed")
        self.assertFalse(free.exists())

    async def test_history_delete_matches_all_same_timestamp_and_keeps_canvas_owner(self):
        path, url = self.media_file()
        history = [
            {"timestamp": 42, "images": [url]},
            {"timestamp": 42, "images": [{"url": url}]},
        ]
        self.history.write_text(json.dumps(history), encoding="utf-8")
        self.write_canvas("owner", {"id": "owner", "nodes": [{"url": url}]})

        result = await main.delete_history(main.DeleteHistoryRequest(timestamp=42))

        self.assertTrue(result["success"])
        self.assertEqual(result["removed_records"], 2)
        self.assertEqual(result["skipped_referenced"], 1)
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(self.history.read_text(encoding="utf-8")), [])

    def test_rename_rewrites_persisted_urls_and_preserves_query_and_fragment(self):
        path, old_url = self.media_file("old.png")
        new_url = main.output_url_for("new.png")
        self.write_canvas("canvas", {"id": "canvas", "nodes": [{"url": f"{old_url}?v=1#thumb"}]})
        result = main.rewrite_persisted_media_urls({old_url: new_url})
        self.assertEqual(result["references"], 1)
        value = json.loads((self.canvases / "canvas.json").read_text(encoding="utf-8"))
        self.assertEqual(value["nodes"][0]["url"], f"{new_url}?v=1#thumb")
        self.assertTrue(path.exists())

    def test_invalid_image_and_video_payloads_are_rejected(self):
        with self.assertRaises(ValueError):
            main.validated_image_extension(b"<html>not image</html>")
        with self.assertRaises(ValueError):
            main.validated_video_extension(b"{\"error\":true}")

        buffer = io.BytesIO()
        Image.new("RGB", (1, 1), "blue").save(buffer, format="PNG")
        self.assertEqual(main.validated_image_extension(buffer.getvalue()), ".png")
        self.assertEqual(main.validated_video_extension(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16), ".mp4")

    def test_storage_directory_copy_rejects_conflicting_content(self):
        source = Path(self.temp.name) / "source"
        target = Path(self.temp.name) / "target"
        source.mkdir()
        target.mkdir()
        (source / "a.bin").write_bytes(b"source")
        (target / "a.bin").write_bytes(b"target")
        with self.assertRaises(main.HTTPException) as caught:
            main.copy_storage_directory_files(str(source), str(target))
        self.assertEqual(caught.exception.status_code, 409)

    def test_storage_settings_roll_back_earlier_directory_when_later_copy_fails(self):
        root = Path(self.temp.name)
        current_upload = root / "current-upload"
        current_generated = root / "current-generated"
        current_local = root / "current-local"
        target_upload = root / "target-upload"
        target_generated = root / "target-generated"
        target_local = root / "target-local"
        for path in (
            current_upload,
            current_generated,
            current_local,
            target_upload,
            target_generated,
            target_local,
        ):
            path.mkdir()
        (current_upload / "copied.bin").write_bytes(b"upload")
        (current_generated / "conflict.bin").write_bytes(b"source")
        (target_generated / "conflict.bin").write_bytes(b"target")
        current = {
            "upload": str(current_upload),
            "generated": str(current_generated),
            "local": str(current_local),
        }
        payload = {
            "upload": str(target_upload),
            "generated": str(target_generated),
            "local": str(target_local),
        }

        with patch.object(main, "load_storage_settings", return_value={"dirs": current}):
            with self.assertRaises(main.HTTPException) as caught:
                main.save_storage_settings(payload)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse((target_upload / "copied.bin").exists())
        self.assertEqual((target_generated / "conflict.bin").read_bytes(), b"target")

    def test_storage_copy_rejects_target_symlink_escape(self):
        root = Path(self.temp.name)
        source = root / "symlink-source"
        target = root / "symlink-target"
        outside = root / "outside"
        (source / "nested").mkdir(parents=True)
        target.mkdir()
        outside.mkdir()
        (source / "nested" / "media.bin").write_bytes(b"media")
        try:
            (target / "nested").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前 Windows 环境不允许创建目录符号链接")

        with self.assertRaises(main.HTTPException) as caught:
            main.copy_storage_directory_files(str(source), str(target))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertFalse((outside / "media.bin").exists())

    def test_atomic_copy_cleans_partial_file_after_interruption(self):
        source = Path(self.temp.name) / "atomic-source.bin"
        target = Path(self.temp.name) / "atomic-target.bin"
        source.write_bytes(b"complete")

        def interrupted_copy(source_file, target_file, length):
            target_file.write(b"partial")
            raise OSError("simulated interruption")

        with patch.object(main.shutil, "copyfileobj", side_effect=interrupted_copy):
            with self.assertRaises(OSError):
                main.copy_file_atomic(str(source), str(target))

        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
