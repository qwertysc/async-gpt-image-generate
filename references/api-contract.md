# sub2api asynchronous image contract

This skill follows the built-in `sb2api-async` mapping from GPT Image Playground at commit `da4fda85b59ecacc51d6a1e2ef680e3abb9e29b8`:

- Source mapping: <https://github.com/CookSleep/gpt_image_playground/blob/da4fda85b59ecacc51d6a1e2ef680e3abb9e29b8/src/lib/apiProfiles.ts#L66-L103>
- Request/response test: <https://github.com/CookSleep/gpt_image_playground/blob/da4fda85b59ecacc51d6a1e2ef680e3abb9e29b8/src/lib/api.test.ts#L1481-L1531>

## Endpoints

The configured default `https://edge.1token-store.com` is normalized to `https://edge.1token-store.com/v1`.

| Operation | Method and path | Encoding |
| --- | --- | --- |
| Generate | `POST /images/generations/async` | JSON |
| Edit | `POST /images/edits/async` | multipart/form-data |
| Poll | `GET /images/tasks/{task_id}` | none |

Every API call uses `Authorization: Bearer <api-key>`.

## Submit fields

Generation and edit submission may include:

- `model`
- `prompt`
- `size`
- `quality`
- `output_format`
- `moderation`
- `output_compression` when relevant
- `n`
- `background` when transparent output is requested

Edit requests attach one or more images using the repeated field `image[]`; a mask uses `mask`.

The task ID is read from top-level `task_id`. The first poll occurs immediately and later polls default to three-second intervals.

## Poll states and results

- `status == "completed"`: success.
- `status == "failed"`: terminal failure; read `error.message`.
- Any other status is pending until the configured wait limit.

Successful images are read from `result.data[*].url` or `result.data[*].b64_json`. URL and Base64 results are downloaded into the runtime `outputs/` directory.

Polling retries network errors and HTTP 408, 429, and 5xx responses within a bounded wait. After remote completion, download failures use their own retry policy and produce a download-stage error instead of returning to generation polling. The task ID is persisted before polling, so `wait <task_id>` can resume without submitting another paid request.

## Local storage and recovery contract

Configuration is read from the independently resolved configuration directory, normally `~/async-gpt-image-generate/config.json`. Only explicit `configure` calls write it. The default model is `gpt-image-2.6`; a per-request `--model` overrides a saved model, which overrides the built-in default. The model list is maintained in `RECOMMENDED_MODELS`; other IDs remain accepted.

Runtime files live in `--data-dir`, then `ASYNC_GPT_IMAGE_DATA_DIR`, then `cwd/async-gpt-image-generate/`. Configuration uses `--config-dir`, then `ASYNC_GPT_IMAGE_CONFIG_DIR`, then legacy `ASYNC_GPT_IMAGE_HOME`, then the Home directory above. The legacy environment variable never chooses runtime storage.

Before submission, `tasks/`, `outputs/`, and `tmp/` must pass a local write probe. `doctor` reports paths and validates configuration without writing or making requests. Atomic image files are staged beside the destination under `outputs/`; task JSON is atomically written under `tasks/`.

Success and errors with a known task ID return `task_id`, `data_dir`, `task_file`, `config_path`, and `resume_args`. The latter is an argument array beginning with `wait`, including the original data and configuration directories. `task_file` may be absent if its first write failed. A local write failure after receiving the ID still returns that ID; it never triggers another generation submission.

When `status` or `wait` cannot find a local record, it may read the matching legacy record from the configuration directory's `tasks/` and save a session-local copy. The legacy record and existing images remain unchanged. With no record in either location, polling uses the supplied task ID and current service configuration. Stored outputs are absolute paths; already completed legacy images can be returned without copying them.

## Download policy and limits

These fields are merged as defaults when reading old configuration; normal execution does not rewrite the config:

| Field | Default | Meaning |
| --- | --- | --- |
| `download_network` | `auto` | `auto`, `system`, or `direct`; only result-image GET requests |
| `download_timeout_seconds` | 30 | Per-attempt socket/read budget, shortened by remaining call/stage time |
| `download_budget_seconds` | 120 | Overall download-stage budget, shared across short waits and batch items |
| `download_max_attempts` | 3 | Maximum HTTP attempts per image, range 1–3; at most two per route |
| `progress_interval_seconds` | 25 | Heartbeat interval; phase/attempt/image-count changes can emit immediately |

`--download-network` overrides the configuration for a task; the selected policy is stored with it and included in `resume_args`. `configure --keep-key --download-network system` explicitly changes the persisted default. Use `system` if the host or user requires proxy use. `doctor` reports the effective policy without network requests.

`auto` starts with the environment/system proxy behavior of Python's urllib. A transport error through a detected, non-bypassed proxy may switch the image request to a per-request `ProxyHandler({})` direct opener. There is no global opener change, proxy-setting change, subprocess downloader, forced IPv4, or TLS-verification relaxation. Certificate errors and OS permission errors are terminal; HTTP errors never select another network route. Result downloads contain no API Authorization header.

Retryable HTTP 408, 429, and 5xx responses use bounded backoff and honor `Retry-After` within the stage budget. A result HTTP 401/403/404 can trigger one GET of the original task to refresh its URLs, sharing the same attempt/time budget. Repeated denial ends recovery. Submission POSTs are never automatically repeated.

Task schema 3 records `phase` independently of `remote_status`, caches URL metadata in the private task JSON, and stages decoded inline results under runtime `tmp/`. Each image's `output_path` is saved as soon as it is written. Resuming a cached result skips polling and already-saved images; missing or expired source information can require polling the original task. Inline staging files are removed after complete delivery to the runtime outputs directory.

After retries are exhausted, `DownloadError` exits with code 6, `remote_status: completed`, `phase: download_failed`, `error_stage: download`, any partial `outputs`, and recovery arguments. A manual resume after a remedy starts a new download attempt budget and retains completed images. Storage errors retain their existing exit code and task recovery fields. Do not automatically loop over terminal errors. Successful completion clears active error fields, including earlier polling errors.

## Progress and short waits

CLI stdout with `--json` remains one final JSON document. Stderr emits flushed JSON events with `event`, `task_id`, `phase`, elapsed time for the current invocation, downloaded count, and optional `download_attempt`, `download_route`, and `remote_status`. Events contain no prompts, keys, signed image URLs, or proxy credentials. `--quiet-progress` disables these events; normal `doctor` and `configure` calls emit no progress.

`wait --yield-after 30` (also supported by foreground generate/edit) returns `ok: true`, `completed: false`, `yielded: true` when its slice ends. The task retains `wait_deadline_epoch` and its download counters/deadline. Continuation `resume_args` retain the yield interval and network policy, but do not reset the overall deadline. Omit `--max-wait` on continuation; providing it explicitly starts a new overall wait budget. Network timeouts are shortened by the remaining slice, and reads check their deadline between chunks; OS DNS/socket operations may delay a cooperative yield.

An overall deadline while still generating produces `TaskWaitTimeout` (exit 5). Once remote completion is known, expiration produces a download-stage error (exit 6). Both end the Agent's automatic wait cycle. A later explicit resume can start a fresh wait budget without generating another image. Existing foreground CLI calls without `--yield-after` keep their wait-until-completion behavior.

The Agent should relay meaningful phase changes promptly and give short updates roughly every 30–60 seconds during long waits. Console events are not a substitute for user-facing messages. On completion, deliver local images immediately using the host's required output directory and display mechanism.
