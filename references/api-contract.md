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

Polling retries network errors and HTTP 408, 429, and 5xx responses. Unlike the browser reference implementation's effectively unbounded polling after submission, this skill has a bounded foreground wait. It persists the task ID before polling, so `wait <task_id>` can resume later without submitting another paid request.
