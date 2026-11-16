# Agent host installation and runtime

Read this when installing, updating, or troubleshooting host integration. The same `SKILL.md` and Python standard-library core are used across hosts; capabilities must be available in the current execution environment.

## Install the complete bundle

Copy `SKILL.md`, `README.md`, `scripts/`, `references/`, and `agents/` together. Do not flatten `SKILL.md` away from its scripts. Resolve script paths relative to the loaded skill, but keep the working/output directory tied to the user's session.

Use `scripts/install_skill.py --agent <codex|dsh|claude>` with a compatible interpreter when available. The default agent is `codex` for old installer callers; another host must select its own target explicitly. Do not infer the current host from which unrelated apps happen to be installed.

Invoke Python as `RUNTIME_PYTHON -B <absolute-script> ...`, including installation and diagnostic imports. The interpreter flag prevents bytecode cache writes in skill installation directories.

`--scope user` is the default. For project installation, use `--scope project --project-dir <absolute-project-root>`. Without `--project-dir`, project scope uses the closest ancestor with a `.git` entry, or the current directory if there is none. DSH's project skill discovery uses that Git-root rule, so its project directory must match that root.

`--skills-root <absolute-parent>` overrides the target directory. Use this for custom deployments and `--agent generic`. `--force` updates the selected installation while preserving unrelated files. The installer does not save API keys or modify any host's own settings.

If Python is not available yet, the host can install the same named files/directories with its native file tools, then resolve the runtime before using the image script. Refresh the skill list after installation; restart only if the skill still does not appear.

## Codex

- New user installations default to `~/.agents/skills/async-gpt-image-generate/`.
- Update an existing copy under `$CODEX_HOME/skills/` or `~/.codex/skills/` in place. An explicit `CODEX_HOME` controls the default Codex installation root when no copy exists.
- Project scope uses `<project>/.agents/skills/async-gpt-image-generate/`.
- If both shared and legacy user locations contain the skill, select `--skills-root` explicitly; do not install another duplicate.
- Use `load_workspace_dependencies` when available and invoke the returned Python path. CLI or other deployments without it can use an existing Python 3.9+.
- `agents/openai.yaml` supplies optional Codex UI text and automatic invocation policy. Core execution does not require that metadata.

Source: [OpenAI skill documentation](https://developers.openai.com/codex/skills).

## DeepSeek Harness (dsh)

- User scope uses `$DSH_HOME/skills/`, defaulting to `~/.dsh/skills/`.
- Shared user skills are also discovered under `$DSH_AGENTS_HOME/skills/`, defaulting to `~/.agents/skills/`. The installer reuses an existing shared installation.
- Project discovery supports `<project-root>/.dsh/skills/` and `<project-root>/.agents/skills/`; the installer selects `.dsh/skills/` for project scope.
- DSH-specific roots take priority over the shared roots. If both user roots already contain the skill, explicitly select which copy to update.
- Supported desktop deployments expose `load_workspace_dependencies`, returning the bundled Python path. Use the tool rather than hardcoding an application or cache location. Other DSH deployments can use an existing Python 3.9+.

These paths and the runtime tool were verified against the installed DeepSeek Harness desktop `0.2.2-rc.3` packages `@deepseek-ai/dsh-skill-filesystem` and `@deepseek-ai/dsh-tool-workspace-dependencies` (their bundled README and source). Custom profiles can change discovery roots or omit tools; use the host's actual capabilities in that case.

## Claude Code desktop and CLI

- Local user scope uses `~/.claude/skills/async-gpt-image-generate/`.
- Project scope uses `<project>/.claude/skills/async-gpt-image-generate/`.
- Keep standard `name` and `description` frontmatter. A local skill can be invoked as `/async-gpt-image-generate` or selected automatically when relevant.
- Detect a Python 3.9+ executable already available to the current session. Do not call unavailable Codex tools or assume a Python runtime is bundled with Claude Code.
- In an SSH, WSL, or container environment, install and run in that environment. Its Home and project paths can differ from the desktop machine.
- Cowork and cloud sessions have separate skill distribution and filesystem contexts; a local folder installation alone does not make this script or its configuration available there.

Sources: [Claude Code skills](https://code.claude.com/docs/en/skills), [Claude Code desktop skills](https://code.claude.com/docs/en/desktop#use-skills).

## Configuration and approvals

All local hosts running as the same OS user can read the shared `~/async-gpt-image-generate/config.json`. Each task writes runtime files to its session's selected directory. No host-specific credential copy is required.

Use the host's normal non-TTY stdin facility to configure a key; do not put it into command text or temporary project files. If that facility is unavailable, use the script's hidden-input local terminal setup instead of interpolating a secret into a shell command.

The skill does not disable sandbox rules, change global filesystem permissions, or grant network/upload access. Normal requests avoid Home writes; explicit configuration may still need a narrow approval from the current host. The runtime loader itself is managed by the host and can prepare its own bundled environment.

## Waiting and deliverables

Use detached submissions and `wait --yield-after 30` with the returned recovery arguments. This works with hosts that only return completed command output. Where background commands are available, inspect progress at short intervals. The script emits progress to stderr; the Agent still needs to provide user-facing updates during long waits. True errors end the automatic loop; do not reset wait budgets or resubmit generation to recover an image.

Honor explicit host `work/` and `outputs/` conventions: runtime files belong under `work/async-gpt-image-generate/` when required, and final images can be copied into the required deliverable directory. Other hosts use the session-local runtime root directly. Never derive runtime directories from the skill installation root.

The download default `auto` can switch from a failing proxy to a direct connection for result images only. If a host or user mandates a proxy, select `--download-network system`; it remains attached to that task and its recovery arguments. All routes remain subject to the host's normal network controls.
