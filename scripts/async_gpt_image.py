#!/usr/bin/env python3
"""Submit, poll, recover, and download sub2api asynchronous GPT Image tasks."""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import json
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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote_to_bytes, urlsplit, urlunsplit
from urllib.request import Request, urlopen


SKILL_NAME = "async-gpt-image-generate"
KEY_URL = "https://1token-store.com/keys"
RECOMMENDED_MODELS = ("gpt-image-2.5", "gpt-image-2")
DEFAULT_MODEL = RECOMMENDED_MODELS[0]
DEFAULT_CONFIG: Dict[str, Any] = {
    "base_url": "https://edge.1token-store.com",
    "model": DEFAULT_MODEL,
    "poll_interval_seconds": 3.0,
    "request_timeout_seconds": 60.0,
    "max_wait_seconds": 900.0,
    "output_format": "png",
    "max_download_bytes": 100 * 1024 * 1024,
}
RETRYABLE_HTTP_STATUSES = {408, 429}
MAX_API_RESPONSE_BYTES = 160 * 1024 * 1024
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_MASK_BYTES = 50 * 1024 * 1024
USER_AGENT = "async-gpt-image-generate/1.0"


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


class RequestFailure(SkillError):
    exit_code = 6

    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


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
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise OSError(f"不是目录：{path}")
    with tempfile.NamedTemporaryFile(prefix=".write-test-", dir=str(path)):
        pass
    for name in ("tasks", "outputs", "tmp"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path.resolve()


def resolve_data_root(
    explicit: Optional[Path] = None,
    *,
    home: Optional[Path] = None,
    cwd: Optional[Path] = None,
    allow_project_fallback: bool = False,
) -> Tuple[Path, bool]:
    """Return (data root, used fallback). Project fallback is always explicit."""
    environment_value = os.environ.get("ASYNC_GPT_IMAGE_HOME", "").strip()
    if explicit is not None or environment_value:
        selected = explicit if explicit is not None else Path(environment_value)
        try:
            return _prepare_data_root(selected.expanduser()), False
        except OSError as exc:
            raise ConfigurationError(f"无法使用指定的数据目录 {selected}：{exc}") from exc

    selected_home = home if home is not None else Path.home()
    preferred = selected_home / SKILL_NAME
    try:
        return _prepare_data_root(preferred), False
    except OSError as home_error:
        if not allow_project_fallback:
            raise ConfigurationError(
                f"默认数据目录不可写：{preferred}（{home_error}）。"
                "请先允许 Codex 访问该目录并重试；如果授权后仍不可写，必须询问用户是否同意"
                "回退到当前项目或会话目录。只有用户明确同意后才能使用 --allow-project-fallback，"
                "不要把配置、任务和图片静默写入当前项目。"
            ) from home_error
        selected_cwd = cwd if cwd is not None else Path.cwd()
        fallback = selected_cwd / SKILL_NAME
        try:
            return _prepare_data_root(fallback), True
        except OSError as cwd_error:
            raise ConfigurationError(
                f"Home 数据目录不可写（{home_error}），当前项目回退目录也不可写（{cwd_error}）。"
            ) from cwd_error


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

    environment_key = os.environ.get("ASYNC_GPT_IMAGE_API_KEY", "").strip()
    api_key = environment_key or str(merged.get("api_key", "")).strip()
    if require_key and not api_key:
        raise ConfigurationError(
            f"尚未配置 API Key。请访问 {KEY_URL}，选择 ChatGPT/Codex 分组申请密钥，"
            "然后把完整密钥发到当前 Codex 对话框，由 Codex 自动配置并校验。"
        )
    merged["api_key"] = api_key
    return merged


def load_config(data_root: Path, require_key: bool = True) -> Dict[str, Any]:
    config_path = data_root / "config.json"
    if not config_path.exists():
        if require_key and not os.environ.get("ASYNC_GPT_IMAGE_API_KEY", "").strip():
            raise ConfigurationError(
                f"没有找到配置文件：{config_path}\n"
                f"请访问 {KEY_URL}，选择 ChatGPT/Codex 分组申请密钥，再把完整密钥发到当前 Codex 对话框。"
            )
        return validate_config({}, require_key=require_key)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"无法读取配置文件 {config_path}：{exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("config.json 顶层必须是 JSON 对象。")
    return validate_config(value, require_key=require_key)


def configure(data_root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    config_path = data_root / "config.json"
    existing: Dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"现有配置无法读取，请先修复或备份：{exc}") from exc

    if args.from_stdin:
        api_key = sys.stdin.readline().strip()
        if not api_key:
            raise ConfigurationError("没有从标准输入收到 API Key。")
        base_url = args.base_url or str(existing.get("base_url") or DEFAULT_CONFIG["base_url"])
        model = args.model or str(existing.get("model") or DEFAULT_CONFIG["model"])
    elif args.from_env:
        api_key = os.environ.get("ASYNC_GPT_IMAGE_API_KEY", "").strip()
        if not api_key:
            raise ConfigurationError("--from-env 需要先设置 ASYNC_GPT_IMAGE_API_KEY。")
        base_url = args.base_url or str(existing.get("base_url") or DEFAULT_CONFIG["base_url"])
        model = args.model or str(existing.get("model") or DEFAULT_CONFIG["model"])
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
        default_base_url = args.base_url or str(existing.get("base_url") or DEFAULT_CONFIG["base_url"])
        entered_base_url = input(f"Base URL [{default_base_url}]：").strip()
        base_url = entered_base_url or default_base_url
        default_model = args.model or str(existing.get("model") or DEFAULT_CONFIG["model"])
        entered_model = input(f"模型 [{default_model}]：").strip()
        model = entered_model or default_model

    value = dict(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in existing:
            value[key] = existing[key]
    value.update({"base_url": base_url, "model": model, "api_key": api_key})
    validated = validate_config(value)
    atomic_write_json(config_path, validated, private=True)
    return {
        "ok": True,
        "configured": True,
        "config_path": str(config_path.resolve()),
        "data_dir": str(data_root),
        "base_url": normalize_api_base_url(validated["base_url"]),
        "model": validated["model"],
    }


def _read_limited(response: Any, maximum: int) -> bytes:
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
        chunk = response.read(min(1024 * 1024, maximum - total + 1))
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
            raw = _read_limited(response, MAX_API_RESPONSE_BYTES)
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
    value["updated_at"] = utc_now()
    atomic_write_json(task_file(data_root, task_id), value, private=True)


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
        "schema_version": 1,
        "task_id": task_id,
        "operation": operation,
        "status": "submitted",
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
        timeout=float(config["request_timeout_seconds"]),
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


def _download_http_image(url: str, maximum: int, timeout: float) -> Tuple[bytes, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProtocolError("结果中包含不支持的图片 URL。")
    request = Request(url, headers={"Accept": "image/*", "User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            mime = response.headers.get("Content-Type", "application/octet-stream")
            data = _read_limited(response, maximum)
    except HTTPError as exc:
        raise RequestFailure(f"图片下载失败（HTTP {exc.code}）。", status=exc.code, retryable=exc.code >= 500) from exc
    except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RequestFailure(f"图片下载失败：{reason}", retryable=True) from exc
    return data, mime


def download_results(
    data_root: Path,
    task_id: str,
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
    fallback_format: str,
) -> Tuple[List[str], List[str]]:
    result = payload.get("result")
    items = result.get("data") if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise ProtocolError("任务已完成，但响应缺少 result.data 图片列表。")
    output_paths: List[str] = []
    raw_urls: List[str] = []
    output_dir = data_root / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    maximum = int(config["max_download_bytes"])
    timeout = float(config["request_timeout_seconds"])
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        data: Optional[bytes] = None
        mime = f"image/{'jpeg' if fallback_format == 'jpeg' else fallback_format}"
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
            raw_urls.append(url_value)
            data, mime = _download_http_image(url_value, maximum, timeout)
        if data is None:
            continue
        if len(data) > maximum:
            raise ProtocolError(f"图片超过 {maximum} 字节下载限制。")
        output_paths.append(str(_write_image_bytes(output_dir, task_id, index, data, mime, fallback_format)))
    if not output_paths:
        raise ProtocolError("任务已完成，但 result.data 中没有可识别的 url 或 b64_json。")
    return output_paths, raw_urls


def _task_output(task: Mapping[str, Any], data_root: Path) -> Dict[str, Any]:
    return {
        "ok": True,
        "completed": task.get("status") == "completed",
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "remote_status": task.get("remote_status"),
        "outputs": task.get("output_paths", []),
        "raw_image_urls": task.get("raw_image_urls", []),
        "error": task.get("error"),
        "data_dir": str(data_root),
    }


def _load_or_recover_task(data_root: Path, task_id: str, config: Mapping[str, Any]) -> Dict[str, Any]:
    task = load_task(data_root, task_id)
    if task is not None:
        return task
    now = utc_now()
    task = {
        "schema_version": 1,
        "task_id": task_id,
        "operation": "recovered",
        "status": "recovered",
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
    if task.get("status") == "completed" and existing_outputs:
        task["output_paths"] = existing_outputs
        return task

    payload = _poll_payload(task_id, task, config)
    remote_status = str(payload.get("status", "")).strip()
    task["remote_status"] = remote_status
    if remote_status == "failed":
        task["status"] = "failed"
        task["error"] = _remote_error(payload)
        save_task(data_root, task)
        raise TaskFailedError(f"任务 {task_id} 失败：{task['error']}")
    if remote_status == "completed":
        fallback_format = str(task.get("parameters", {}).get("output_format") or config["output_format"])
        try:
            output_paths, raw_urls = download_results(data_root, task_id, payload, config, fallback_format)
        except SkillError as exc:
            task["status"] = "download_error"
            task["error"] = str(exc)
            save_task(data_root, task)
            raise
        task["status"] = "completed"
        task["output_paths"] = output_paths
        task["raw_image_urls"] = raw_urls
        task.pop("error", None)
        save_task(data_root, task)
        return task

    task["status"] = "processing"
    task.pop("error", None)
    save_task(data_root, task)
    return task


def wait_for_task(
    data_root: Path,
    config: Mapping[str, Any],
    task_id: str,
    *,
    max_wait: Optional[float] = None,
    poll_interval: Optional[float] = None,
) -> Dict[str, Any]:
    maximum = float(max_wait if max_wait is not None else config["max_wait_seconds"])
    interval = float(poll_interval if poll_interval is not None else config["poll_interval_seconds"])
    if maximum <= 0 or interval <= 0:
        raise ConfigurationError("max-wait 和 poll-interval 必须大于 0。")
    started = time.monotonic()
    last_retryable_error: Optional[str] = None
    while True:
        try:
            task = poll_task_once(data_root, config, task_id)
            last_retryable_error = None
            if task.get("status") == "completed":
                return task
        except RequestFailure as exc:
            if not exc.retryable:
                raise
            last_retryable_error = str(exc)
            task = _load_or_recover_task(data_root, task_id, config)
            task["status"] = "processing"
            task["last_poll_error"] = last_retryable_error
            save_task(data_root, task)

        elapsed = time.monotonic() - started
        if elapsed >= maximum:
            task = _load_or_recover_task(data_root, task_id, config)
            task["status"] = "wait_timeout"
            task["error"] = (
                f"等待超过 {maximum:g} 秒；任务仍可通过 wait {task_id} 恢复。"
                + (f" 最近一次轮询错误：{last_retryable_error}" if last_retryable_error else "")
            )
            save_task(data_root, task)
            raise TaskWaitTimeout(str(task["error"]))
        time.sleep(min(interval, max(0.0, maximum - elapsed)))


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
    parser.add_argument("--data-dir", type=Path, help="显式指定配置和工作目录")
    parser.add_argument(
        "--allow-project-fallback",
        action="store_true",
        help="经用户明确同意后，允许回退到当前项目或会话目录",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")


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
    add_runtime_args(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="async_gpt_image.py",
        description="1Token sub2api 异步 GPT 图片生成和编辑工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="检查运行环境和配置")
    doctor_parser.add_argument("--mainland-help", action="store_true", help="显示中国大陆安装和镜像提示")
    add_runtime_args(doctor_parser)

    configure_parser = subparsers.add_parser("configure", help="安全配置 API Key")
    configure_parser.add_argument("--base-url", help="覆盖默认 Base URL")
    configure_parser.add_argument("--model", help="覆盖默认模型")
    key_source = configure_parser.add_mutually_exclusive_group()
    key_source.add_argument("--from-env", action="store_true", help="从 ASYNC_GPT_IMAGE_API_KEY 读取密钥")
    key_source.add_argument(
        "--from-stdin",
        action="store_true",
        help="从标准输入读取一行密钥，供 Codex 自动配置使用",
    )
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
    add_runtime_args(wait_parser)
    return parser


def doctor(data_root: Path, used_fallback: bool, show_mainland_help: bool) -> Dict[str, Any]:
    config_path = data_root / "config.json"
    configured = False
    config_error: Optional[str] = None
    base_url = normalize_api_base_url(DEFAULT_CONFIG["base_url"])
    model = DEFAULT_CONFIG["model"]
    try:
        config = load_config(data_root, require_key=True)
        configured = True
        base_url = normalize_api_base_url(str(config["base_url"]))
        model = str(config["model"])
    except ConfigurationError as exc:
        config_error = str(exc)
    result: Dict[str, Any] = {
        "ok": configured and sys.version_info >= (3, 9),
        "python": sys.version.split()[0],
        "python_supported": sys.version_info >= (3, 9),
        "configured": configured,
        "config_path": str(config_path.resolve()),
        "data_dir": str(data_root),
        "used_project_fallback": used_fallback,
        "base_url": base_url,
        "model": model,
        "config_error": config_error,
        "key_help": f"在 {KEY_URL} 申请密钥并选择 ChatGPT/Codex 分组。" if not configured else None,
    }
    if show_mainland_help:
        result["mainland_china_help"] = [
            "本技能仅使用 Python 标准库，不需要 pip 安装依赖。",
            "Windows 可尝试 winget；受阻时可使用清华 Miniconda 镜像。",
            "macOS 的 Homebrew 可参考中科大镜像帮助。",
            "Ubuntu/Debian 可参考清华镜像帮助切换软件源。",
            "详细链接见技能 README.md。",
        ]
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    failure_model: Optional[str] = None
    try:
        data_root, used_fallback = resolve_data_root(
            getattr(args, "data_dir", None),
            allow_project_fallback=bool(getattr(args, "allow_project_fallback", False)),
        )
        if args.command == "doctor":
            result = doctor(data_root, used_fallback, args.mainland_help)
            _emit(result, as_json)
            return 0 if result["ok"] else 2
        if args.command == "configure":
            _emit(configure(data_root, args), as_json)
            return 0

        config = load_config(data_root, require_key=True)
        if args.command in {"generate", "edit"}:
            parameters = _request_parameters(args, config)
            failure_model = str(parameters["model"])
            if args.command == "generate":
                task = submit_generation(data_root, config, parameters)
            else:
                images = [path.expanduser() for path in args.image]
                mask = args.mask.expanduser() if args.mask else None
                task = submit_edit(data_root, config, parameters, images, mask)
            if args.detach:
                _emit(_task_output(task, data_root), as_json)
                return 0
            task = wait_for_task(
                data_root,
                config,
                str(task["task_id"]),
                max_wait=args.max_wait,
                poll_interval=args.poll_interval,
            )
            _emit(_task_output(task, data_root), as_json)
            return 0
        if args.command == "status":
            saved_task = load_task(data_root, args.task_id)
            failure_model = str((saved_task or {}).get("model") or config["model"])
            task = poll_task_once(data_root, config, args.task_id)
            _emit(_task_output(task, data_root), as_json)
            return 0
        if args.command == "wait":
            saved_task = load_task(data_root, args.task_id)
            failure_model = str((saved_task or {}).get("model") or config["model"])
            task = wait_for_task(
                data_root,
                config,
                args.task_id,
                max_wait=args.max_wait,
                poll_interval=args.poll_interval,
            )
            _emit(_task_output(task, data_root), as_json)
            return 0
        parser.error("未知命令")
    except SkillError as exc:
        error_result = {"ok": False, "error": str(exc), "error_type": exc.__class__.__name__}
        if failure_model:
            error_result.update(_model_failure_guidance(failure_model))
        if as_json:
            print(json.dumps(error_result, ensure_ascii=False, indent=2))
        else:
            print(f"错误：{exc}", file=sys.stderr)
            if failure_model:
                guidance = _model_failure_guidance(failure_model)
                print(f"当前模型：{guidance['model']}", file=sys.stderr)
                print(f"可选模型：{', '.join(guidance['recommended_models'])}", file=sys.stderr)
                print(str(guidance["hint"]), file=sys.stderr)
        return exc.exit_code
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
