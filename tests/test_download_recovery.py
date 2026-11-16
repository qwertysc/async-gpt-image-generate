from __future__ import annotations

import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from email.utils import formatdate
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import test_async_gpt_image as existing

MODULE = existing.MODULE


class FakeClock:
    def __init__(self):
        self.now = 1000000.0

    def read(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@contextmanager
def clock():
    value = FakeClock()
    with patch.object(MODULE.time, "time", value.read), patch.object(MODULE.time, "monotonic", value.read), patch.object(MODULE.time, "sleep", value.sleep):
        yield value


def result(*urls):
    return {"status": "completed", "result": {"data": [{"url": u} for u in urls]}}


def transport_error():
    return MODULE.DownloadRequestFailure("SSL EOF", retryable=True, transport=True)


class DownloadRecoveryTests(unittest.TestCase):
    setUpClass = classmethod(existing.AsyncImageTests.setUpClass.__func__)
    tearDownClass = classmethod(existing.AsyncImageTests.tearDownClass.__func__)
    setUp = existing.AsyncImageTests.setUp
    run_cli = existing.AsyncImageTests.run_cli
    parameters = existing.AsyncImageTests.parameters

    def test_auto_recovers_with_direct_download_without_reposting(self):
        before = (self.config_root / "config.json").read_bytes()
        with clock(), patch.object(MODULE, "_uses_proxy", return_value=True), patch.object(MODULE, "_poll_payload", return_value=result("https://images.test/one.png")) as poll:
            with patch.object(MODULE, "_download_http_image", side_effect=[transport_error(), (b"png-data", "image/png")]) as download:
                code, output = self.run_cli(["generate", "--prompt", "cat"])
        self.assertEqual(code, 0)
        self.assertTrue(output["completed"])
        self.assertEqual([c.kwargs["network"] for c in download.call_args_list], ["system", "direct"])
        self.assertEqual(poll.call_count, 1)
        self.assertEqual(sum(r["method"] == "POST" for r in existing.MockHandler.state["requests"]), 1)
        self.assertEqual((self.config_root / "config.json").read_bytes(), before)

    def test_system_policy_is_preserved_in_resume_and_never_switches(self):
        with clock(), patch.object(MODULE, "_uses_proxy", return_value=True), patch.object(MODULE, "_poll_payload", return_value=result("https://images.test/one.png")):
            with patch.object(MODULE, "_download_http_image", side_effect=transport_error()) as download:
                code, output = self.run_cli(["generate", "--prompt", "cat", "--download-network", "system"])
        self.assertEqual(code, MODULE.DownloadError.exit_code)
        self.assertEqual(output["error_type"], "DownloadError")
        self.assertEqual(output["remote_status"], "completed")
        self.assertEqual(output["phase"], "download_failed")
        self.assertEqual(output["error_stage"], "download")
        self.assertEqual(download.call_count, 2)
        self.assertEqual({c.kwargs["network"] for c in download.call_args_list}, {"system"})
        args = output["resume_args"]
        self.assertEqual(args[args.index("--download-network") + 1], "system")

    def test_all_routes_fail_then_manual_resume_only_downloads_cached_result(self):
        with clock() as timer, patch.object(MODULE, "_uses_proxy", return_value=True), patch.object(MODULE, "_poll_payload", return_value=result("https://images.test/one.png")) as poll:
            with patch.object(MODULE, "_download_http_image", side_effect=transport_error()) as download:
                code, output = self.run_cli(["generate", "--prompt", "cat", "--max-wait", "60"])
            self.assertEqual(download.call_count, 3)
            self.assertEqual(code, 6)
            self.assertEqual(poll.call_count, 1)
            timer.sleep(3600)
            with patch.object(MODULE, "_poll_payload", side_effect=AssertionError("cached result must not poll")):
                with patch.object(MODULE, "_download_http_image", return_value=(b"restored", "image/png")):
                    code, restored = self.run_cli(output["resume_args"])
        self.assertEqual(code, 0)
        self.assertTrue(restored["completed"])
        task = MODULE.load_task(self.root, "task-gen")
        self.assertNotIn("error", task)
        self.assertNotIn("last_poll_error", task)
        self.assertEqual(sum(r["method"] == "POST" for r in existing.MockHandler.state["requests"]), 1)

    def test_certificate_and_permission_failures_are_not_retryable(self):
        for reason in (ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"), PermissionError(13, "denied")):
            opener = Mock()
            opener.open.side_effect = URLError(reason)
            with patch.object(MODULE, "build_opener", return_value=opener):
                with self.assertRaises(MODULE.DownloadRequestFailure) as raised:
                    MODULE._download_http_image("https://images.test/a", 10000, 1)
            self.assertFalse(raised.exception.retryable)
            self.assertFalse(raised.exception.transport)

    def test_download_has_no_api_authorization_and_does_not_disable_tls(self):
        response = Mock()
        response.headers = {"Content-Type": "image/png"}
        response.read1.side_effect = [b"image", b""]
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(MODULE, "build_opener", return_value=opener) as builder:
            data, mime = MODULE._download_http_image("https://images.test/a?signature=private", 10000, 1, network="direct")
        self.assertEqual(data, b"image")
        request = opener.open.call_args.args[0]
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(builder.call_args.args[0].proxies, {})
        self.assertEqual(len(builder.call_args.args), 1)

    def test_http_throttling_honors_retry_after_without_route_switch(self):
        with clock() as timer, patch.object(MODULE, "_uses_proxy", return_value=True), patch.object(MODULE, "_poll_payload", return_value=result("https://images.test/a")):
            with patch.object(MODULE, "_download_http_image", side_effect=[MODULE.DownloadRequestFailure("limited", status=429, retryable=True, retry_after=5), (b"image", "image/png")]) as download:
                code, _ = self.run_cli(["generate", "--prompt", "cat", "--max-wait", "20"])
            self.assertGreaterEqual(timer.now, 1000005)
        self.assertEqual(code, 0)
        self.assertEqual([c.kwargs["network"] for c in download.call_args_list], ["system", "system"])

    def test_http_retry_after_accepts_seconds_and_http_date(self):
        with clock() as timer:
            for header in ("7", formatdate(timer.now + 7, usegmt=True)):
                opener = Mock()
                opener.open.side_effect = HTTPError("https://images.test/a", 429, "limited", {"Retry-After": header}, None)
                with patch.object(MODULE, "build_opener", return_value=opener):
                    with self.assertRaises(MODULE.DownloadRequestFailure) as raised:
                        MODULE._download_http_image("https://images.test/a", 10000, 1)
                self.assertEqual(raised.exception.retry_after, 7)
                self.assertFalse(raised.exception.transport)

    def test_expired_url_refreshes_same_task_once(self):
        with clock(), patch.object(MODULE, "_poll_payload", side_effect=[result("https://images.test/expired"), result("https://images.test/fresh")]) as poll:
            with patch.object(MODULE, "_download_http_image", side_effect=[MODULE.DownloadRequestFailure("expired", status=403), (b"image", "image/png")]) as download:
                code, _ = self.run_cli(["generate", "--prompt", "cat"])
        self.assertEqual(code, 0)
        self.assertEqual(poll.call_count, 2)
        self.assertEqual([c.args[0] for c in download.call_args_list], ["https://images.test/expired", "https://images.test/fresh"])
        self.assertEqual(sum(r["method"] == "POST" for r in existing.MockHandler.state["requests"]), 1)

    def test_refresh_failure_is_a_download_error_not_a_polling_loop(self):
        with clock(), patch.object(MODULE, "_poll_payload", side_effect=[result("https://images.test/expired"), MODULE.RequestFailure("refresh unavailable", retryable=True)]) as poll:
            with patch.object(MODULE, "_download_http_image", side_effect=MODULE.DownloadRequestFailure("expired", status=403)):
                code, output = self.run_cli(["generate", "--prompt", "cat"])
        self.assertEqual(code, 6)
        self.assertEqual(output["error_type"], "DownloadError")
        self.assertEqual(output["phase"], "download_failed")
        self.assertEqual(poll.call_count, 2)

    def test_known_completed_legacy_task_does_not_regress_to_generation_on_refresh_error(self):
        task = MODULE._new_task_record("legacy-complete", "generate", self.config, self.parameters())
        task.update(remote_status="completed", status="download_error")
        MODULE.save_task(self.root, task)
        with patch.object(MODULE, "_poll_payload", side_effect=MODULE.RequestFailure("unavailable", retryable=True)) as poll:
            code, output = self.run_cli(["wait", "legacy-complete"])
        self.assertEqual(code, 6)
        self.assertEqual(output["error_type"], "DownloadError")
        self.assertEqual(output["remote_status"], "completed")
        self.assertEqual(output["phase"], "download_failed")
        self.assertEqual(poll.call_count, 1)

    def test_repeated_expired_url_does_not_refresh_forever(self):
        with clock(), patch.object(MODULE, "_poll_payload", return_value=result("https://images.test/expired")) as poll:
            with patch.object(MODULE, "_download_http_image", side_effect=MODULE.DownloadRequestFailure("expired", status=403)) as download:
                code, output = self.run_cli(["generate", "--prompt", "cat"])
        self.assertEqual(code, 6)
        self.assertEqual(poll.call_count, 2)
        self.assertEqual(download.call_count, 2)

    def test_partial_batch_resumes_only_missing_images(self):
        urls = ["https://images.test/one", "https://images.test/two", "https://images.test/three"]
        with clock(), patch.object(MODULE, "_poll_payload", return_value=result(*urls)):
            with patch.object(MODULE, "_download_http_image", side_effect=[(b"first", "image/png"), MODULE.DownloadRequestFailure("denied", status=400)]):
                code, output = self.run_cli(["generate", "--prompt", "cat", "-n", "3"])
            self.assertEqual(code, 6)
            self.assertEqual(len(output["outputs"]), 1)
            first = Path(output["outputs"][0])
            first_stat = first.stat().st_mtime_ns
            with patch.object(MODULE, "_poll_payload", side_effect=AssertionError("no repoll")), patch.object(MODULE, "_download_http_image", return_value=(b"remaining", "image/png")) as download:
                code, restored = self.run_cli(["wait", "task-gen"])
        self.assertEqual(code, 0)
        self.assertEqual([c.args[0] for c in download.call_args_list], urls[1:])
        self.assertEqual(len(restored["outputs"]), 3)
        self.assertEqual(first.stat().st_mtime_ns, first_stat)
        self.assertEqual(first.read_bytes(), b"first")

    def test_short_wait_returns_pending_and_preserves_overall_deadline(self):
        with clock(), patch.object(MODULE, "_poll_payload", return_value={"status": "processing"}):
            code, first = self.run_cli(["generate", "--prompt", "cat", "--yield-after", "30", "--max-wait", "70", "--poll-interval", "5"])
            self.assertEqual(code, 0)
            self.assertTrue(first["yielded"])
            self.assertFalse(first["completed"])
            code, second = self.run_cli(first["resume_args"])
            self.assertEqual(code, 0)
            self.assertEqual(first["wait_deadline_epoch"], second["wait_deadline_epoch"])
            code, third = self.run_cli(second["resume_args"])
            self.assertEqual(code, 5)
            self.assertEqual(third["error_type"], "TaskWaitTimeout")
        self.assertEqual(sum(r["method"] == "POST" for r in existing.MockHandler.state["requests"]), 1)

    def test_short_download_wait_preserves_route_and_attempts(self):
        with clock(), patch.object(MODULE, "_uses_proxy", return_value=True), patch.object(MODULE, "_poll_payload", return_value=result("https://images.test/a")) as poll:
            with patch.object(MODULE, "_download_http_image", side_effect=[transport_error(), (b"restored", "image/png")]) as download:
                code, pending = self.run_cli(["generate", "--prompt", "cat", "--yield-after", "0.5"])
                self.assertEqual(code, 0)
                self.assertTrue(pending["yielded"])
                self.assertEqual(pending["phase"], "downloading")
                # Use a longer slice while preserving the same overall deadline.
                code, restored = self.run_cli(["wait", "task-gen", "--yield-after", "2"])
        self.assertEqual(code, 0)
        self.assertTrue(restored["completed"])
        self.assertEqual(poll.call_count, 1)
        self.assertEqual([c.kwargs["network"] for c in download.call_args_list], ["system", "direct"])

    def test_download_budget_expires_independently_of_generation_wait(self):
        config = {**self.config, "download_budget_seconds": 0.5, "max_wait_seconds": 60}
        MODULE.atomic_write_json(self.config_root / "config.json", config)
        with clock() as timer, patch.object(MODULE, "_poll_payload", return_value=result("https://images.test/a")):
            with patch.object(MODULE, "_download_http_image", side_effect=transport_error()) as download:
                code, output = self.run_cli(["generate", "--prompt", "cat"])
            self.assertLessEqual(timer.now, 1000000.5)
        self.assertEqual(code, 6)
        self.assertEqual(output["error_type"], "DownloadError")
        self.assertEqual(output["remote_status"], "completed")
        self.assertEqual(download.call_count, 1)

    def test_progress_is_throttled_and_does_not_contain_secrets_or_urls(self):
        stderr = io.StringIO()
        with clock() as timer, redirect_stderr(stderr):
            reporter = MODULE.ProgressReporter(25)
            task = {"task_id": "known", "phase": "generating", "error": "secret", "url": "https://x.test/?signature=secret"}
            reporter(task)
            timer.sleep(10)
            reporter(task)
            timer.sleep(15)
            reporter(task)
            task["phase"] = "downloading"
            reporter(task)
        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual(len(events), 3)
        self.assertNotIn("secret", stderr.getvalue())
        self.assertNotIn("https://", stderr.getvalue())

    def test_cli_progress_keeps_stdout_as_one_json_document(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = MODULE.main(["generate", "--prompt", "cat", "--json", "--data-dir", str(self.root), "--config-dir", str(self.config_root)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["completed"])
        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual(events[0]["phase"], "submitted")
        self.assertEqual(events[-1]["phase"], "completed")

    def test_invalid_wait_options_fail_before_paid_submission(self):
        for option in ("--yield-after", "--max-wait", "--poll-interval"):
            code, output = self.run_cli(["generate", "--prompt", "cat", option, "nan"])
            self.assertEqual(code, 2)
        self.assertEqual(existing.MockHandler.state["requests"], [])

    def test_wait_caps_poll_timeout_to_remaining_slice(self):
        with clock() as timer, patch.object(MODULE, "request_json", return_value={"status": "processing"}) as request:
            config = {**self.config, "_call_deadline": timer.now + 0.5}
            MODULE._poll_payload("known", {}, config)
        self.assertLessEqual(request.call_args.kwargs["timeout"], 0.5)

    def test_refresh_caps_api_timeout_to_download_budget(self):
        with clock() as timer, patch.object(MODULE, "request_json", return_value=result("https://images.test/fresh")) as request:
            task = MODULE._new_task_record("refresh", "generate", self.config, self.parameters())
            task["download_state"] = {"deadline_epoch": timer.now + 0.25, "refresh_pending": True}
            MODULE._refresh_result(self.root, task, self.config)
            self.assertLessEqual(request.call_args.kwargs["timeout"], 0.25)

    def test_auto_has_no_direct_fallback_without_an_effective_proxy(self):
        with clock(), patch.object(MODULE, "_uses_proxy", return_value=False), patch.object(MODULE, "_poll_payload", return_value=result("https://images.test/a")):
            with patch.object(MODULE, "_download_http_image", side_effect=transport_error()) as download:
                code, output = self.run_cli(["generate", "--prompt", "cat"])
        self.assertEqual(code, 6)
        self.assertEqual(download.call_count, 2)
        self.assertEqual({c.kwargs["network"] for c in download.call_args_list}, {"system"})

    def test_signed_url_and_proxy_credentials_are_removed_from_errors(self):
        safe = MODULE._safe_error("request https://images.test/a?signature=private via http://user:password@proxy.test:8080 failed")
        self.assertNotIn("signature", safe)
        self.assertNotIn("password", safe)
        self.assertIn("images.test", safe)
        self.assertIn("proxy.test", safe)

    def test_configure_network_policy_preserves_key_and_other_settings(self):
        args = MODULE.build_parser().parse_args(["configure", "--keep-key", "--download-network", "system"])
        MODULE.configure(self.config_root, args)
        saved = MODULE.read_config_file(self.config_root)
        self.assertEqual(saved["download_network"], "system")
        self.assertEqual(saved["api_key"], self.config["api_key"])
        self.assertEqual(saved["model"], self.config["model"])

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"), "macOS sandbox-exec required")
    def test_real_sandbox_allows_writes_only_inside_session_runtime(self):
        skill = self.base / "installed-skill" / "scripts"
        skill.mkdir(parents=True)
        script = skill / "async_gpt_image.py"
        shutil.copy2(existing.SCRIPT_PATH, script)
        before = (self.config_root / "config.json").read_bytes()
        profile = '(version 1) (allow default) (deny file-write*) (allow file-write* (subpath ' + json.dumps(str(self.root)) + '))'
        env = dict(os.environ)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        existing.MockHandler.state["generation_task_id"] = "task-url"
        prefix = ["sandbox-exec", "-p", profile, sys.executable, "-B", str(script)]
        options = ["--data-dir", str(self.root), "--config-dir", str(self.config_root), "--json", "--quiet-progress"]
        submitted = subprocess.run([*prefix, "generate", "--prompt", "cat", "--detach", *options], capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        run = subprocess.run([*prefix, "wait", json.loads(submitted.stdout)["task_id"], *options], capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(json.loads(run.stdout)["completed"])
        self.assertEqual((self.config_root / "config.json").read_bytes(), before)
        self.assertFalse((skill / "__pycache__").exists())
        self.assertEqual({p.name for p in self.config_root.iterdir()}, {"config.json"})
        downloads = [r for r in existing.MockHandler.state["requests"] if r["path"] == "/download/image.png"]
        self.assertEqual(len(downloads), 1)
        self.assertIsNone(downloads[0]["authorization"])


if __name__ == "__main__":
    unittest.main()
