"""外部语录导入源：URL 直链、本地文件、WebDAV、Cloudflare R2、Cloudflare D1。

全部使用 Python 标准库（urllib + hmac），不引入第三方依赖，
因此插件无需 vendor/ 目录。函数均为阻塞式，调用方请用
asyncio.to_thread 包装。配置字段见 config.example.toml。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

USER_AGENT = "neko-plugin-quote-extract/0.1"
DEFAULT_TIMEOUT = 20.0

_SOURCES = ("none", "url", "file", "webdav", "r2", "d1")


class FetchError(Exception):
    """导入失败，message 面向用户展示。"""


def known_source(source: str) -> bool:
    return source in _SOURCES


def _http_get(url: str, *, headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code}：{url}") from e
    except urllib.error.URLError as e:
        raise FetchError(f"网络错误：{e.reason}（{url}）") from e
    except TimeoutError as e:
        raise FetchError(f"请求超时（{url}）") from e


def _basic_auth(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


# ---- 各来源 ----

def fetch_url(cfg: dict) -> bytes:
    url = (cfg.get("url") or "").strip()
    if not url:
        raise FetchError("未配置导入直链：请在配置 [import] 中填写 url")
    return _http_get(url)


def read_local_file(cfg: dict) -> bytes:
    path = (cfg.get("local_path") or "").strip()
    if not path:
        raise FetchError("未配置本地文件：请在配置 [import] 中填写 local_path")
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise FetchError(f"读取本地文件失败：{e}") from e


def fetch_webdav(cfg: dict) -> bytes:
    base = (cfg.get("webdav_url") or "").strip().rstrip("/")
    path = (cfg.get("webdav_path") or "").strip()
    if not base or not path:
        raise FetchError("WebDAV 未配置完整：需要 webdav_url 与 webdav_path")
    url = f"{base}/{path.lstrip('/')}"
    headers = {}
    user = (cfg.get("webdav_username") or "").strip()
    password = cfg.get("webdav_password") or ""
    if user:
        headers["Authorization"] = _basic_auth(user, password)
    return _http_get(url, headers=headers)


def _cf_api(cfg: dict, path: str, *, method: str = "GET", body: dict | None = None,
            parse_json: bool = True) -> Any:
    token = (cfg.get("_api_token") or "").strip()
    if not token:
        raise FetchError("缺少 Cloudflare API Token")
    url = f"https://api.cloudflare.com/client/v4{path}"
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            envelope = json.loads(e.read().decode("utf-8", errors="replace"))
            errors = envelope.get("errors") or []
            if errors:
                detail = "：" + "; ".join(str(x.get("message", x)) for x in errors)
        except Exception:
            pass
        raise FetchError(f"Cloudflare API HTTP {e.code}{detail}") from e
    except urllib.error.URLError as e:
        raise FetchError(f"网络错误：{e.reason}") from e

    # R2 对象下载返回原始字节（不做信封解析，语录文件本身可能就是 JSON）；
    # D1 等管理 API 返回 JSON 信封
    if not parse_json:
        return raw
    try:
        envelope = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return raw
    if isinstance(envelope, dict) and envelope.get("success") is False:
        errors = envelope.get("errors") or []
        detail = "; ".join(str(x.get("message", x)) for x in errors) if errors else "未知错误"
        raise FetchError(f"Cloudflare API 错误：{detail}")
    return envelope


def fetch_r2(cfg: dict) -> bytes:
    """R2 三种方式：公开直链 > S3 兼容（SigV4）> Cloudflare API。"""
    public_url = (cfg.get("r2_public_url") or "").strip()
    s3_endpoint = (cfg.get("r2_s3_endpoint") or "").strip()
    if public_url:
        return _http_get(public_url)
    if s3_endpoint:
        access_key = (cfg.get("r2_access_key_id") or "").strip()
        secret_key = (cfg.get("r2_secret_access_key") or "").strip()
        if not access_key or not secret_key:
            raise FetchError("使用 r2_s3_endpoint 时需要 r2_access_key_id 与 r2_secret_access_key")
        key = (cfg.get("r2_object_key") or "quotes.json").strip().lstrip("/")
        url = f"{s3_endpoint.rstrip('/')}/{key}"
        return _http_get(url, headers=sigv4_get_headers(url, access_key, secret_key))

    account = (cfg.get("r2_account_id") or "").strip()
    bucket = (cfg.get("r2_bucket") or "").strip()
    key = (cfg.get("r2_object_key") or "quotes.json").strip().lstrip("/")
    if account and bucket:
        cfg = dict(cfg)
        cfg["_api_token"] = cfg.get("r2_api_token")
        path = f"/accounts/{account}/r2/buckets/{bucket}/objects/{urllib.parse.quote(key)}"
        return _cf_api(cfg, path, parse_json=False)  # type: ignore[return-value]
    raise FetchError("R2 未配置：请填写 r2_public_url，或 r2_s3_endpoint + 密钥，或 account_id + bucket + api_token")


def fetch_d1(cfg: dict) -> list[dict]:
    """从 Cloudflare D1 执行配置的 SQL，返回行列表。"""
    account = (cfg.get("d1_account_id") or "").strip()
    database = (cfg.get("d1_database_id") or "").strip()
    sql = (cfg.get("d1_sql") or "").strip()
    if not account or not database or not sql:
        raise FetchError("D1 未配置完整：需要 d1_account_id、d1_database_id、d1_sql 与 d1_api_token")
    cfg = dict(cfg)
    cfg["_api_token"] = cfg.get("d1_api_token")
    envelope = _cf_api(
        cfg,
        f"/accounts/{account}/d1/database/{database}/query",
        method="POST",
        body={"sql": sql},
    )
    try:
        results = envelope["result"]
        rows = results[0]["results"]
        if not isinstance(rows, list):
            raise KeyError("results")
        return rows
    except (KeyError, IndexError, TypeError) as e:
        raise FetchError(f"D1 返回格式异常，请检查 SQL 是否为 SELECT 查询：{e}") from e


def fetch_quotes(cfg: dict) -> bytes | list[dict]:
    """按配置的 source 拉取原始字节（d1 直接返回行）。"""
    source = (cfg.get("source") or "none").strip().lower()
    if source == "none":
        raise FetchError("尚未配置导入源（source = \"none\"）。请在配置中选择 url / file / webdav / r2 / d1 之一")
    if source == "url":
        return fetch_url(cfg)
    if source == "file":
        return read_local_file(cfg)
    if source == "webdav":
        return fetch_webdav(cfg)
    if source == "r2":
        return fetch_r2(cfg)
    if source == "d1":
        return fetch_d1(cfg)
    raise FetchError(f"未知的导入源：{source}（可选：{', '.join(_SOURCES[1:])}）")


# ---- S3 SigV4（R2 S3 兼容模式）----

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_get_headers(url: str, access_key: str, secret_key: str, *,
                      region: str = "auto", service: str = "s3") -> dict:
    """为 GET 请求生成 AWS Signature V4 头（path-style，无查询参数）。"""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    canonical_uri = urllib.parse.quote(parsed.path or "/", safe="/-_.~")
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        ["GET", canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), date_stamp),
                            region), service), "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
