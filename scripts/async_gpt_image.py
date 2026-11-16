#!/usr/bin/env python3
"""Submit, poll, recover, and download sub2api asynchronous GPT Image tasks."""

from __future__ import annotations

import argparse
import base64
import binascii
import errno
import getpass
import hashlib
import http.client
import json
import math
import mimetypes
import os
import re
import socket
import ssl
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote_to_bytes, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener, getproxies, proxy_bypass, urlopen


SKILL_NAME = "async-gpt-image-generate"
KEY_URL = "https://1token-store.com/keys"
RECOMMENDED_MODELS = (
    "gpt-image-2.6",
    "gpt-image-2.6-flare",
    "gpt-image-2.6-sunburst",
    "gpt-image-2.5",
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
    "gpt-image-2",
)
DEFAULT_MODEL = RECOMMENDED_MODELS[0]
DEFAULT_CONFIG: Dict[str, Any] = {
    "base_url": "https://edge.1token-store.com",
    "model": DEFAULT_MODEL,
    "poll_interval_seconds": 3.0,
    "request_timeout_seconds": 60.0,
    "max_wait_seconds": 900.0,
    "output_format": "png",
    "max_download_bytes": 100 * 1024 * 1024,
    "download_network": "auto",
    "download_timeout_seconds": 30.0,
    "download_budget_seconds": 120.0,
    "download_max_attempts": 3,
    "progress_interval_seconds": 25.0,
}
RETRYABLE_HTTP_STATUSES = {408, 429}
MAX_API_RESPONSE_BYTES = 160 * 1024 * 1024
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_MASK_BYTES = 50 * 1024 * 1024
USER_AGENT = "async-gpt-image-generate/2.1"


class SkillError(Exception):
    """Expected user-facing error."""

    exit_code = 1


class ConfigurationError(SkillError):
    exit_code = 2


class ProtocolError(SkillError):
    exit_code = 3


class TaskFailedError(SkillError):
    exit_code = 4


class TaskWaitTimeout(SkillError):
    exit_code = 5


class StorageError(SkillError):
    exit_code = 7

    def __init__(self, message: str, *, task_id: str, data_root: Path):
        super().__init__(message)
        self.task_id = task_id
        self.data_root = data_root


class RequestFailure(SkillError):
    exit_code = 6

    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class DownloadError(SkillError):
    """The remote image exists; only local download/recovery failed."""

    exit_code = 6


class DownloadRequestFailure(RequestFailure):
    def __init__(self, message: str, *, status: Optional[int] = None,
                 retryable: bool = False, transport: bool = False, retry_after: float = 0):
        super().__init__(message, status, retryable)
        self.transport = transport
        self.retry_after = retry_after


class WaitSliceFinished(Exception):
    """Cooperative yield; persisted task state is still pending."""


class ProgressReporter:
    def __init__(self, interval: float):
        self.interval = interval
        self.started = time.monotonic()
        self.last_at = float("-inf")
        self.last_state: Any = None

    def __call__(self, task: Mapping[str, Any], *, event: str = "progress") -> None:
        now = time.monotonic()
        state = (task.get("phase"), task.get("download_attempt"), len(task.get("output_paths", [])), event)
        if state == self.last_state and now - self.last_at < self.interval:
            return
        self.last_at, self.last_state = now, state
        value = {"event": event, "task_id": task.get("task_id"), "phase": task.get("phase"),
                 "elapsed_seconds": round(now - self.started, 1),
                 "downloaded": len(task.get("output_paths", []))}
        for key in ("download_attempt", "download_route", "remote_status"):
            if key in task:
                value[key] = task[key]
        print(json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True)


def report_progress(config: Mapping[str, Any], task: Mapping[str, Any], *, event: str = "progress") -> None:
    reporter = config.get("_progress")
    if reporter is not None:
        reporter(task, event=event)


def _safe_error(message: str, secret: str = "") -> str:
    # Signed result URLs and proxy credentials do not belong in diagnostics.
    message = _redact_secret(message, secret)
    def hostname(match: Any) -> str:
        try:
            return urlsplit(match.group()).hostname or "[URL]"
        except ValueError:
            return "[URL]"
    return re.sub(r"https?://[^\s\"'<>]+", hostname, message)


def _remaining(config: Mapping[str, Any]) -> float:
    deadline = config.get("_call_deadline")
    return float("inf") if deadline is None else max(0.0, float(deadline) - time.monotonic())


def _request_timeout(config: Mapping[str, Any]) -> float:
    remaining = _remaining(config)
    if remaining <= 0:
        raise WaitSliceFinished()
    return max(0.001, min(float(config["request_timeout_seconds"]), remaining))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, value: Mapping[str, Any], private: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _prepare_data_root(path: Path) -> Path:
    # Check every destination before a paid submission, including existing folders.
    path = path.resolve()
    try:
        for directory in (path, *(path / name for name in ("tasks", "outputs", "tmp"))):
            resolved = directory.resolve()
            if resolved != path and path not in resolved.parents:
                raise ConfigurationError(f"运行子目录不能通过链接指向产物目录外：{directory}。请使用会话内的目录。")
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".write-test-", dir=str(directory)):
                pass
    except OSError as exc:
        raise ConfigurationError(f"运行目录不可写：{directory}（{exc}）。请使用当前会话内可写的目录。") from exc
    return path.resolve()


def resolve_config_root(explicit: Optional[Path] = None, *, home: Optional[Path] = None) -> Path:
    """Resolve configuration without creating or testing any Home directory."""
    environment_value = (
        os.environ.get("ASYNC_GPT_IMAGE_CONFIG_DIR", "").strip()
        or os.environ.get("ASYNC_GPT_IMAGE_HOME", "").strip()
    )
    selected_home = home if home is not None else Path.home()
    selected = explicit if explicit is not None else (
        Path(environment_value) if environment_value else selected_home / SKILL_NAME
    )
    return selected.expanduser().resolve()


def resolve_data_root(
    explicit: Optional[Path] = None,
    *,
    cwd: Optional[Path] = None,
    config_root: Optional[Path] = None,
) -> Path:
    """Resolve session storage; filesystem preparation is a separate operation."""
    environment_value = os.environ.get("ASYNC_GPT_IMAGE_DATA_DIR", "").strip()
    selected_cwd = (cwd if cwd is not None else Path.cwd()).expanduser().resolve()
    selected = explicit if explicit is not None else (
        Path(environment_value) if environment_value else selected_cwd / SKILL_NAME
    )
    selected = selected.expanduser()
    if not selected.is_absolute():
        selected = selected_cwd / selected
    selected = selected.resolve()
    config_root = (config_root or resolve_config_root()).resolve()
    if selected == config_root or config_root in selected.parents:
        raise ConfigurationError(
            f"运行目录不能位于配置目录 {config_root} 内。请用 --data-dir 指定当前会话目录下的 {SKILL_NAME}/。"
        )
    skill_root = Path(__file__).resolve().parent.parent
    if selected == skill_root or skill_root in selected.parents or (selected / "SKILL.md").is_file():
        raise ConfigurationError("运行目录不能位于技能代码目录内。请用 --data-dir 指定当前会话的产物目录。")
    return selected


def normalize_api_base_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ConfigurationError("Base URL 不能为空。")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(f"无效的 Base URL：{value}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError("Base URL 不应包含账号、密码、查询参数或片段。")
    netloc = parsed.netloc
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def endpoint(base_url: str, path: str) -> str:
    return f"{normalize_api_base_url(base_url).rstrip('/')}/{path.lstrip('/')}"


def validate_config(config: Mapping[str, Any], require_key: bool = True) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(DEFAULT_CONFIG)
    merged.update(config)
    merged["base_url"] = str(merged.get("base_url", "")).strip()
    normalize_api_base_url(merged["base_url"])
    merged["model"] = str(merged.get("model", "")).strip()
    if not merged["model"]:
        raise ConfigurationError("模型名称不能为空。")
    for field, minimum, maximum in (
        ("poll_interval_seconds", 0.1, 300.0),
        ("request_timeout_seconds", 1.0, 600.0),
        ("max_wait_seconds", 1.0, 86400.0),
        ("download_timeout_seconds", 0.1, 600.0),
        ("download_budget_seconds", 0.1, 3600.0),
        ("progress_interval_seconds", 1.0, 60.0),
    ):
        try:
            merged[field] = float(merged[field])
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"配置字段 {field} 必须是数字。") from exc
        if not minimum <= merged[field] <= maximum:
            raise ConfigurationError(f"配置字段 {field} 必须在 {minimum} 到 {maximum} 之间。")
    try:
        merged["max_download_bytes"] = int(merged["max_download_bytes"])
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("配置字段 max_download_bytes 必须是整数。") from exc
    if not 1024 <= merged["max_download_bytes"] <= 1024 * 1024 * 1024:
        raise ConfigurationError("max_download_bytes 必须在 1 KiB 到 1 GiB 之间。")
    if merged.get("output_format") not in {"png", "jpeg", "webp"}:
        raise ConfigurationError("output_format 只能是 png、jpeg 或 webp。")
    if merged.get("download_network") not in {"auto", "system", "direct"}:
        raise ConfigurationError("download_network 只能是 auto、system 或 direct。")
    if (isinstance(merged["download_max_attempts"], bool)
            or not isinstance(merged["download_max_attempts"], int)
            or not 1 <= merged["download_max_attempts"] <= 3):
        raise ConfigurationError("download_max_attempts 必须是 1 到 3 的整数。")

    environment_key = os.environ.get("ASYNC_GPT_IMAGE_API_KEY", "").strip()
    api_key = environment_key or str(merged.get("api_key", "")).strip()
    if require_key and not api_key:
        raise ConfigurationError(
            f"尚未配置 API Key。请访问 {KEY_URL}，选择 ChatGPT/Codex 分组申请密钥，"
            "然后把完整密钥发到当前 Agent 对话框，由 Agent 自动配置并校验。"
        )
    merged["api_key"] = api_key
    return merged


def read_config_file(config_root: Path) -> Dict[str, Any]:
    config_path = config_root / "config.json"
    if not config_path.exists():
        return {}
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"无法读取配置文件 {config_path}：{exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("config.json 顶层必须是 JSON 对象。")
    return value


def load_config(config_root: Path, require_key: bool = True) -> Dict[str, Any]:
    return validate_config(read_config_file(config_root), require_key=require_key)


def configure(config_root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    config_path = config_root / "config.json"
    existing = read_config_file(config_root)
    base_url = args.base_url or str(existing.get("base_url") or DEFAULT_CONFIG["base_url"])
    # Missing model follows the built-in default; an existing model remains pinned.
    pinned_model = None if args.use_default_model else (
        args.model if args.model is not None else existing.get("model")
    )

    if args.from_stdin:
        api_key = sys.stdin.readline().strip()
        if not api_key:
            raise ConfigurationError("没有从标准输入收到 API Key。")
    elif args.from_env:
        api_key = os.environ.get("ASYNC_GPT_IMAGE_API_KEY", "").strip()
        if not api_key:
            raise ConfigurationError("--from-env 需要先设置 ASYNC_GPT_IMAGE_API_KEY。")
    elif args.keep_key:
        api_key = str(existing.get("api_key", "")).strip()
        if not api_key:
            raise ConfigurationError("现有配置没有已保存的密钥，不能使用 --keep-key。请通过标准输入配置密钥。")
    else:
        if not sys.stdin.isatty():
            raise ConfigurationError(
                "交互式配置需要本机终端。请在终端中运行 configure，或设置 "
                "ASYNC_GPT_IMAGE_API_KEY 后运行 configure --from-env。不要把密钥放在命令行参数中。"
            )
        current_key = str(existing.get("api_key", "")).strip()
        prompt = "API Key（输入隐藏"
        if current_key:
            prompt += "，直接回车保留现有密钥"
        api_key = getpass.getpass(prompt + "）：").strip() or current_key
        if not api_key:
            raise ConfigurationError("API Key 不能为空。")
        entered_base_url = input(f"Base URL [{base_url}]：").strip()
        base_url = entered_base_url or base_url
        if args.model is None and not args.use_default_model:
            model_label = str(pinned_model) if pinned_model is not None else f"跟随默认 {DEFAULT_MODEL}"
            entered_model = input(f"模型 [{model_label}]（输入 default 跟随默认）：").strip()
            if entered_model:
                pinned_model = None if entered_model == "default" else entered_model

    value = dict(DEFAULT_CONFIG)
    value.update(existing)
    value.update({"base_url": base_url, "api_key": api_key})
    if args.download_network is not None:
        value["download_network"] = args.download_network
    if pinned_model is None:
        value.pop("model", None)
    else:
        value["model"] = pinned_model
    validated = validate_config(value)
    persisted = dict(validated)
    # Validation can read an environment key, but configuration persists only its chosen input.
    persisted["api_key"] = api_key
    if pinned_model is None:
        persisted.pop("model", None)
    atomic_write_json(config_path, persisted, private=True)
    return {
        "ok": True,
        "configured": True,
        "config_path": str(config_path.resolve()),
        "config_dir": str(config_root),
        "base_url": normalize_api_base_url(validated["base_url"]),
        "model": validated["model"],
        "model_source": "config" if pinned_model is not None else "default",
        "download_network": validated["download_network"],
    }


def _read_limited(response: Any, maximum: int, *, deadline: Optional[float] = None,
                  heartbeat: Any = None) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > maximum:
                raise ProtocolError(f"响应过大，超过 {maximum} 字节限制。")
        except ValueError:
            pass
    chunks: List[bytes] = []
    total = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("读取超过本次时间预算")
        if heartbeat is not None:
            heartbeat()
        # read1 avoids waiting for an entire MiB on slow/chunked responses.
        reader = getattr(response, "read1", response.read)
        chunk = reader(min(1024 * 1024, maximum - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise ProtocolError(f"响应过大，超过 {maximum} 字节限制。")
        chunks.append(chunk)
    return b"".join(chunks)


def _extract_error_message(raw: bytes, status: Optional[int] = None) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = None
    candidates: List[Any] = []
    if isinstance(payload, dict):
        error_value = payload.get("error")
        if isinstance(error_value, dict):
            candidates.append(error_value.get("message"))
        else:
            candidates.append(error_value)
        candidates.extend((payload.get("message"), payload.get("detail")))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    if text:
        return text[:1000]
    return f"HTTP {status}" if status is not None else "网络请求失败"


def _redact_secret(message: str, secret: str) -> str:
    return message.replace(secret, "[REDACTED]") if secret else message


def request_json(
    method: str,
    url: str,
    api_key: str,
    *,
    json_body: Optional[Mapping[str, Any]] = None,
    raw_body: Optional[bytes] = None,
    content_type: Optional[str] = None,
    timeout: float,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    if json_body is not None and raw_body is not None:
        raise ValueError("json_body and raw_body are mutually exclusive")
    body = raw_body
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = _read_limited(response, MAX_API_RESPONSE_BYTES, deadline=deadline)
    except HTTPError as exc:
        try:
            raw = _read_limited(exc, 1024 * 1024)
        except Exception:
            raw = b""
        retryable = exc.code in RETRYABLE_HTTP_STATUSES or exc.code >= 500
        message = _redact_secret(_extract_error_message(raw, exc.code), api_key)
        raise RequestFailure(
            f"API 请求失败（HTTP {exc.code}）：{message}",
            status=exc.code,
            retryable=retryable,
        ) from exc
    except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RequestFailure(f"网络请求失败：{reason}", retryable=True) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("API 返回的不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("API JSON 顶层必须是对象。")
    return payload


def _string_fields(values: Mapping[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            result[key] = "true" if value else "false"
        else:
            result[key] = str(value)
    return result


def build_multipart(fields: Mapping[str, Any], files: Sequence[Tuple[str, Path]]) -> Tuple[bytes, str]:
    total_size = 0
    for field_name, path in files:
        if not path.is_file():
            raise ConfigurationError(f"找不到输入图片：{path}")
        size = path.stat().st_size
        if field_name == "mask" and size > MAX_MASK_BYTES:
            raise ConfigurationError("遮罩文件超过 50 MiB。")
        total_size += size
    if total_size > MAX_INPUT_BYTES:
        raise ConfigurationError("输入图片总大小超过 512 MiB。")

    boundary = "----async-gpt-image-" + uuid.uuid4().hex
    chunks: List[bytes] = []
    for name, value in _string_fields(fields).items():
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    for index, (field_name, path) in enumerate(files, start=1):
        extension = path.suffix.lower() or ".bin"
        filename = ("mask" if field_name == "mask" else f"input-{index}") + extension
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("ascii"),
                f"Content-Type: {mime}\r\n\r\n".encode("ascii"),
                path.read_bytes(),
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def task_file(data_root: Path, task_id: str) -> Path:
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return data_root / "tasks" / f"{digest}.json"


def save_task(data_root: Path, task: Mapping[str, Any]) -> None:
    task_id = str(task.get("task_id", "")).strip()
    if not task_id:
        raise ProtocolError("无法保存没有 task_id 的任务。")
    value = dict(task)
    if value.get("schema_version") in (None, 1, 2):
        value["schema_version"] = 3
    value["updated_at"] = utc_now()
    value["data_dir"] = str(data_root.resolve())
    try:
        atomic_write_json(task_file(data_root, task_id), value, private=True)
    except OSError as exc:
        raise StorageError(
            f"任务 {task_id} 的本地记录保存失败：{exc}。请保留任务 ID，修复目录后使用 wait 恢复，不要重复提交。",
            task_id=task_id,
            data_root=data_root,
        ) from exc


def load_task(data_root: Path, task_id: str) -> Optional[Dict[str, Any]]:
    path = task_file(data_root, task_id)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"任务记录损坏：{path}：{exc}") from exc
    if not isinstance(value, dict) or str(value.get("task_id", "")) != task_id:
        raise ProtocolError(f"任务记录与 task_id 不匹配：{path}")
    return value


def _request_parameters(args: argparse.Namespace, config: Mapping[str, Any]) -> Dict[str, Any]:
    output_format = args.output_format or config["output_format"]
    if args.output_compression is not None and not 0 <= args.output_compression <= 100:
        raise ConfigurationError("output-compression 必须在 0 到 100 之间。")
    if not 1 <= args.n <= 10:
        raise ConfigurationError("n 必须在 1 到 10 之间。")
    result: Dict[str, Any] = {
        "model": args.model or config["model"],
        "prompt": args.prompt,
        "size": args.size,
        "quality": args.quality,
        "output_format": output_format,
        "moderation": args.moderation,
        "n": args.n,
    }
    if args.output_compression is not None:
        result["output_compression"] = args.output_compression
    if args.transparent:
        result["background"] = "transparent"
    return result


def _new_task_record(
    task_id: str,
    operation: str,
    config: Mapping[str, Any],
    parameters: Mapping[str, Any],
    input_files: Optional[Sequence[Path]] = None,
    mask_file: Optional[Path] = None,
) -> Dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": 3,
        "task_id": task_id,
        "operation": operation,
        "status": "submitted",
        "phase": "submitted",
        "download_network": config["download_network"],
        "created_at": now,
        "updated_at": now,
        "api_base_url": normalize_api_base_url(str(config["base_url"])),
        "model": parameters.get("model"),
        "prompt": parameters.get("prompt"),
        "parameters": {key: value for key, value in parameters.items() if key != "prompt"},
        "input_files": [str(path.resolve()) for path in (input_files or [])],
        "mask_file": str(mask_file.resolve()) if mask_file else None,
        "output_paths": [],
    }


def _extract_task_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("task_id")
    task_id = str(value).strip() if value is not None else ""
    if not task_id:
        excerpt = json.dumps(payload, ensure_ascii=False)[:1000]
        raise ProtocolError(f"提交响应缺少 task_id。响应摘要：{excerpt}")
    return task_id


def submit_generation(
    data_root: Path,
    config: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> Dict[str, Any]:
    try:
        payload = request_json(
            "POST",
            endpoint(str(config["base_url"]), "images/generations/async"),
            str(config["api_key"]),
            json_body=parameters,
            timeout=float(config["request_timeout_seconds"]),
        )
    except RequestFailure as exc:
        if exc.retryable:
            raise RequestFailure(
                f"{exc}。提交请求的结果不确定，为避免重复扣费，不会自动重试。请先在服务商后台确认任务。",
                status=exc.status,
                retryable=False,
            ) from exc
        raise
    task_id = _extract_task_id(payload)
    task = _new_task_record(task_id, "generate", config, parameters)
    task["remote_status"] = payload.get("status")
    save_task(data_root, task)
    return task


def submit_edit(
    data_root: Path,
    config: Mapping[str, Any],
    parameters: Mapping[str, Any],
    images: Sequence[Path],
    mask: Optional[Path],
) -> Dict[str, Any]:
    files: List[Tuple[str, Path]] = [("image[]", path.expanduser()) for path in images]
    if mask is not None:
        files.append(("mask", mask.expanduser()))
    body, content_type = build_multipart(parameters, files)
    try:
        payload = request_json(
            "POST",
            endpoint(str(config["base_url"]), "images/edits/async"),
            str(config["api_key"]),
            raw_body=body,
            content_type=content_type,
            timeout=float(config["request_timeout_seconds"]),
        )
    except RequestFailure as exc:
        if exc.retryable:
            raise RequestFailure(
                f"{exc}。提交请求的结果不确定，为避免重复扣费，不会自动重试。请先在服务商后台确认任务。",
                status=exc.status,
                retryable=False,
            ) from exc
        raise
    task_id = _extract_task_id(payload)
    task = _new_task_record(task_id, "edit", config, parameters, images, mask)
    task["remote_status"] = payload.get("status")
    save_task(data_root, task)
    return task


def _poll_payload(task_id: str, task: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
    base_url = str(task.get("api_base_url") or config["base_url"])
    return request_json(
        "GET",
        endpoint(base_url, f"images/tasks/{quote(task_id, safe='')}"),
        str(config["api_key"]),
        timeout=_request_timeout(config),
        deadline=config.get("_call_deadline"),
    )


def _remote_error(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return str(error["message"]).strip() or "异步任务失败"
    if isinstance(error, str) and error.strip():
        return error.strip()
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return "异步任务失败"


def _safe_prefix(task_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", task_id).strip("._-")
    return (cleaned or hashlib.sha256(task_id.encode("utf-8")).hexdigest())[:24]


def _extension_for_mime(mime: str, fallback: str) -> str:
    normalized = mime.split(";", 1)[0].strip().lower()
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(normalized, "jpg" if fallback == "jpeg" else fallback)


def _write_image_bytes(
    output_dir: Path,
    task_id: str,
    index: int,
    data: bytes,
    mime: str,
    fallback_format: str,
) -> Path:
    if not data:
        raise ProtocolError("图片数据为空。")
    extension = _extension_for_mime(mime, fallback_format)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final_path = output_dir / f"{timestamp}_{_safe_prefix(task_id)}_{index:02d}.{extension}"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".image-", dir=str(output_dir))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, final_path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
    return final_path.resolve()


def _decode_data_url(value: str) -> Tuple[bytes, str]:
    header, separator, encoded = value.partition(",")
    if not separator or not header.lower().startswith("data:"):
        raise ProtocolError("无效的 data URL。")
    metadata = header[5:]
    parts = metadata.split(";")
    mime = parts[0] or "application/octet-stream"
    try:
        if any(part.lower() == "base64" for part in parts[1:]):
            return base64.b64decode(encoded, validate=True), mime
        return unquote_to_bytes(encoded), mime
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError("data URL 图片编码无效。") from exc


def _download_http_image(url: str, maximum: int, timeout: float, *, network: str = "system",
                         deadline: Optional[float] = None, heartbeat: Any = None) -> Tuple[bytes, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProtocolError("结果中包含不支持的图片 URL。")
    request = Request(url, headers={"Accept": "image/*", "User-Agent": USER_AGENT})
    opener = build_opener(ProxyHandler({}) if network == "direct" else ProxyHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            mime = response.headers.get("Content-Type", "application/octet-stream")
            data = _read_limited(response, maximum, deadline=deadline, heartbeat=heartbeat)
    except HTTPError as exc:
        headers = exc.headers or {}
        try:
            retry_after = max(0.0, float(headers.get("Retry-After", "0")))
        except (TypeError, ValueError):
            try:
                retry_after = max(0.0, parsedate_to_datetime(headers.get("Retry-After", "")).timestamp() - time.time())
            except (AttributeError, TypeError, ValueError, OverflowError):
                retry_after = 0.0
        if not math.isfinite(retry_after):
            retry_after = 0.0
        raise DownloadRequestFailure(
            f"图片下载失败（HTTP {exc.code}）。", status=exc.code,
            retryable=exc.code in RETRYABLE_HTTP_STATUSES or exc.code >= 500,
            retry_after=retry_after,
        ) from exc
    except (URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        certificate = isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason)
        denied = isinstance(reason, PermissionError) or getattr(reason, "errno", None) in {errno.EACCES, errno.EPERM}
        retryable = not certificate and not denied
        raise DownloadRequestFailure(
            f"图片下载失败：{_safe_error(str(reason))}", retryable=retryable, transport=retryable,
        ) from exc
    return data, mime


def _uses_proxy(url: str) -> bool:
    parsed = urlsplit(url)
    return bool(getproxies().get(parsed.scheme)) and not proxy_bypass(parsed.hostname or "")


def _image_saved(item: Mapping[str, Any]) -> bool:
    path = item.get("output_path")
    return bool(path and Path(str(path)).is_file() and Path(str(path)).stat().st_size > 0)


def _cache_result(data_root: Path, task: Dict[str, Any], payload: Mapping[str, Any],
                  config: Mapping[str, Any]) -> None:
    """Persist the remote result before downloading; inline bytes stay out of task JSON."""
    result = payload.get("result")
    items = result.get("data") if isinstance(result, dict) else None
    if not isinstance(items, list) or not items:
        raise ProtocolError("任务已完成，但响应缺少 result.data 图片列表。")
    old_items = task.get("images", [])
    if old_items and len(old_items) != len(items):
        raise ProtocolError("原任务刷新后的图片数量发生变化，请检查服务端任务结果。")
    images = [dict(old_items[i]) if i < len(old_items) else {"index": i + 1} for i in range(len(items))]
    task.update(remote_status="completed", status="downloading", phase="downloading", images=images)
    save_task(data_root, task)
    maximum = int(config["max_download_bytes"])
    fallback = str(task.get("parameters", {}).get("output_format") or config["output_format"])
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ProtocolError("result.data 中包含无效的图片记录。")
        cached = images[index - 1]
        if _image_saved(cached):
            continue
        data: Optional[bytes] = None
        mime = f"image/{'jpeg' if fallback == 'jpeg' else fallback}"
        b64_value = item.get("b64_json")
        url_value = item.get("url")
        if isinstance(b64_value, str) and b64_value.strip():
            try:
                data = base64.b64decode(b64_value, validate=True)
            except binascii.Error as exc:
                raise ProtocolError("result.data 中的 b64_json 无效。") from exc
        elif isinstance(url_value, str) and url_value.startswith("data:"):
            data, mime = _decode_data_url(url_value)
        elif isinstance(url_value, str) and url_value.strip():
            cached["url"] = url_value
            save_task(data_root, task)
            continue
        if data is None:
            raise ProtocolError("任务已完成，但 result.data 中没有可识别的 url 或 b64_json。")
        if len(data) > maximum:
            raise ProtocolError(f"图片超过 {maximum} 字节下载限制。")
        cached["source_path"] = str(_write_image_bytes(data_root / "tmp", str(task["task_id"]), index, data, mime, fallback))
        cached["mime"] = mime
        save_task(data_root, task)


def _refresh_result(data_root: Path, task: Dict[str, Any], config: Mapping[str, Any]) -> None:
    state = task["download_state"]
    budget = state["deadline_epoch"] - time.time()
    if budget <= 0:
        raise DownloadError("刷新图片地址前，下载恢复时间预算已经用完。")
    active_config = dict(config, _call_deadline=time.monotonic() + min(budget, _remaining(config)))
    payload = _poll_payload(str(task["task_id"]), task, active_config)
    if payload.get("status") != "completed":
        raise DownloadError("刷新图片地址时，服务端未返回原任务的完成结果。")
    _cache_result(data_root, task, payload, config)
    state["refresh_pending"] = False
    save_task(data_root, task)


def download_results(data_root: Path, task: Dict[str, Any], config: Mapping[str, Any]) -> None:
    policy = str(task.get("download_network") or config["download_network"])
    state = task.get("download_state")
    if not state:
        state = {"deadline_epoch": time.time() + float(config["download_budget_seconds"]),
                 "attempts": {}, "routes": {}, "next_route": {}, "refreshes": 0}
        task["download_state"] = state
        save_task(data_root, task)
    if state.get("refresh_pending"):
        _refresh_result(data_root, task, config)
    index = 0
    while index < len(task["images"]):
        item = task["images"][index]
        key = str(index + 1)
        if _image_saved(item):
            index += 1
            continue
        budget = state["deadline_epoch"] - time.time()
        if budget <= 0:
            raise DownloadError("图片已生成，但下载恢复已达到时间上限；请使用原任务的 resume_args 恢复。")
        if _remaining(config) <= 0:
            raise WaitSliceFinished()
        next_attempt = float(state.get("next_attempt_epoch", 0)) - time.time()
        if next_attempt > 0:
            report_progress(config, task, event="download_retry")
            time.sleep(min(next_attempt, budget, _remaining(config), 1.0))
            continue
        maximum = int(config["max_download_bytes"])
        if item.get("source_path"):
            with Path(item["source_path"]).open("rb") as source:
                data = source.read(maximum + 1)
            if len(data) > maximum:
                raise ProtocolError("缓存图片超过下载大小限制。")
            mime = str(item["mime"])
        else:
            attempts = state["attempts"].get(key, 0)
            route = state["next_route"].get(key, "direct" if policy == "direct" else "system")
            routes = state["routes"].setdefault(key, {})
            if attempts >= int(config["download_max_attempts"]) or routes.get(route, 0) >= 2:
                raise DownloadError(str(state.get("last_error") or "图片下载重试次数已用完。"))
            state["attempts"][key] = attempts + 1
            routes[route] = routes.get(route, 0) + 1
            task.update(download_attempt=attempts + 1, download_route=route)
            save_task(data_root, task)
            report_progress(config, task)
            timeout = min(float(config["download_timeout_seconds"]), budget, _remaining(config))
            try:
                data, mime = _download_http_image(
                    str(item["url"]), maximum, max(0.001, timeout), network=route,
                    deadline=time.monotonic() + timeout,
                    heartbeat=lambda: report_progress(config, task),
                )
            except DownloadRequestFailure as exc:
                state["last_error"] = _safe_error(str(exc), str(config["api_key"]))
                if exc.status in {401, 403, 404} and state["refreshes"] == 0:
                    state.update(refreshes=1, refresh_pending=True)
                    save_task(data_root, task)
                    _refresh_result(data_root, task, config)
                elif not exc.retryable:
                    raise DownloadError(state["last_error"]) from exc
                elif policy == "auto" and route == "system" and exc.transport and _uses_proxy(str(item["url"])):
                    state["next_route"][key] = "direct"
                # Ordinary HTTP errors never switch network routes.
                next_route = state["next_route"].get(key, route)
                if state["attempts"][key] >= int(config["download_max_attempts"]) or routes.get(next_route, 0) >= 2:
                    raise DownloadError(state["last_error"]) from exc
                state["next_attempt_epoch"] = time.time() + max(exc.retry_after, min(2 ** attempts, 4))
                save_task(data_root, task)
                report_progress(config, task, event="download_retry")
                continue
        fallback = str(task.get("parameters", {}).get("output_format") or config["output_format"])
        item["output_path"] = str(_write_image_bytes(data_root / "outputs", str(task["task_id"]), index + 1, data, mime, fallback))
        task["output_paths"] = [image["output_path"] for image in task["images"] if _image_saved(image)]
        save_task(data_root, task)
        report_progress(config, task)
        index += 1
    task.update(status="completed", phase="completed", raw_image_urls=[i["url"] for i in task["images"] if i.get("url")])
    for key in ("error", "error_stage", "last_poll_error", "download_state", "wait_deadline_epoch", "wait_yielded"):
        task.pop(key, None)
    save_task(data_root, task)
    for item in task["images"]:
        if item.get("source_path"):
            try:
                source = Path(item["source_path"]).resolve()
                if (data_root / "tmp").resolve() in source.parents:
                    source.unlink(missing_ok=True)
            except OSError:
                pass
    report_progress(config, task)


def _recovery_info(task_id: str, data_root: Path, config_root: Path,
                   task: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    result = {
        "task_id": task_id,
        "data_dir": str(data_root),
        "task_file": str(task_file(data_root, task_id)),
        "config_path": str(config_root / "config.json"),
        "resume_args": [
            "wait", task_id, "--data-dir", str(data_root),
            "--config-dir", str(config_root), "--json",
        ],
    }
    if task:
        if task.get("download_network"):
            result["resume_args"].extend(["--download-network", task["download_network"]])
        if task.get("wait_yield_after"):
            result["resume_args"].extend(["--yield-after", str(task["wait_yield_after"])])
    return result


def _task_output(task: Mapping[str, Any], data_root: Path, config_root: Path) -> Dict[str, Any]:
    return {
        **_recovery_info(str(task["task_id"]), data_root, config_root, task),
        "ok": True,
        "completed": task.get("status") == "completed",
        "status": task.get("status"),
        "remote_status": task.get("remote_status"),
        "outputs": task.get("output_paths", []),
        "raw_image_urls": task.get("raw_image_urls", []),
        "error": task.get("error"),
        "phase": task.get("phase"),
        "yielded": bool(task.get("wait_yielded")),
        "wait_deadline_epoch": task.get("wait_deadline_epoch"),
    }


def _load_or_recover_task(
    data_root: Path,
    task_id: str,
    config: Mapping[str, Any],
    *,
    legacy_root: Optional[Path] = None,
) -> Dict[str, Any]:
    task = load_task(data_root, task_id)
    if task is not None:
        return task
    if legacy_root is not None and legacy_root != data_root:
        task = load_task(legacy_root, task_id)
        if task is not None:
            task["legacy_task_file"] = str(task_file(legacy_root, task_id))
            save_task(data_root, task)
            return task
    now = utc_now()
    task = {
        "schema_version": 3,
        "task_id": task_id,
        "operation": "recovered",
        "status": "recovered",
        "phase": "generating",
        "download_network": config["download_network"],
        "created_at": now,
        "updated_at": now,
        "api_base_url": normalize_api_base_url(str(config["base_url"])),
        "model": config["model"],
        "parameters": {"output_format": config["output_format"]},
        "output_paths": [],
    }
    save_task(data_root, task)
    return task


def poll_task_once(data_root: Path, config: Mapping[str, Any], task_id: str) -> Dict[str, Any]:
    task = _load_or_recover_task(data_root, task_id, config)
    existing_outputs = [str(path) for path in task.get("output_paths", []) if Path(str(path)).is_file()]
    if (task.get("status") == "completed" and existing_outputs
            and len(existing_outputs) == len(task.get("output_paths", []))
            and (not task.get("images") or all(_image_saved(i) for i in task["images"]))):
        task["output_paths"] = existing_outputs
        task["phase"] = "completed"
        for key in ("last_poll_error", "error", "error_stage", "wait_yielded", "wait_deadline_epoch"):
            task.pop(key, None)
        save_task(data_root, task)
        report_progress(config, task)
        return task
    if task.get("status") in {"download_error", "wait_timeout"}:
        task.pop("download_state", None)
    cached = bool(task.get("images")) and all(
        _image_saved(i) or i.get("url") or (i.get("source_path") and Path(i["source_path"]).is_file())
        for i in task.get("images", [])
    )
    payload = None
    if not cached:
        task["phase"] = "downloading" if task.get("remote_status") == "completed" else "generating"
        save_task(data_root, task)
        report_progress(config, task)
        try:
            payload = _poll_payload(task_id, task, config)
        except SkillError as exc:
            if task.get("remote_status") != "completed":
                raise
            task.update(status="download_error", phase="download_failed", error_stage="download")
            task["error"] = _safe_error(str(exc), str(config["api_key"]))
            task.pop("wait_deadline_epoch", None)
            save_task(data_root, task)
            raise DownloadError(task["error"]) from exc
        task["remote_status"] = str(payload.get("status", "")).strip()
        if task["remote_status"] == "failed":
            task.update(status="failed", phase="failed", error_stage="generation")
            task["error"] = _safe_error(_remote_error(payload), str(config["api_key"]))
            save_task(data_root, task)
            raise TaskFailedError(f"任务 {task_id} 失败：{task['error']}")
    if cached or task.get("remote_status") == "completed":
        task.update(status="downloading", phase="downloading", remote_status="completed")
        task.setdefault("download_network", config["download_network"])
        save_task(data_root, task)
        report_progress(config, task)
        try:
            if payload is not None:
                _cache_result(data_root, task, payload, config)
            download_results(data_root, task, config)
        except (SkillError, OSError) as exc:
            task.update(status="download_error", phase="download_failed", error_stage="download")
            task["error"] = _safe_error(str(exc), str(config["api_key"]))
            task.pop("wait_deadline_epoch", None)
            save_task(data_root, task)
            report_progress(config, task)
            if isinstance(exc, RequestFailure):
                raise DownloadError(task["error"]) from exc
            raise
        return task
    task.update(status="processing", phase="generating")
    task.pop("error", None)
    task.pop("last_poll_error", None)
    save_task(data_root, task)
    report_progress(config, task)
    return task


def wait_for_task(
    data_root: Path,
    config: Mapping[str, Any],
    task_id: str,
    *,
    max_wait: Optional[float] = None,
    poll_interval: Optional[float] = None,
    yield_after: Optional[float] = None,
) -> Dict[str, Any]:
    maximum = float(max_wait if max_wait is not None else config["max_wait_seconds"])
    interval = float(poll_interval if poll_interval is not None else config["poll_interval_seconds"])
    if not all(math.isfinite(v) and v > 0 for v in (maximum, interval)):
        raise ConfigurationError("max-wait 和 poll-interval 必须大于 0。")
    if yield_after is not None and (not math.isfinite(yield_after) or yield_after <= 0):
        raise ConfigurationError("yield-after 必须是大于 0 的有限秒数。")
    task = _load_or_recover_task(data_root, task_id, config)
    deadline = float(task.get("wait_deadline_epoch", time.time() + maximum))
    if max_wait is not None:
        deadline = time.time() + maximum
    task["wait_deadline_epoch"] = deadline
    task.pop("wait_yielded", None)
    if yield_after is not None:
        task["wait_yield_after"] = yield_after
    save_task(data_root, task)
    call_budget = max(0.0, deadline - time.time())
    if yield_after is not None:
        call_budget = min(call_budget, yield_after)
    active_config = dict(config, _call_deadline=time.monotonic() + call_budget)
    while True:
        task = _load_or_recover_task(data_root, task_id, config)
        if time.time() >= deadline:
            task.pop("wait_deadline_epoch", None)
            if task.get("remote_status") == "completed":
                task.update(status="download_error", phase="download_failed", error_stage="download")
                task["error"] = "图片已生成，但本次下载等待已超时；请使用原任务恢复下载。"
                save_task(data_root, task)
                raise DownloadError(task["error"])
            task.update(status="wait_timeout", phase="generating", error_stage="poll")
            task["error"] = f"等待超过本轮上限；任务仍可通过 wait {task_id} 恢复。"
            if task.get("last_poll_error"):
                task["error"] += f" 最近一次轮询错误：{task['last_poll_error']}"
            save_task(data_root, task)
            raise TaskWaitTimeout(task["error"])
        if _remaining(active_config) <= 0:
            task["wait_yielded"] = True
            save_task(data_root, task)
            report_progress(config, task)
            return task
        try:
            task = poll_task_once(data_root, active_config, task_id)
            if task.get("status") == "completed":
                return task
        except WaitSliceFinished:
            continue
        except RequestFailure as exc:
            if not exc.retryable:
                task = _load_or_recover_task(data_root, task_id, config)
                task.pop("wait_deadline_epoch", None)
                task.update(status="poll_error", phase="generating", error_stage="poll")
                task["error"] = _safe_error(str(exc), str(config["api_key"]))
                save_task(data_root, task)
                raise
            task = _load_or_recover_task(data_root, task_id, config)
            task.update(status="processing", phase="generating", error_stage="poll")
            task["last_poll_error"] = _safe_error(str(exc), str(config["api_key"]))
            save_task(data_root, task)
        report_progress(config, task)
        time.sleep(min(interval, _remaining(active_config), max(0.0, deadline - time.time())))


def _emit(value: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    for key, item in value.items():
        if item is None or item == []:
            continue
        label = key.replace("_", " ")
        if isinstance(item, list):
            print(f"{label}:")
            for entry in item:
                print(f"  - {entry}")
        else:
            print(f"{label}: {item}")


def _model_failure_guidance(current_model: str) -> Dict[str, Any]:
    model = current_model.strip()
    suggested_model = "gpt-image-2" if model != "gpt-image-2" else None
    hint = "可指定其他模型重新运行。"
    if suggested_model:
        hint += f"建议降级使用 {suggested_model}。"
    return {
        "model": model,
        "recommended_models": list(RECOMMENDED_MODELS),
        "suggested_model": suggested_model,
        "hint": hint,
    }


def add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path, help="任务和图片目录，默认当前会话工作目录下的 async-gpt-image-generate")
    parser.add_argument("--config-dir", type=Path, help="独立配置目录，默认 ~/async-gpt-image-generate")
    parser.add_argument(
        "--allow-project-fallback",
        action="store_true",
        help="已弃用：兼容接受但不改变路径；运行文件默认已位于当前会话目录",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--download-network", choices=("auto", "system", "direct"),
                        help="仅控制图片下载；system 固定遵守宿主代理，auto 可对连接错误尝试直连")
    parser.add_argument("--quiet-progress", action="store_true", help="不向 stderr 输出阶段进度")


def add_image_parameters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", required=True, help="完整图片提示词")
    parser.add_argument("--model", help="覆盖配置中的模型")
    parser.add_argument("--size", default="auto")
    parser.add_argument("--quality", choices=("auto", "low", "medium", "high", "xhigh", "max"), default="auto")
    parser.add_argument("--output-format", choices=("png", "jpeg", "webp"))
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--moderation", choices=("auto", "low"), default="auto")
    parser.add_argument("-n", type=int, default=1, help="生成图片数量，1-10")
    parser.add_argument("--transparent", action="store_true", help="请求透明背景")
    parser.add_argument("--detach", action="store_true", help="提交后立即返回 task_id")
    parser.add_argument("--max-wait", type=float, help="本次前台等待上限秒数")
    parser.add_argument("--poll-interval", type=float, help="本次轮询间隔秒数")
    parser.add_argument("--yield-after", type=float, help="短时等待后返回未完成状态，整体等待预算保留")
    add_runtime_args(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="async_gpt_image.py",
        description="TokenToken sub2api 异步 GPT 图片生成和编辑工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="检查运行环境和配置")
    doctor_parser.add_argument("--mainland-help", action="store_true", help="显示中国大陆安装和镜像提示")
    add_runtime_args(doctor_parser)

    configure_parser = subparsers.add_parser("configure", help="安全配置 API Key")
    configure_parser.add_argument("--base-url", help="覆盖默认 Base URL")
    model_source = configure_parser.add_mutually_exclusive_group()
    model_source.add_argument("--model", help="固定使用此模型")
    model_source.add_argument("--use-default-model", action="store_true", help="移除固定模型，跟随内置默认模型")
    key_source = configure_parser.add_mutually_exclusive_group()
    key_source.add_argument("--from-env", action="store_true", help="从 ASYNC_GPT_IMAGE_API_KEY 读取密钥")
    key_source.add_argument(
        "--from-stdin",
        action="store_true",
        help="从标准输入读取一行密钥，供 Agent 自动配置使用",
    )
    key_source.add_argument("--keep-key", action="store_true", help="保留已保存的密钥，只更新其他配置")
    add_runtime_args(configure_parser)

    generate_parser = subparsers.add_parser("generate", help="异步文本生图")
    add_image_parameters(generate_parser)

    edit_parser = subparsers.add_parser("edit", help="异步图片编辑")
    edit_parser.add_argument("--image", type=Path, action="append", required=True, help="参考图片，可重复")
    edit_parser.add_argument("--mask", type=Path, help="可选遮罩图片")
    add_image_parameters(edit_parser)

    status_parser = subparsers.add_parser("status", help="单次查询任务状态")
    status_parser.add_argument("task_id")
    add_runtime_args(status_parser)

    wait_parser = subparsers.add_parser("wait", help="持续等待已有任务")
    wait_parser.add_argument("task_id")
    wait_parser.add_argument("--max-wait", type=float)
    wait_parser.add_argument("--poll-interval", type=float)
    wait_parser.add_argument("--yield-after", type=float)
    add_runtime_args(wait_parser)
    return parser


def doctor(config_root: Path, data_root: Path, show_mainland_help: bool) -> Dict[str, Any]:
    """Local read-only check: no mkdir, write probe, configuration update, or HTTP."""
    config_path = config_root / "config.json"
    configured = False
    config_error: Optional[str] = None
    base_url = normalize_api_base_url(DEFAULT_CONFIG["base_url"])
    model = DEFAULT_CONFIG["model"]
    model_source = "default"
    download_network = DEFAULT_CONFIG["download_network"]
    try:
        raw_config = read_config_file(config_root)
        config = validate_config(raw_config, require_key=True)
        configured = True
        base_url = normalize_api_base_url(str(config["base_url"]))
        model = str(config["model"])
        model_source = "config" if "model" in raw_config else "default"
        download_network = config["download_network"]
    except ConfigurationError as exc:
        config_error = str(exc)
    result: Dict[str, Any] = {
        "ok": configured and sys.version_info >= (3, 9),
        "python": sys.version.split()[0],
        "python_supported": sys.version_info >= (3, 9),
        "configured": configured,
        "config_path": str(config_path.resolve()),
        "data_dir": str(data_root),
        "config_dir": str(config_root),
        "data_dir_write_checked": False,
        "base_url": base_url,
        "model": model,
        "model_source": model_source,
        "download_network": download_network,
        "default_model": DEFAULT_MODEL,
        "recommended_models": list(RECOMMENDED_MODELS),
        "model_note": (
            f"现有配置固定为 {model}。如需跟随默认 {DEFAULT_MODEL}，运行 configure --keep-key --use-default-model。"
            if model_source == "config" and model != DEFAULT_MODEL else None
        ),
        "config_error": config_error,
        "key_help": f"在 {KEY_URL} 申请密钥并选择 ChatGPT/Codex 分组。" if not configured else None,
    }
    if show_mainland_help:
        result["mainland_china_help"] = [
            "本技能仅使用 Python 标准库，不需要 pip 安装依赖。",
            "优先使用宿主提供的 Python，或系统已有的 Python 3.9+。",
            "Windows/macOS 确实缺少解释器时，可使用 https://www.python.org/downloads/。",
            "Linux 可通过系统软件源安装 Python 3；不要执行来源不明的一键脚本。",
            "运行时与各宿主安装方式见技能 README.md。",
        ]
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    failure_model: Optional[str] = None
    data_root: Optional[Path] = None
    config_root: Optional[Path] = None
    config: Dict[str, Any] = {}
    current_task_id: Optional[str] = getattr(args, "task_id", None)
    warnings: List[str] = []
    if args.allow_project_fallback:
        warnings.append("--allow-project-fallback 已弃用且不再改变路径；运行文件默认保存在当前会话目录。")
    if os.environ.get("ASYNC_GPT_IMAGE_HOME", "").strip() and not (
        args.config_dir is not None or os.environ.get("ASYNC_GPT_IMAGE_CONFIG_DIR", "").strip()
    ):
        warnings.append("ASYNC_GPT_IMAGE_HOME 仅作为配置目录的兼容别名；运行目录请用 --data-dir 或 ASYNC_GPT_IMAGE_DATA_DIR。")

    def emit(result: Dict[str, Any]) -> None:
        if warnings:
            result["warnings"] = warnings
        _emit(result, as_json)

    try:
        config_root = resolve_config_root(args.config_dir)
        if args.command == "configure":
            if args.data_dir is not None:
                raise ConfigurationError("configure 不再用 --data-dir 选择配置位置，请改用 --config-dir。")
            emit(configure(config_root, args))
            return 0

        data_root = resolve_data_root(args.data_dir, config_root=config_root)
        if args.command == "doctor":
            result = doctor(config_root, data_root, args.mainland_help)
            if args.download_network is not None:
                result["download_network"] = args.download_network
            emit(result)
            return 0 if result["ok"] else 2

        config = load_config(config_root, require_key=True)
        if args.download_network is not None:
            config["download_network"] = args.download_network
        for option in ("max_wait", "poll_interval", "yield_after"):
            value = getattr(args, option, None)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ConfigurationError(f"{option.replace('_', '-')} 必须是大于 0 的有限秒数。")
        if not args.quiet_progress:
            config["_progress"] = ProgressReporter(float(config["progress_interval_seconds"]))
        _prepare_data_root(data_root)
        if args.command in {"generate", "edit"}:
            parameters = _request_parameters(args, config)
            failure_model = str(parameters["model"])
            if args.command == "generate":
                task = submit_generation(data_root, config, parameters)
            else:
                images = [path.expanduser() for path in args.image]
                mask = args.mask.expanduser() if args.mask else None
                task = submit_edit(data_root, config, parameters, images, mask)
            current_task_id = str(task["task_id"])
            report_progress(config, task)
            if args.detach:
                emit(_task_output(task, data_root, config_root))
                return 0
            task = wait_for_task(
                data_root,
                config,
                str(task["task_id"]),
                max_wait=args.max_wait,
                poll_interval=args.poll_interval,
                yield_after=args.yield_after,
            )
            emit(_task_output(task, data_root, config_root))
            return 0
        if args.command in {"status", "wait"}:
            saved_task = _load_or_recover_task(data_root, args.task_id, config, legacy_root=config_root)
            failure_model = str(saved_task.get("model") or config["model"])
            if args.download_network is not None and saved_task.get("download_network") != args.download_network:
                saved_task["download_network"] = args.download_network
                saved_task.pop("download_state", None)
                save_task(data_root, saved_task)
        if args.command == "status":
            task = poll_task_once(data_root, config, args.task_id)
            emit(_task_output(task, data_root, config_root))
            return 0
        if args.command == "wait":
            task = wait_for_task(
                data_root,
                config,
                args.task_id,
                max_wait=args.max_wait,
                poll_interval=args.poll_interval,
                yield_after=args.yield_after,
            )
            emit(_task_output(task, data_root, config_root))
            return 0
        parser.error("未知命令")
    except (SkillError, OSError) as exc:
        message = _safe_error(str(exc), str(config.get("api_key", "")))
        error_result: Dict[str, Any] = {"ok": False, "error": message, "error_type": exc.__class__.__name__}
        if isinstance(exc, StorageError):
            current_task_id, data_root = exc.task_id, exc.data_root
        if config_root is not None:
            error_result["config_path"] = str(config_root / "config.json")
        if data_root is not None:
            error_result["data_dir"] = str(data_root)
        if current_task_id and data_root is not None and config_root is not None:
            saved = None
            try:
                saved = load_task(data_root, current_task_id)
            except (SkillError, OSError):
                pass
            error_result.update(_recovery_info(current_task_id, data_root, config_root, saved))
            if saved:
                error_result.update({key: saved.get(key) for key in ("phase", "remote_status", "error_stage")})
                error_result["outputs"] = saved.get("output_paths", [])
        if failure_model:
            error_result["model"] = failure_model
            error_result["recommended_models"] = list(RECOMMENDED_MODELS)
            if isinstance(exc, TaskFailedError) or (isinstance(exc, RequestFailure) and not current_task_id):
                error_result.update(_model_failure_guidance(failure_model))
        if current_task_id and not isinstance(exc, TaskFailedError):
            error_result["hint"] = "请保留任务 ID 和目录，修复错误后使用 resume_args 中的 wait 参数恢复，不要重复提交。"
        if as_json:
            emit(error_result)
        else:
            print(f"错误：{message}", file=sys.stderr)
            emit({key: value for key, value in error_result.items() if key not in {"ok", "error"}})
        return exc.exit_code if isinstance(exc, SkillError) else StorageError.exit_code
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
