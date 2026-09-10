#!/usr/bin/env python3
"""Install async-gpt-image-generate into the current user's Codex skills directory."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


SKILL_NAME = "async-gpt-image-generate"


def default_skills_root() -> Path:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    return (Path(codex_home).expanduser() if codex_home else Path.home() / ".codex") / "skills"


def copy_tree(source: Path, destination: Path, force: bool) -> None:
    if destination.exists() and not force:
        raise RuntimeError(
            f"目标已存在：{destination}\n如需更新，请重新运行并添加 --force。"
        )
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("SKILL.md", "README.md"):
        shutil.copy2(source / name, destination / name)
    for name in ("agents", "references", "scripts"):
        source_dir = source / name
        destination_dir = destination / name
        shutil.copytree(
            source_dir,
            destination_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )


def print_configuration_guide() -> None:
    print("")
    print("下一步只需配置生图密钥：")
    print("1. 打开 https://1token-store.com/keys 申请 API Key。")
    print("2. 申请时选择 ChatGPT/Codex 分组。")
    print("3. 回到当前 Codex 对话框，把申请到的完整密钥直接发给 Codex。")
    print("Codex 会自动写入配置文件并完成无费用校验，你不需要运行任何命令。")


def main() -> int:
    parser = argparse.ArgumentParser(description="安装 async-gpt-image-generate Codex 技能")
    parser.add_argument(
        "--skills-root",
        type=Path,
        default=default_skills_root(),
        help="Codex skills 父目录，默认 ~/.codex/skills",
    )
    parser.add_argument("--force", action="store_true", help="更新已有安装")
    args = parser.parse_args()

    if sys.version_info < (3, 9):
        print(
            "当前解释器低于 Python 3.9。请让 Codex 使用 load_workspace_dependencies 返回的捆绑 Python；"
            "仅当捆绑运行时不可用时再阅读 README.md 的备用安装说明。",
            file=sys.stderr,
        )
        return 2

    source = Path(__file__).resolve().parent.parent
    destination = args.skills_root.expanduser().resolve() / SKILL_NAME
    try:
        if source == destination:
            print(f"技能已经位于安装目录：{destination}")
        else:
            copy_tree(source, destination, args.force)
    except (OSError, RuntimeError) as exc:
        print(f"安装失败：{exc}", file=sys.stderr)
        return 1

    if source != destination:
        print(f"安装完成：{destination}")
    print_configuration_guide()
    print("")
    print("技能安装完成。配置密钥后，重启 Codex 或重新加载技能列表即可使用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
