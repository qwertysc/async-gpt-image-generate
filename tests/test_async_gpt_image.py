from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "async_gpt_image.py"
SPEC = importlib.util.spec_from_file_location("async_gpt_image", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MockHandler(BaseHTTPRequestHandler):
    state = {
        "requests": [],
        "polls": {},
    }

    def log_message(self, _format, *_args):
        return

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.state["requests"].append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )
        if self.path == "/v1/images/generations/async":
            self._json(202, {"task_id": "task-gen", "status": "processing"})
            return
        if self.path == "/v1/images/edits/async":
            self._json(202, {"task_id": "task-edit", "status": "processing"})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_GET(self):
        self.state["requests"].append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        task_id = self.path.rsplit("/", 1)[-1]
        count = self.state["polls"].get(task_id, 0) + 1
        self.state["polls"][task_id] = count
        if task_id == "task-gen":
            if count == 1:
                self._json(200, {"task_id": task_id, "status": "processing"})
            else:
                self._json(
                    200,
                    {
                        "task_id": task_id,
                        "status": "completed",
                        "result": {"data": [{"b64_json": "aW1hZ2UtYnl0ZXM="}]},
                    },
                )
            return
        if task_id == "task-edit":
            self._json(
                200,
                {
                    "task_id": task_id,
                    "status": "completed",
                    "result": {"data": [{"b64_json": "ZWRpdGVkLWltYWdl"}]},
                },
            )
            return
        if task_id == "task-fail":
            self._json(200, {"task_id": task_id, "status": "failed", "error": {"message": "bad prompt"}})
            return
        if task_id == "task-retry":
            if count == 1:
                self._json(500, {"error": {"message": "temporary"}})
            else:
                self._json(
                    200,
                    {
                        "task_id": task_id,
                        "status": "completed",
                        "result": {"data": [{"b64_json": "cmV0cmllZA=="}]},
                    },
                )
            return
        self._json(404, {"error": {"message": "unknown task"}})


class AsyncImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        MockHandler.state["requests"] = []
        MockHandler.state["polls"] = {}
        self.temporary = tempfile.TemporaryDirectory()
        self.root = MODULE._prepare_data_root(Path(self.temporary.name) / "data")
        self.config = MODULE.validate_config(
            {
                "base_url": f"http://127.0.0.1:{self.server.server_address[1]}",
                "api_key": "test-secret-key",
                "model": "gpt-image-2",
                "poll_interval_seconds": 0.1,
                "request_timeout_seconds": 2,
                "max_wait_seconds": 3,
            }
        )

    def tearDown(self):
        self.temporary.cleanup()

    def parameters(self):
        return {
            "model": "gpt-image-2",
            "prompt": "draw a cat",
            "size": "auto",
            "quality": "auto",
            "output_format": "png",
            "moderation": "auto",
            "n": 1,
        }

    def test_normalizes_default_base_url(self):
        self.assertEqual(
            MODULE.normalize_api_base_url("https://edge.1token-store.com"),
            "https://edge.1token-store.com/v1",
        )

    def test_default_and_per_request_model_selection(self):
        self.assertEqual(MODULE.DEFAULT_MODEL, "gpt-image-2.5")
        self.assertEqual(MODULE.DEFAULT_CONFIG["model"], "gpt-image-2.5")
        self.assertEqual(list(MODULE.RECOMMENDED_MODELS), ["gpt-image-2.5", "gpt-image-2"])

        for command in ("generate", "edit"):
            arguments = [command]
            if command == "edit":
                arguments.extend(["--image", "reference.png"])
            arguments.extend(["--prompt", "draw a cat", "--model", "custom-image-model"])
            parsed = MODULE.build_parser().parse_args(arguments)
            parameters = MODULE._request_parameters(parsed, self.config)
            self.assertEqual(parameters["model"], "custom-image-model")

    def test_generation_failure_lists_models_without_retrying(self):
        MODULE.atomic_write_json(self.root / "config.json", self.config)
        output = io.StringIO()
        failure = MODULE.RequestFailure("model unavailable", status=400, retryable=False)
        with patch.object(MODULE, "submit_generation", side_effect=failure) as submit:
            with redirect_stdout(output):
                exit_code = MODULE.main(
                    [
                        "generate",
                        "--prompt",
                        "draw a cat",
                        "--model",
                        "gpt-image-2.5",
                        "--data-dir",
                        str(self.root),
                        "--json",
                    ]
                )
        self.assertEqual(exit_code, MODULE.RequestFailure.exit_code)
        self.assertEqual(submit.call_count, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["model"], "gpt-image-2.5")
        self.assertEqual(result["recommended_models"], ["gpt-image-2.5", "gpt-image-2"])
        self.assertEqual(result["suggested_model"], "gpt-image-2")
        self.assertEqual(
            MODULE.normalize_api_base_url("https://edge.1token-store.com/v1/"),
            "https://edge.1token-store.com/v1",
        )

    def test_falls_back_to_current_project_when_home_is_unusable(self):
        base = Path(self.temporary.name)
        fake_home = base / "home-is-a-file"
        fake_home.write_text("not a directory", encoding="utf-8")
        project = base / "project"
        resolved, used_fallback = MODULE.resolve_data_root(
            home=fake_home,
            cwd=project,
            allow_project_fallback=True,
        )
        self.assertTrue(used_fallback)
        self.assertEqual(resolved, (project / MODULE.SKILL_NAME).resolve())
        self.assertTrue((resolved / "tasks").is_dir())

    def test_does_not_silently_fall_back_to_current_project(self):
        base = Path(self.temporary.name)
        fake_home = base / "home-is-a-file"
        fake_home.write_text("not a directory", encoding="utf-8")
        project = base / "project"
        with self.assertRaisesRegex(MODULE.ConfigurationError, "必须询问用户是否同意回退"):
            MODULE.resolve_data_root(home=fake_home, cwd=project)
        self.assertFalse((project / MODULE.SKILL_NAME).exists())

    def test_generation_submission_polling_and_download(self):
        task = MODULE.submit_generation(self.root, self.config, self.parameters())
        self.assertEqual(task["task_id"], "task-gen")
        first_request = MockHandler.state["requests"][0]
        self.assertEqual(first_request["path"], "/v1/images/generations/async")
        self.assertEqual(first_request["authorization"], "Bearer test-secret-key")
        self.assertEqual(json.loads(first_request["body"])["model"], "gpt-image-2")

        completed = MODULE.wait_for_task(self.root, self.config, "task-gen", max_wait=2, poll_interval=0.01)
        self.assertEqual(completed["status"], "completed")
        output = Path(completed["output_paths"][0])
        self.assertEqual(output.read_bytes(), b"image-bytes")
        task_text = MODULE.task_file(self.root, "task-gen").read_text(encoding="utf-8")
        self.assertNotIn("test-secret-key", task_text)

    def test_edit_uses_async_multipart_fields(self):
        image = Path(self.temporary.name) / "reference.png"
        mask = Path(self.temporary.name) / "mask.png"
        image.write_bytes(b"reference")
        mask.write_bytes(b"mask")
        task = MODULE.submit_edit(self.root, self.config, self.parameters(), [image], mask)
        self.assertEqual(task["task_id"], "task-edit")
        request = MockHandler.state["requests"][0]
        self.assertEqual(request["path"], "/v1/images/edits/async")
        self.assertIn("multipart/form-data; boundary=", request["content_type"])
        self.assertIn(b'name="image[]"', request["body"])
        self.assertIn(b'name="mask"', request["body"])
        completed = MODULE.wait_for_task(self.root, self.config, "task-edit", max_wait=1, poll_interval=0.01)
        self.assertEqual(Path(completed["output_paths"][0]).read_bytes(), b"edited-image")

    def test_failure_uses_error_message_and_is_persisted(self):
        with self.assertRaisesRegex(MODULE.TaskFailedError, "bad prompt"):
            MODULE.poll_task_once(self.root, self.config, "task-fail")
        task = MODULE.load_task(self.root, "task-fail")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error"], "bad prompt")

    def test_wait_retries_retryable_polling_http_error(self):
        completed = MODULE.wait_for_task(self.root, self.config, "task-retry", max_wait=1, poll_interval=0.01)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(Path(completed["output_paths"][0]).read_bytes(), b"retried")
        self.assertEqual(MockHandler.state["polls"]["task-retry"], 2)

    def test_task_id_is_url_encoded_for_polling(self):
        task = {"api_base_url": self.config["base_url"]}
        with patch.object(MODULE, "request_json", return_value={}) as request:
            MODULE._poll_payload("folder/task?x=1", task, self.config)
        self.assertTrue(request.call_args.args[1].endswith("/images/tasks/folder%2Ftask%3Fx%3D1"))

    def test_api_key_can_be_redacted_from_error_text(self):
        self.assertEqual(
            MODULE._redact_secret("upstream echoed test-secret-key", "test-secret-key"),
            "upstream echoed [REDACTED]",
        )

    def test_config_file_is_private_and_environment_key_is_supported(self):
        config_path = self.root / "config.json"
        MODULE.atomic_write_json(config_path, {**MODULE.DEFAULT_CONFIG, "api_key": "saved"})
        if os.name == "posix":
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        previous = os.environ.get("ASYNC_GPT_IMAGE_API_KEY")
        os.environ["ASYNC_GPT_IMAGE_API_KEY"] = "environment-key"
        try:
            loaded = MODULE.load_config(self.root)
        finally:
            if previous is None:
                os.environ.pop("ASYNC_GPT_IMAGE_API_KEY", None)
            else:
                os.environ["ASYNC_GPT_IMAGE_API_KEY"] = previous
        self.assertEqual(loaded["api_key"], "environment-key")

    def test_codex_can_configure_from_stdin_without_echoing_key(self):
        data_root = Path(self.temporary.name) / "chat-config"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "configure",
                "--from-stdin",
                "--data-dir",
                str(data_root),
                "--json",
            ],
            input="chat-secret-key\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("chat-secret-key", result.stdout)
        saved = json.loads((data_root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "chat-secret-key")
        self.assertEqual(saved["base_url"], "https://edge.1token-store.com")
        self.assertEqual(saved["model"], "gpt-image-2.5")


if __name__ == "__main__":
    unittest.main()
