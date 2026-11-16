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
            self._json(202, {"task_id": self.state.get("generation_task_id", "task-gen"), "status": "processing"})
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
        if self.path == "/download/image.png":
            body = b"local-http-image"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if task_id == "task-url":
            self._json(200, {
                "task_id": task_id, "status": "completed",
                "result": {"data": [{"url": f"http://127.0.0.1:{self.server.server_address[1]}/download/image.png"}]},
            })
            return
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
        if task_id == "task-pending":
            self._json(200, {"task_id": task_id, "status": "processing"})
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
        self.environment = patch.dict(os.environ, {
            "ASYNC_GPT_IMAGE_HOME": "", "ASYNC_GPT_IMAGE_CONFIG_DIR": "",
            "ASYNC_GPT_IMAGE_DATA_DIR": "", "ASYNC_GPT_IMAGE_API_KEY": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        MockHandler.state["requests"] = []
        MockHandler.state["polls"] = {}
        MockHandler.state["generation_task_id"] = "task-gen"
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = MODULE._prepare_data_root(Path(self.temporary.name) / "data")
        self.config_root = self.base / "home" / MODULE.SKILL_NAME
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
        MODULE.atomic_write_json(self.config_root / "config.json", self.config)

    def run_cli(self, arguments, *, data_root=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = MODULE.main([
                *arguments, "--quiet-progress", "--config-dir", str(self.config_root),
                "--data-dir", str(data_root or self.root), "--json",
            ])
        return code, json.loads(output.getvalue())

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
        self.assertEqual(MODULE.DEFAULT_MODEL, "gpt-image-2.6")
        self.assertEqual(MODULE.DEFAULT_CONFIG["model"], "gpt-image-2.6")
        self.assertEqual(list(MODULE.RECOMMENDED_MODELS), [
            "gpt-image-2.6", "gpt-image-2.6-flare", "gpt-image-2.6-sunburst",
            "gpt-image-2.5", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2",
        ])

        for command in ("generate", "edit"):
            arguments = [command]
            if command == "edit":
                arguments.extend(["--image", "reference.png"])
            for model in (*MODULE.RECOMMENDED_MODELS, "custom-image-model"):
                parsed = MODULE.build_parser().parse_args([*arguments, "--prompt", "draw a cat", "--model", model])
                parameters = MODULE._request_parameters(parsed, self.config)
                self.assertEqual(parameters["model"], model)

    def test_generation_failure_lists_models_without_retrying(self):
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
                        "--config-dir",
                        str(self.config_root),
                        "--json",
                    ]
                )
        self.assertEqual(exit_code, MODULE.RequestFailure.exit_code)
        self.assertEqual(submit.call_count, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["model"], "gpt-image-2.5")
        self.assertEqual(result["recommended_models"], list(MODULE.RECOMMENDED_MODELS))
        self.assertEqual(result["suggested_model"], "gpt-image-2")
        self.assertEqual(
            MODULE.normalize_api_base_url("https://edge.1token-store.com/v1/"),
            "https://edge.1token-store.com/v1",
        )

    def test_default_paths_are_separate_and_resolution_does_not_write(self):
        home, project = self.base / "missing-home", self.base / "missing-project"
        config_root = MODULE.resolve_config_root(home=home)
        data_root = MODULE.resolve_data_root(cwd=project, config_root=config_root)
        self.assertEqual(config_root, home / MODULE.SKILL_NAME)
        self.assertEqual(data_root, project / MODULE.SKILL_NAME)
        self.assertFalse(home.exists())
        self.assertFalse(project.exists())

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

    def test_agent_can_configure_from_stdin_without_echoing_key(self):
        data_root = Path(self.temporary.name) / "chat-config"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT_PATH),
                "configure",
                "--from-stdin",
                "--config-dir",
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
        self.assertNotIn("model", saved)
        self.assertEqual(json.loads(result.stdout)["model"], "gpt-image-2.6")
        self.assertEqual({p.name for p in data_root.iterdir()}, {"config.json"})

    def test_doctor_is_read_only_even_when_directories_are_missing(self):
        new_root = self.base / "never-created" / MODULE.SKILL_NAME
        config_before = (self.config_root / "config.json").read_bytes()
        with patch.object(Path, "mkdir", side_effect=AssertionError("doctor must not create directories")):
            with patch.object(MODULE.tempfile, "NamedTemporaryFile", side_effect=AssertionError("doctor must not probe writes")):
                code, result = self.run_cli(["doctor"], data_root=new_root)
        self.assertEqual(code, 0)
        self.assertFalse(result["data_dir_write_checked"])
        self.assertFalse(new_root.parent.exists())
        self.assertEqual((self.config_root / "config.json").read_bytes(), config_before)
        self.assertEqual(MockHandler.state["requests"], [])

    def test_missing_configuration_does_not_create_home_or_workspace(self):
        config_root = self.base / "absent-config"
        data_root = self.base / "absent-data"
        for command in (["doctor"], ["generate", "--prompt", "cat"]):
            output = io.StringIO()
            with redirect_stdout(output):
                code = MODULE.main([*command, "--config-dir", str(config_root), "--data-dir", str(data_root), "--json"])
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(output.getvalue())["ok"])
        self.assertFalse(config_root.exists())
        self.assertFalse(data_root.exists())
        self.assertEqual(MockHandler.state["requests"], [])

    def test_read_only_home_supports_generation_edit_and_recovery(self):
        config_file = self.config_root / "config.json"
        before = config_file.read_bytes()
        reference = self.base / "参考 图.png"
        reference.write_bytes(b"reference")
        real_write = MODULE.atomic_write_json

        def guard_home(path, value, private=True):
            self.assertNotIn(self.config_root, path.resolve().parents)
            return real_write(path, value, private)

        if os.name == "posix":
            config_file.chmod(0o400)
            self.config_root.chmod(0o500)
        try:
            with patch.object(MODULE, "atomic_write_json", side_effect=guard_home):
                code, submitted = self.run_cli(["generate", "--prompt", "cat", "--detach"])
                self.assertEqual(code, 0)
                code, completed = self.run_cli(["wait", submitted["task_id"], "--poll-interval", "0.01"])
                self.assertEqual(code, 0)
                self.assertTrue(completed["completed"])
                code, edited = self.run_cli(["edit", "--image", str(reference), "--prompt", "snow"])
                self.assertEqual(code, 0)
                self.assertTrue(edited["completed"])
        finally:
            if os.name == "posix":
                self.config_root.chmod(0o700)
                config_file.chmod(0o600)
        self.assertEqual(config_file.read_bytes(), before)
        self.assertEqual({p.name for p in self.config_root.iterdir()}, {"config.json"})
        self.assertFalse((self.root / "config.json").exists())
        for path in completed["outputs"] + edited["outputs"]:
            self.assertTrue(Path(path).is_relative_to(self.root))

    def test_sessions_default_to_their_own_directories(self):
        config_before = (self.config_root / "config.json").read_bytes()
        outputs = []
        for folder in ("会话 A", "会话 B"):
            workspace = self.base / folder
            workspace.mkdir()
            output = io.StringIO()
            with patch.object(Path, "cwd", return_value=workspace), redirect_stdout(output):
                code = MODULE.main([
                    "generate", "--prompt", "cat", "--poll-interval", "0.01",
                    "--config-dir", str(self.config_root), "--json",
                ])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            expected = workspace / MODULE.SKILL_NAME
            self.assertEqual(result["data_dir"], str(expected))
            self.assertTrue(Path(result["task_file"]).is_relative_to(expected))
            self.assertTrue(Path(result["outputs"][0]).is_relative_to(expected))
            outputs.extend(result["outputs"])
            self.assertFalse((expected / "config.json").exists())
        self.assertNotEqual(outputs[0], outputs[1])
        self.assertEqual((self.config_root / "config.json").read_bytes(), config_before)

    def test_explicit_data_dir_works_from_skill_directory(self):
        selected = self.base / "会话 空格" / MODULE.SKILL_NAME
        result = subprocess.run([
            sys.executable, "-B", str(SCRIPT_PATH), "generate", "--prompt", "cat",
            "--config-dir", str(self.config_root), "--data-dir", str(selected),
            "--poll-interval", "0.01", "--json",
        ], cwd=SCRIPT_PATH.parent, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data_dir"], str(selected))
        self.assertTrue(Path(payload["outputs"][0]).is_relative_to(selected))

    def test_resume_arguments_survive_a_changed_working_directory(self):
        code, submitted = self.run_cli(["generate", "--prompt", "cat", "--detach"])
        self.assertEqual(code, 0)
        elsewhere = self.base / "different-session"
        elsewhere.mkdir()
        output = io.StringIO()
        with patch.object(Path, "cwd", return_value=elsewhere), redirect_stdout(output):
            code = MODULE.main(submitted["resume_args"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(result["completed"])
        self.assertEqual(result["data_dir"], str(self.root))
        self.assertFalse((elsewhere / MODULE.SKILL_NAME).exists())
        self.assertEqual(sum(r["method"] == "POST" for r in MockHandler.state["requests"]), 1)

    def test_unwritable_output_directory_prevents_paid_submission(self):
        real_probe = MODULE.tempfile.NamedTemporaryFile

        def probe(*args, **kwargs):
            if Path(kwargs["dir"]).name == "outputs":
                raise PermissionError("read-only outputs")
            return real_probe(*args, **kwargs)

        with patch.object(MODULE.tempfile, "NamedTemporaryFile", side_effect=probe):
            code, result = self.run_cli(["generate", "--prompt", "cat"])
        self.assertEqual(code, 2)
        self.assertIn("outputs", result["error"])
        self.assertEqual(MockHandler.state["requests"], [])

    @unittest.skipUnless(os.name == "posix", "directory symlink creation requires extra privileges on Windows")
    def test_output_symlink_cannot_write_back_into_home(self):
        (self.root / "outputs").rmdir()
        (self.root / "outputs").symlink_to(self.config_root, target_is_directory=True)
        before = {p.name: p.read_bytes() for p in self.config_root.iterdir()}
        code, result = self.run_cli(["generate", "--prompt", "cat"])
        self.assertEqual(code, 2)
        self.assertIn("链接", result["error"])
        self.assertEqual(MockHandler.state["requests"], [])
        self.assertEqual({p.name: p.read_bytes() for p in self.config_root.iterdir()}, before)

    def test_invalid_data_path_does_not_fall_back_to_home(self):
        blocked = self.base / "not-a-directory"
        blocked.write_text("file", encoding="utf-8")
        code, result = self.run_cli(["generate", "--prompt", "cat", "--allow-project-fallback"], data_root=blocked)
        self.assertEqual(code, 2)
        self.assertFalse((self.config_root / "outputs").exists())
        self.assertTrue(result["warnings"])
        self.assertEqual(MockHandler.state["requests"], [])

    def test_config_directory_and_skill_code_cannot_be_runtime_storage(self):
        for root in (self.config_root, self.config_root / "nested", SCRIPT_PATH.parent / "outputs"):
            code, result = self.run_cli(["generate", "--prompt", "cat"], data_root=root)
            self.assertEqual(code, 2)
            self.assertFalse(result["ok"])
        self.assertEqual(MockHandler.state["requests"], [])

    def test_timeout_preserves_structured_recovery_without_reposting(self):
        MockHandler.state["generation_task_id"] = "task-pending"
        code, result = self.run_cli(["generate", "--prompt", "cat", "--max-wait", "0.001", "--poll-interval", "0.001"])
        self.assertEqual(code, MODULE.TaskWaitTimeout.exit_code)
        self.assertEqual(result["task_id"], "task-pending")
        self.assertTrue(Path(result["task_file"]).exists())
        self.assertEqual(result["resume_args"][0:2], ["wait", "task-pending"])
        self.assertNotIn("suggested_model", result)
        self.assertEqual(sum(r["method"] == "POST" for r in MockHandler.state["requests"]), 1)

    def test_storage_failure_after_submission_keeps_task_id(self):
        with patch.object(MODULE, "atomic_write_json", side_effect=PermissionError("disk became read-only")):
            code, result = self.run_cli(["generate", "--prompt", "cat"])
        self.assertEqual(code, MODULE.StorageError.exit_code)
        self.assertEqual(result["task_id"], "task-gen")
        self.assertFalse(Path(result["task_file"]).exists())
        self.assertNotIn("test-secret-key", json.dumps(result))
        output = io.StringIO()
        with redirect_stdout(output):
            code = MODULE.main(result["resume_args"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["completed"])
        self.assertEqual(sum(r["method"] == "POST" for r in MockHandler.state["requests"]), 1)

    def test_download_write_failure_is_recoverable(self):
        with patch.object(MODULE, "_write_image_bytes", side_effect=PermissionError("disk full")):
            code, result = self.run_cli(["generate", "--prompt", "cat", "--poll-interval", "0.01"])
        self.assertEqual(code, MODULE.StorageError.exit_code)
        self.assertEqual(result["task_id"], "task-gen")
        self.assertEqual(MODULE.load_task(self.root, "task-gen")["status"], "download_error")
        code, restored = self.run_cli(["wait", "task-gen"])
        self.assertEqual(code, 0)
        self.assertTrue(restored["completed"])
        self.assertEqual(sum(r["method"] == "POST" for r in MockHandler.state["requests"]), 1)

    def test_legacy_task_is_read_only_and_continues_in_session(self):
        old = MODULE._new_task_record("task-edit", "edit", self.config, self.parameters())
        old["schema_version"] = 2
        old.pop("phase")
        old.pop("download_network")
        old_file = MODULE.task_file(self.config_root, "task-edit")
        MODULE.atomic_write_json(old_file, old)
        before = old_file.read_bytes()
        code, result = self.run_cli(["wait", "task-edit"])
        self.assertEqual(code, 0)
        self.assertTrue(result["completed"])
        self.assertEqual(old_file.read_bytes(), before)
        self.assertFalse((self.config_root / "outputs").exists())
        self.assertTrue(Path(result["outputs"][0]).is_relative_to(self.root))
        saved = MODULE.load_task(self.root, "task-edit")
        self.assertEqual(saved["legacy_task_file"], str(old_file))
        self.assertEqual(saved["data_dir"], str(self.root))
        self.assertEqual(saved["schema_version"], 3)
        self.assertEqual(sum(r["method"] == "POST" for r in MockHandler.state["requests"]), 0)

    def test_legacy_home_environment_is_only_a_config_alias(self):
        project = self.base / "project"
        with patch.dict(os.environ, {"ASYNC_GPT_IMAGE_HOME": str(self.config_root)}):
            self.assertEqual(MODULE.resolve_config_root(), self.config_root)
            self.assertEqual(MODULE.resolve_data_root(cwd=project), project / MODULE.SKILL_NAME)
            output = io.StringIO()
            with redirect_stdout(output):
                code = MODULE.main(["doctor", "--data-dir", str(self.root), "--json"])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output.getvalue())["warnings"])
        self.assertFalse(project.exists())

    def test_explicit_paths_override_new_and_legacy_environment(self):
        with patch.dict(os.environ, {
            "ASYNC_GPT_IMAGE_HOME": str(self.base / "legacy"),
            "ASYNC_GPT_IMAGE_CONFIG_DIR": str(self.base / "new-config"),
            "ASYNC_GPT_IMAGE_DATA_DIR": str(self.base / "new-data"),
        }):
            self.assertEqual(MODULE.resolve_config_root(), self.base / "new-config")
            self.assertEqual(MODULE.resolve_config_root(self.config_root), self.config_root)
            self.assertEqual(MODULE.resolve_data_root(), self.base / "new-data")
            self.assertEqual(MODULE.resolve_data_root(self.root), self.root)

    def test_unpinned_config_uses_new_default_in_api_request(self):
        raw = {key: value for key, value in self.config.items() if key != "model"}
        MODULE.atomic_write_json(self.config_root / "config.json", raw)
        before = (self.config_root / "config.json").read_bytes()
        code, result = self.run_cli(["generate", "--prompt", "cat", "--detach"])
        self.assertEqual(code, 0)
        request = next(r for r in MockHandler.state["requests"] if r["method"] == "POST")
        self.assertEqual(json.loads(request["body"])["model"], "gpt-image-2.6")
        self.assertEqual((self.config_root / "config.json").read_bytes(), before)

    def test_old_model_is_preserved_until_explicitly_reset(self):
        raw = {**self.config, "model": "gpt-image-2.5", "custom_setting": "keep-me"}
        config_file = self.config_root / "config.json"
        MODULE.atomic_write_json(config_file, raw)
        before = config_file.read_bytes()
        code, result = self.run_cli(["doctor"])
        self.assertEqual(code, 0)
        self.assertEqual(result["model"], "gpt-image-2.5")
        self.assertEqual(result["default_model"], "gpt-image-2.6")
        self.assertEqual(result["model_source"], "config")
        self.assertEqual(config_file.read_bytes(), before)
        output = io.StringIO()
        with redirect_stdout(output):
            code = MODULE.main([
                "configure", "--keep-key", "--use-default-model",
                "--config-dir", str(self.config_root), "--json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["model"], "gpt-image-2.6")
        self.assertNotIn("test-secret-key", output.getvalue())
        saved = json.loads(config_file.read_text(encoding="utf-8"))
        self.assertNotIn("model", saved)
        self.assertEqual(saved["api_key"], "test-secret-key")
        self.assertEqual(saved["custom_setting"], "keep-me")

    def test_configuration_can_pin_any_supported_model(self):
        for model in MODULE.RECOMMENDED_MODELS:
            args = MODULE.build_parser().parse_args(["configure", "--keep-key", "--model", model])
            result = MODULE.configure(self.config_root, args)
            self.assertEqual(result["model"], model)
            self.assertEqual(result["model_source"], "config")
            self.assertEqual(MODULE.load_config(self.config_root)["model"], model)

    def test_old_configure_data_dir_fails_with_clear_migration_guidance(self):
        target = self.base / "do-not-place-key-here"
        code, result = self.run_cli(["configure", "--from-stdin"], data_root=target)
        self.assertEqual(code, 2)
        self.assertIn("--config-dir", result["error"])
        self.assertFalse(target.exists())

    def test_stdin_configuration_does_not_save_an_environment_key_by_accident(self):
        args = MODULE.build_parser().parse_args(["configure", "--from-stdin"])
        with patch.dict(os.environ, {"ASYNC_GPT_IMAGE_API_KEY": "environment-secret"}):
            with patch.object(sys, "stdin", io.StringIO("intended-secret\n")):
                MODULE.configure(self.config_root, args)
        saved = json.loads((self.config_root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "intended-secret")

    def test_remote_errors_do_not_persist_an_echoed_api_key(self):
        payload = {"status": "failed", "error": {"message": "upstream echoed test-secret-key"}}
        with patch.object(MODULE, "_poll_payload", return_value=payload):
            code, result = self.run_cli(["status", "task-secret"])
        self.assertEqual(code, MODULE.TaskFailedError.exit_code)
        self.assertNotIn("test-secret-key", json.dumps(result))
        self.assertNotIn("test-secret-key", MODULE.task_file(self.root, "task-secret").read_text())

    def test_polling_error_keeps_recovery_without_model_switch_guidance(self):
        failure = MODULE.RequestFailure("service denied polling", status=403)
        with patch.object(MODULE, "_poll_payload", side_effect=failure):
            code, result = self.run_cli(["wait", "task-known"])
        self.assertEqual(code, MODULE.RequestFailure.exit_code)
        self.assertEqual(result["task_id"], "task-known")
        self.assertNotIn("suggested_model", result)
        self.assertEqual(MockHandler.state["requests"], [])


if __name__ == "__main__":
    unittest.main()
