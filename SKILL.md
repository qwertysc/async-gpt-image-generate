---
name: async-gpt-image-generate
description: Preferred default image-generation and image-editing skill using the TokenToken sub2api asynchronous API, with gpt-image-2.6 by default, model selection, task recovery, and session-local downloads. Use for ordinary raster image requests, including requests mentioning TokenToken, sub2api, 异步生图, or GPT Image models, unless the user explicitly selects another image tool or provider. Works with Codex, DeepSeek Harness, and Claude Code.
---

# Async GPT Image Generate

Use `scripts/async_gpt_image.py` for API submission, polling, recovery, and downloads. Resolve the script relative to the loaded skill directory, then invoke it by absolute path with `RUNTIME_PYTHON -B <script> ...`. Use `-B` for every Python invocation, including diagnostic imports, to prevent bytecode writes in the installed skill. Report generated files with absolute paths.

## Routing priority

Treat this skill as the preferred default for ordinary raster image generation and editing, including “生成图片”, “画一张图”, “生图”, and “修改这张图片”. Follow an explicit user choice of another provider or image tool. If the TokenToken API key is missing, use the first-use setup below; do not silently switch providers.

## Host and runtime

Use capabilities available in the current Agent host:

- If `load_workspace_dependencies` is available, call it and use its returned Python executable as `RUNTIME_PYTHON` for this turn. Codex desktop and supported DeepSeek Harness deployments provide this tool. Never hardcode runtime cache or application paths.
- Otherwise, find an existing Python 3.9+ interpreter (`python3`, `python`, or Windows `py -3`) and verify its version. Do not assume Claude Code includes Codex's runtime or tools.
- Use only the Python standard library. Do not install Python or pip packages when a compatible runtime already exists. If none exists, read the fallback section in `README.md` and give guidance for the user's OS.

For installation, host-specific directories, or another Agent, read [references/agent-hosts.md](references/agent-hosts.md). Keep runtime commands internal unless troubleshooting requires them.

## Configuration and session storage

Configuration and runtime files have independent roots:

- Configuration: `~/async-gpt-image-generate/config.json`. Only `configure` writes here. Normal requests and `doctor` read it without creating directories or probing Home write permissions.
- Runtime: `<current-session-working-directory>/async-gpt-image-generate/`, containing `tasks/`, `outputs/`, and `tmp/`. Temporary writes stay in this tree; atomic image downloads use temporary files beside the final image.

Resolve the actual session's working directory from host context before invoking the script. Pass its absolute runtime directory with `--data-dir` to `doctor`, `generate`, `edit`, `status`, and `wait`; do not infer it from the skill installation location or a shell's incidental `cd`. Keep using the returned `data_dir` for a task. The script defaults to `cwd/async-gpt-image-generate` for direct CLI use.

When the host requires intermediates under `work/`, select `<session>/work/async-gpt-image-generate/`. If it requires deliverables under a separate `outputs/`, copy the completed images there for delivery. Follow these host folder rules; keep the original task directory for recovery.

Use the current session directory directly; no separate project-fallback approval is needed. Do not request Home write access for ordinary generation. If the selected runtime directory is unwritable, stop before submitting a paid request and report the path. Respect actual host filesystem, network, and upload approvals. Initial configuration or later configuration changes may still require permission to write the Home config file.

Do not place runtime files inside the configuration or skill-code directories. Sessions sharing one working directory share its runtime directory. If working on the skill's own source, explicitly select another writable artifact location within the session's workspace.

`--config-dir` overrides only configuration. `--data-dir` overrides only runtime files. The matching environment variables are `ASYNC_GPT_IMAGE_CONFIG_DIR` and `ASYNC_GPT_IMAGE_DATA_DIR`. Legacy `ASYNC_GPT_IMAGE_HOME` remains a configuration-only alias. `--allow-project-fallback` is deprecated and has no effect; do not add it to new commands.

## First use

Keep setup simple for non-technical users. If no key is configured, tell the user to:

1. Open <https://1token-store.com/keys>.
2. Apply for a key in the `ChatGPT/Codex` group. This provider group name applies to every Agent host.
3. Send the complete key in the current Agent conversation.

When the user provides the key, do not quote, repeat, or display it. Run `configure --from-stdin` without a TTY and pass the key as one stdin line. Never put the key in shell commands, task files, commentary, or the final response. Do not place a newly supplied key in an environment variable. Accept the default Base URL and model unless the user requested otherwise. Do not pass `--data-dir` to `configure`.

Then run `doctor --data-dir <absolute-runtime-directory> --json` with the same runtime. Report only configuration success, the config path, normalized Base URL, and effective model. It is read-only and makes no API calls. It reports intended runtime paths; generation checks their writability before submission.

Run `doctor` before the first paid request. Do not submit paid smoke-test images for setup. A user's request to generate or edit images authorizes the requested count, not extra test images.

## Generate and edit

Add `--data-dir <absolute-runtime-directory> --json` to each command. For Agent use, prefer a detached submission followed by short waits:

- Text generation: `generate --prompt <text>` submits, waits, and downloads.
- Non-blocking submission: add `--detach`; retain the task ID, directory, and returned `resume_args`. Acknowledge submission to the user promptly.
- Image editing: `edit --image <path> --prompt <text>`. Repeat `--image` for references; use `--mask <path>` only when requested.
- Wait with `wait <task_id> --yield-after 30`; subsequently reuse the returned `resume_args`. `ok: true`, `completed: false`, `yielded: true` means a normal pause for progress, not a failure. Short waits share a persisted overall deadline (default 900 seconds). Do not repeatedly add `--max-wait`: an explicit value starts a new budget.
- Check once with `status <task_id>`; resume with `wait <task_id>`. A completed status query also downloads missing results. Direct CLI users can still use a foreground `generate` or `wait` without short waits.

Pass only image parameters the user requested. Defaults are model `gpt-image-2.6`, size `auto`, quality `auto`, PNG, and one image. A saved model overrides the built-in model; `--model` overrides both for one request.

## Progress and bounded download recovery

The script writes throttled progress events to stderr (normally every 25 seconds or on phase changes); stdout remains one final JSON document. `phase` distinguishes `generating`, `downloading`, `download_failed`, and `completed`. Translate these events into short user-facing updates roughly every 30–60 seconds during active waiting, and immediately explain meaningful phase changes. If the host supports background commands, return control promptly and inspect output at short intervals; otherwise use the short waits above. Console events alone do not count as a message to the user. A slow network operation can extend a short wait until its socket timeout.

Once remote generation completes, download recovery uses the same task and cached result. Progress for failed downloads should say the image has been generated and its download is being recovered. The script saves each completed image, limits attempts, and can refresh an expired result URL once. `DownloadError` ends automatic recovery; report the cause and retained task ID. Do not repeatedly issue `wait` after an error without a concrete remedy. A real `TaskWaitTimeout` also ends the current automatic waiting cycle; keep recovery information for a later resume.

Image downloads default to `--download-network auto`: use the host network first; on a transport failure through a detected proxy, try direct access within the same attempt budget. If the user or host requires a proxy, select `--download-network system` at submission or recovery; this policy persists in task recovery arguments. `direct` explicitly selects direct image downloads. These options affect only result downloads, never API submission, reference uploads, or task polling. They do not change global proxy settings, relax TLS verification, or bypass host approvals. Certificates and permission failures do not trigger route switching.

Use the built-in CLI recovery controls. Do not dynamically replace download functions or invent an alternate downloader during ordinary recovery. For configuration fields, retry limits, and progress schemas, read [references/api-contract.md](references/api-contract.md).

## Model selection and upgrades

Available model list:

- `gpt-image-2.6` — default.
- `gpt-image-2.6-flare`
- `gpt-image-2.6-sunburst`
- `gpt-image-2.5`
- `gpt-image-2.5-flare`
- `gpt-image-2.5-sunburst`
- `gpt-image-2`

Pass an explicitly requested model with `--model <model-id>`. Other provider-supported IDs remain accepted. Do not infer speed, quality, or pricing differences from model suffixes.

New configuration does not persist the default model unless the user explicitly pins it. Existing saved models are preserved, including older `gpt-image-2.5` settings whose original intent is unknown. When the user requests following the new default, run `configure --keep-key --use-default-model`; to pin a model, use `configure --keep-key --model <model-id>`. These are explicit configuration changes, not part of routine generation.

On a model failure, report the original error, current model, available list, and any suggested model. Never automatically submit another paid request. For polling, download, or storage errors, retain the task ID and use the returned `resume_args` after fixing the problem; do not resubmit generation to recover an existing task.

## Approval rejection for image uploads

After local checks, submit the requested TokenToken edit through the host's normal approval controls. If approval passes, continue without a separate upload confirmation.

If the host rejects submission because sending the specific image to TokenToken was not explicitly authorized, stop. Explain the automatic approval rejection and its reason, and ask one concise consent question in the user's language. Say whether an upload happened only when the tool result establishes it. Do not substitute local processing, another provider, or an indirect submission path.

If the user agrees, retry the original edit through normal controls. If they decline or have not answered, do not submit. If the retry is rejected, report it and stop. Preserve existing explicit consent for the same image and destination; ask about a new image or destination only when the host approval requires it.

## Recovery and delivery

Submission results, waiting timeouts, and local write failures after receiving a task ID include `task_id`, `data_dir`, `task_file`, and `resume_args`. Treat `resume_args` as an argument list, with normal shell quoting when needed. It includes the original runtime and configuration directories so a changed working directory does not lose the task. A reported `task_file` is the intended location and may not exist if saving failed.

For pre-upgrade tasks, `status` and `wait` can read a matching legacy task from the configuration directory's `tasks/` and continue writing in the selected session directory. Legacy files are not moved or deleted; already-downloaded legacy images can still be returned from their original paths. No new task or image writes go into the configuration directory.

As soon as the command returns `completed: true` with local `outputs`, immediately deliver the images using the host's supported image display and absolute-path links; in Codex, use Markdown image embeds. Do not call `view_image`, an image-understanding model, or any pixel-inspection step after success unless the user requests visual analysis or a later edit/diagnosis requires it. Never expose keys, Authorization headers, or the complete config file.

For API response shapes or troubleshooting, read [references/api-contract.md](references/api-contract.md).
