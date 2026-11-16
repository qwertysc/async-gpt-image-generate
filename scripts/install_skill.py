#!/usr/bin/env python3
"""Install the shared skill bundle for Codex, DeepSeek Harness, or Claude Code."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


SKILL_NAME = "async-gpt-image-generate"
AGENT_LABELS = {"codex": "Codex", "dsh": "DeepSeek Harness", "claude": "Claude Code", "generic": "Agent"}


def _existing_or_default(candidates: list[Path], preferred: Path) -> Path:
    existing = list(dict.fromkeys(
        root.expanduser().resolve() for root in candidates if (root.expanduser() / SKILL_NAME).exists()
    ))
    if len(existing) > 1:
        raise RuntimeError("发现多个同名技能安装，请用 --skills-root 指定要更新的父目录：" + ", ".join(map(str, existing)))
    return existing[0] if existing else preferred


def default_skills_root(agent: str = "codex", scope: str = "user", project_dir: Path | None = None) -> Path:
    if agent == "generic":
        raise RuntimeError("其他 Agent 请通过 --skills-root 指定其支持的技能父目录。")
    if scope == "project":
        root = (project_dir or Path.cwd()).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"项目目录不存在：{root}")
        if project_dir is None:
            root = next((path for path in (root, *root.parents) if (path / ".git").exists()), root)
        return root / {"codex": ".agents", "dsh": ".dsh", "claude": ".claude"}[agent] / "skills"
    if agent == "claude":
        return Path.home() / ".claude" / "skills"
    if agent == "dsh":
        dsh_home = Path(os.environ.get("DSH_HOME", "").strip() or Path.home() / ".dsh").expanduser()
        agents_home = Path(os.environ.get("DSH_AGENTS_HOME", "").strip() or Path.home() / ".agents").expanduser()
        return _existing_or_default([dsh_home / "skills", agents_home / "skills"], dsh_home / "skills")
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    legacy = (Path(codex_home).expanduser() if codex_home else Path.home() / ".codex") / "skills"
    shared = Path.home() / ".agents" / "skills"
    return _existing_or_default([shared, legacy], legacy if codex_home else shared)


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


def print_configuration_guide(agent: str) -> None:
    label = AGENT_LABELS[agent]
    print("")
    print("如果尚未配置，下一步只需配置生图密钥：")
    print("1. 打开 https://1token-store.com/keys 申请 API Key。")
    print("2. 申请时选择 ChatGPT/Codex 分组。")
    print(f"3. 回到当前 {label} 对话框，把申请到的完整密钥直接发给 {label}。")
    print(f"{label} 会自动写入配置文件并完成无费用校验，你不需要运行任何命令。")


def main() -> int:
    parser = argparse.ArgumentParser(description="安装 async-gpt-image-generate Agent 技能")
    parser.add_argument("--agent", choices=tuple(AGENT_LABELS), default="codex", help="由当前 Agent 选择安装宿主")
    parser.add_argument("--scope", choices=("user", "project"), default="user", help="用户级或项目级安装")
    parser.add_argument("--project-dir", type=Path, help="项目根目录，仅用于 --scope project")
    parser.add_argument(
        "--skills-root",
        type=Path,
        help="显式指定 skills 父目录，优先于 --agent 和 --scope 的默认位置",
    )
    parser.add_argument("--force", action="store_true", help="更新已有安装")
    args = parser.parse_args()

    if sys.version_info < (3, 9):
        print(
            "当前解释器低于 Python 3.9。宿主提供 load_workspace_dependencies 时请使用其 Python；"
            "否则检查已有 Python 3.9+，仅在都不可用时阅读 README.md 的备用安装说明。",
            file=sys.stderr,
        )
        return 2

    source = Path(__file__).resolve().parent.parent
    try:
        if args.project_dir is not None and args.scope != "project":
            raise RuntimeError("--project-dir 需要与 --scope project 一起使用。")
        skills_root = args.skills_root if args.skills_root is not None else default_skills_root(
            args.agent, args.scope, args.project_dir,
        )
        destination = skills_root.expanduser().resolve() / SKILL_NAME
        if source == destination:
            print(f"技能已经位于安装目录：{destination}")
        else:
            copy_tree(source, destination, args.force)
    except (OSError, RuntimeError) as exc:
        print(f"安装失败：{exc}", file=sys.stderr)
        return 1

    if source != destination:
        print(f"安装完成：{destination}")
    print_configuration_guide(args.agent)
    print("")
    print(f"技能安装完成。配置密钥后，刷新 {AGENT_LABELS[args.agent]} 的技能列表；未显示时再重启。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
