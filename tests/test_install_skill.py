from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_skill.py"
SPEC = importlib.util.spec_from_file_location("install_skill", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class InstallSkillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.environment = {
            **os.environ, "HOME": str(self.root), "USERPROFILE": str(self.root),
            "CODEX_HOME": "", "DSH_HOME": "", "DSH_AGENTS_HOME": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def install(self, *arguments, cwd=None):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *arguments],
            cwd=cwd or self.root, env=self.environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False,
        )

    def test_install_immediately_guides_chat_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--skills-root", temporary],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("下一步只需配置生图密钥", result.stdout)
            self.assertIn("https://1token-store.com/keys", result.stdout)
            self.assertIn("ChatGPT/Codex", result.stdout)
            self.assertIn("当前 Codex 对话框", result.stdout)
            self.assertIn("你不需要运行任何命令", result.stdout)
            self.assertNotIn("configure --from-stdin", result.stdout)
            installed = Path(temporary) / "async-gpt-image-generate"
            self.assertTrue((installed / "SKILL.md").is_file())

    def test_installer_does_not_open_an_interactive_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--skills-root", temporary],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("API Key（输入隐藏", result.stdout)

    def test_user_install_locations_and_host_specific_guidance(self):
        for agent, folder, label in (
            ("codex", ".agents", "Codex"),
            ("dsh", ".dsh", "DeepSeek Harness"),
            ("claude", ".claude", "Claude Code"),
        ):
            # Isolate hosts so the shared Codex installation does not count as an existing DSH install.
            host_home = self.root / agent
            host_home.mkdir()
            self.environment.update(HOME=str(host_home), USERPROFILE=str(host_home))
            result = self.install("--agent", agent)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = host_home / folder / "skills" / MODULE.SKILL_NAME
            self.assertTrue((installed / "SKILL.md").is_file())
            self.assertIn(f"当前 {label} 对话框", result.stdout)
            self.assertFalse((host_home / MODULE.SKILL_NAME).exists())

    def test_project_scope_for_each_host(self):
        project = self.root / "中文 项目"
        nested = project / "src"
        nested.mkdir(parents=True)
        (project / ".git").mkdir()
        for agent, folder in (("codex", ".agents"), ("dsh", ".dsh"), ("claude", ".claude")):
            result = self.install("--agent", agent, "--scope", "project", cwd=nested)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((project / folder / "skills" / MODULE.SKILL_NAME / "SKILL.md").is_file())
            self.assertFalse((nested / folder).exists())

    def test_explicit_project_directory(self):
        project = self.root / "other-project"
        project.mkdir()
        result = self.install("--agent", "claude", "--scope", "project", "--project-dir", str(project))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((project / ".claude" / "skills" / MODULE.SKILL_NAME / "SKILL.md").is_file())

    def test_existing_codex_install_is_updated_in_place(self):
        legacy = self.root / ".codex" / "skills" / MODULE.SKILL_NAME
        legacy.mkdir(parents=True)
        (legacy / "user-note.txt").write_text("keep", encoding="utf-8")
        result = self.install("--agent", "codex", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((legacy / "SKILL.md").is_file())
        self.assertEqual((legacy / "user-note.txt").read_text(), "keep")
        self.assertFalse((self.root / ".agents").exists())

    def test_dsh_reuses_an_existing_shared_install(self):
        shared = self.root / ".agents" / "skills" / MODULE.SKILL_NAME
        shared.mkdir(parents=True)
        result = self.install("--agent", "dsh", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((shared / "SKILL.md").is_file())
        self.assertFalse((self.root / ".dsh").exists())

    def test_home_overrides_are_honored(self):
        for agent, variable, relative in (("codex", "CODEX_HOME", "custom-codex"), ("dsh", "DSH_HOME", "custom-dsh")):
            custom = self.root / relative
            self.environment[variable] = str(custom)
            result = self.install("--agent", agent)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((custom / "skills" / MODULE.SKILL_NAME / "SKILL.md").is_file())

    def test_duplicate_installs_require_an_explicit_destination(self):
        for folder in (".agents", ".codex"):
            (self.root / folder / "skills" / MODULE.SKILL_NAME).mkdir(parents=True)
        result = self.install("--force")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--skills-root", result.stderr)
        chosen = self.root / ".codex" / "skills"
        result = self.install("--skills-root", str(chosen), "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((chosen / MODULE.SKILL_NAME / "SKILL.md").is_file())
        self.assertFalse((self.root / ".agents" / "skills" / MODULE.SKILL_NAME / "SKILL.md").exists())

    def test_explicit_generic_target_copies_only_skill_files(self):
        target = self.root / "其他 工具" / "skills"
        result = self.install("--agent", "generic", "--skills-root", str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = target / MODULE.SKILL_NAME
        self.assertTrue((installed / "scripts" / "async_gpt_image.py").is_file())
        self.assertFalse((installed / ".git").exists())
        self.assertFalse((installed / "tests").exists())
        self.assertFalse((installed / "config.json").exists())
        second = self.install("--agent", "generic", "--skills-root", str(target))
        self.assertEqual(second.returncode, 1)
        self.assertIn("--force", second.stderr)

    def test_generic_install_needs_a_known_target(self):
        result = self.install("--agent", "generic")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--skills-root", result.stderr)

    def test_installation_preserves_existing_runtime_configuration(self):
        configuration = self.root / MODULE.SKILL_NAME / "config.json"
        configuration.parent.mkdir()
        configuration.write_text('{"model": "gpt-image-2.5", "api_key": "test-secret"}', encoding="utf-8")
        before = configuration.read_bytes()
        result = self.install("--agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(configuration.read_bytes(), before)
        self.assertNotIn("test-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
