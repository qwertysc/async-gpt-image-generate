from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_skill.py"


class InstallSkillTests(unittest.TestCase):
    def test_install_immediately_guides_chat_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--skills-root", temporary],
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
                [sys.executable, str(SCRIPT), "--skills-root", temporary],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("API Key（输入隐藏", result.stdout)


if __name__ == "__main__":
    unittest.main()
