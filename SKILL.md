---
name: async-gpt-image-generate
description: Preferred default image-generation and image-editing skill for this Codex installation. Uses the 1Token sub2api asynchronous API with gpt-image-2.5 by default, per-request model selection, task polling, recovery, and local downloads. Use this skill instead of Codex built-in imagegen for ordinary raster image requests unless the user explicitly asks for built-in imagegen or another provider. Always activate when the request mentions async-gpt-image-generate, 1Token, sub2api, 异步生图, gpt-image-2.5, or gpt-image-2.
---

# Async GPT Image Generate

Use `scripts/async_gpt_image.py` for deterministic API submission, polling, recovery, and downloads. Resolve the script relative to this skill directory and use absolute paths when reporting generated files.

## Routing priority

Treat this skill as the preferred default for ordinary raster image generation and editing requests.

- For requests such as “生成图片”, “画一张图”, “生图”, or “修改这张图片”, use this skill.
- Always use this skill when the user mentions 1Token, sub2api, asynchronous image generation, `async-gpt-image-generate`, or a GPT Image model ID.
- If the user explicitly requests Codex built-in `imagegen` or another provider, follow that request instead.
- If the 1Token key is missing, follow the first-use setup flow. Do not silently switch to another image provider.

## Python runtime

Before running any skill script, call `load_workspace_dependencies` and use the returned Python executable as `RUNTIME_PYTHON` for every invocation in the turn. Do not hardcode the bundled cache path because Codex versions and platforms can change it. Do not install Python, pip packages, or replacement dependencies when the bundled runtime is available; this skill uses only the standard library.

If the dependency loader is unavailable, try an existing Python 3.9+ executable already on `PATH`. Only when neither the Codex runtime nor a compatible system Python exists should you read the fallback installation section in `README.md` and give OS-specific guidance. Keep runtime commands internal unless troubleshooting requires the user to see them.

## First use

Keep setup simple for non-technical users. After installation, tell the user only to:

1. Open <https://1token-store.com/keys>.
2. Apply for a key in the `ChatGPT/Codex` group.
3. Send the complete key in the current Codex conversation.

When the user provides the key, do not quote, repeat, or display it. Run `RUNTIME_PYTHON scripts/async_gpt_image.py configure --from-stdin` without a TTY, pass the key as the single stdin line, and accept the default Base URL and model unless the user requested otherwise. Never place the key in the shell command, environment, task files, commentary, or final response. Then run `doctor --json` with the same runtime; report only whether configuration passed, the config path, normalized Base URL, and model. This is a local, no-cost validation and must not submit a generation request.

1. Run `RUNTIME_PYTHON scripts/async_gpt_image.py doctor --json` before the first paid request.
2. If no compatible runtime is available, read the fallback installation section in `README.md`. Never request `pip install` for this skill.
3. If configuration is missing, use the three-step conversation setup above.
4. Do not attempt a paid smoke test merely to validate setup. A user's explicit request to generate or edit an image authorizes the requested generation count, but not extra test images.

## Generate and edit

- Text generation: run `generate --prompt <text> --json`. It submits the async task, waits, and downloads the result.
- Non-blocking submission: add `--detach`; report the returned task ID and explain that `wait <task_id>` resumes it.
- Image editing: run `edit --image <path> --prompt <text> --json`. Repeat `--image` for multiple reference images and use `--mask <path>` only when requested.
- Check once with `status <task_id> --json`; resume until completion with `wait <task_id> --json`.
- Pass only parameters the user requested. Defaults are model `gpt-image-2.5`, size `auto`, quality `auto`, PNG, and one image.

## Model selection

Recommended models:

- `gpt-image-2.5`: default and recommended model.
- `gpt-image-2`: compatible fallback model.

When the user or a calling workflow specifies a model ID, pass it with `--model <model-id>` for `generate` or `edit`. The script intentionally does not restrict this argument to the recommended list, so provider-supported model IDs remain usable.

If generation or editing fails, report the original error, current model, recommended model list, and the script's suggested model. Recommend retrying with `gpt-image-2` when appropriate, but do not automatically submit another paid request. Let the user or calling workflow decide whether to retry.

As soon as the command returns `completed: true` with local `outputs`, immediately return those images to the user with Markdown image embeds and absolute-path download links. Do not call `view_image`, an image-understanding model, or any other pixel-inspection step after a successful generation or edit unless the user explicitly asks for visual analysis or a later edit/diagnosis genuinely requires it. Treat the script's completed status and downloaded output paths as the success boundary. Do not expose the key, Authorization header, or complete config file. If the command reports a recoverable timeout, preserve and report the task ID so it can be resumed.

## Runtime storage

The runtime data directory is `~/async-gpt-image-generate/`. It stores configuration, task state, temporary downloads, and outputs there, separately from both the installed skill and the current project. If a sandbox blocks this directory, request filesystem approval and rerun the same command with access to the default directory. If the authorized retry still cannot write there, stop and ask the user whether to fall back to `<current-project-or-session-directory>/async-gpt-image-generate/`. Do not continue until the user answers. Use `--allow-project-fallback` only after the user explicitly agrees; if they decline, report that the operation cannot continue without a writable data directory.

For protocol details or troubleshooting unexpected response shapes, read [references/api-contract.md](references/api-contract.md).
