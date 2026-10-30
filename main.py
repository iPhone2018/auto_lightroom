#!/usr/bin/env python3
"""Lightroom Web 自动化（单文件版）：上传 -> 公开共享 -> 修改照片 Title。

包含 Lightroom API 客户端和 Tkinter GUI。

启动：
    source ../auto_vimeo/venv/bin/activate
    pip install openpyxl
    python main.py
"""
from __future__ import annotations


import argparse
import http.cookiejar
import json
import mimetypes
import os
import random
import string
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

BASE_URL = "https://lightroom.adobe.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
IMPORTED_ON_DEVICE = (
    "Lightroom Web {0363f1a98b0c507a237977267358886724194e58198ae5efc58c659238ee8fc3}"
)
CRS_VERSION = "18.7"
MONTHS_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

MIME_OVERRIDES = {
    "dng": "image/x-adobe-dng",
    "cr2": "image/x-canon-cr2",
    "cr3": "image/x-canon-cr3",
    "arw": "image/x-sony-arw",
    "nef": "image/x-nikon-nef",
    "raf": "image/x-fuji-raf",
    "orf": "image/x-olympus-orf",
    "rw2": "image/x-panasonic-rw2",
}


class LightroomError(RuntimeError):
    pass


class QuotaExceededError(LightroomError):
    pass


class AlreadyUploadedError(LightroomError):
    """The server rejected the upload because the same image already exists."""

    def __init__(self, message: str, existing_asset_id: str | None = None):
        super().__init__(message)
        self.existing_asset_id = existing_asset_id


def iso_now(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_json_body(raw: bytes) -> Any:
    """Lightroom responses are prefixed with an anti-hijacking `while (1) {}`."""
    if not raw:
        return None
    text = raw.decode("utf-8", "replace").lstrip()
    prefix = "while (1) {}"
    if text.startswith(prefix):
        text = text[len(prefix):].lstrip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LightroomError(f"无法解析响应 JSON: {text[:300]!r}") from exc


def load_cookie_jar(path: str) -> http.cookiejar.CookieJar:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "cookies" in data:
        cookies = data["cookies"]
    elif isinstance(data, dict):
        # Also accept a plain {"cookie_name": "cookie_value"} export.
        cookies = [
            {"name": str(k), "value": str(v), "domain": ".adobe.com", "path": "/"}
            for k, v in data.items()
        ]
    else:
        cookies = data
    jar = http.cookiejar.CookieJar()
    for c in cookies:
        expires = c.get("expires")
        try:
            expires = int(float(expires)) if expires and float(expires) > 0 else None
        except (TypeError, ValueError):
            expires = None
        domain = c.get("domain") or ""
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=c["name"],
                value=c.get("value", ""),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=c.get("path") or "/",
                path_specified=True,
                secure=bool(c.get("secure")),
                expires=expires,
                discard=False,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""} if c.get("httpOnly") else {},
                rfc2109=False,
            )
        )
    return jar


class LightroomClient:
    def __init__(
        self, cookies_path: str | None = None, cookie_header: str | None = None
    ):
        """Authenticate with either a cookies JSON file or a raw Cookie header."""
        self.cookie_header = cookie_header.strip() if cookie_header else None
        if self.cookie_header:
            self.jar = None
            self.opener = urllib.request.build_opener()
        else:
            if not cookies_path:
                raise LightroomError("需要 cookies_path 或 cookie_header")
            self.jar = load_cookie_jar(cookies_path)
            self.opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self.jar)
            )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> tuple[int, dict[str, str], bytes]:
        url = path if path.startswith("http") else BASE_URL + path
        h = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": USER_AGENT,
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
            "x-requested-with": "XMLHttpRequest",
        }
        if headers:
            h.update(headers)
        if self.cookie_header:
            h["Cookie"] = self.cookie_header
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        # GETs are safe to retry; writes are not (a lost response could mean the
        # write actually succeeded).
        retries = 2 if method.upper() == "GET" else 0
        for attempt in range(retries + 1):
            try:
                with self.opener.open(req, timeout=timeout) as resp:
                    return (
                        resp.status,
                        {k.lower(): v for k, v in resp.headers.items()},
                        resp.read(),
                    )
            except urllib.error.HTTPError as exc:
                return (
                    exc.code,
                    {k.lower(): v for k, v in exc.headers.items()},
                    exc.read(),
                )
            except OSError as exc:
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LightroomError(f"{method} {path} 网络错误: {exc}") from exc
        raise AssertionError("unreachable")

    def json_request(self, method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, str], Any]:
        status, headers, raw = self.request(method, path, **kwargs)
        return status, headers, parse_json_body(raw)

    def json_write(
        self,
        method: str,
        path: str,
        payload: Any,
        expected: tuple[int, ...] = (200, 201, 202, 204),
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"Content-Type": "application/json;charset=UTF-8"}
        if headers:
            req_headers.update(headers)
        status, resp_headers, data = self.json_request(
            method,
            path,
            body=body,
            headers=req_headers,
        )
        if status not in expected:
            raise LightroomError(
                f"{method} {path} 失败: HTTP {status} {data or ''}"
            )
        return status, resp_headers, data


def discover_catalog(client: LightroomClient) -> str:
    status, _, data = client.json_request(
        "GET",
        "/v2/catalogs?role=any&subtype=lightroom%3BX-lightroom",
        headers={"x-all-account-states": "true"},
    )
    resources = (data or {}).get("resources") or []
    if status != 200 or not resources:
        raise LightroomError(f"无法获取 catalog 列表: HTTP {status} {data}")
    return resources[0]["id"]


def account_name(client: LightroomClient, account_id: str | None) -> str:
    if not account_id:
        return ""
    status, _, data = client.json_request("GET", f"/v2/accounts/{account_id}")
    if status != 200 or not isinstance(data, dict):
        return ""
    return (
        data.get("full_name")
        or " ".join(x for x in (data.get("first_name"), data.get("last_name")) if x)
        or data.get("display_name")
        or ""
    )


def format_bytes(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def discover_account_id(client: LightroomClient, catalog_id: str) -> str | None:
    """Read the uploader account id from an existing catalog asset.

    The API has no "current account" endpoint, so this is the reliable way to
    find the account id.  Pass --account-id when the catalog has no assets yet
    or contains someone else's uploads.
    """
    status, _, data = client.json_request(
        "GET", f"/v2/catalogs/{catalog_id}/assets?limit=1"
    )
    for resource in (data or {}).get("resources") or []:
        imported_by = (
            ((resource.get("payload") or {}).get("importSource") or {}).get("importedBy")
        )
        if imported_by:
            return imported_by
    return None


def get_storage_info(client: LightroomClient, account_id: str) -> dict[str, Any]:
    status, _, data = client.json_request("GET", f"/v2/accounts/{account_id}")
    if status != 200 or not isinstance(data, dict):
        raise LightroomError(f"读取账号存储信息失败: HTTP {status}")
    storage = (data.get("entitlement") or {}).get("storage") or {}
    if "used" not in storage or not (storage.get("display_limit") or storage.get("limit")):
        raise LightroomError("账号响应中没有可用的 storage 信息")
    return storage


def print_quota(storage: dict[str, Any]) -> int:
    used = int(storage.get("used") or 0)
    limit = int(storage.get("display_limit") or storage.get("limit") or 0)
    warn = int(storage.get("warn") or 0)
    remaining = max(0, limit - used)
    percent = (used / limit * 100) if limit else 0.0
    print(
        f"[配额] 已用 {format_bytes(used)} / {format_bytes(limit)}"
        f"（{percent:.3f}%），剩余 {format_bytes(remaining)}"
    )
    if warn:
        print(f"[配额] 预警线 {format_bytes(warn)}")
    return remaining


def ensure_space_for(storage: dict[str, Any], file_size: int) -> None:
    used = int(storage.get("used") or 0)
    limit = int(storage.get("display_limit") or storage.get("limit") or 0)
    remaining = max(0, limit - used)
    if limit and file_size > remaining:
        raise QuotaExceededError(
            f"存储空间不足：文件 {format_bytes(file_size)}，剩余 {format_bytes(remaining)}"
            f"（已用 {format_bytes(used)} / {format_bytes(limit)}）"
        )


def guess_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def build_upload_body(
    file_path: str,
    file_bytes: bytes,
    batch_time: str,
) -> tuple[str, bytes]:
    filename = os.path.basename(file_path)
    revision = {
        "payload": {
            "userCreated": batch_time,
            "userUpdated": batch_time,
            "importSource": {
                "importedOnDevice": IMPORTED_ON_DEVICE,
                "importTimestamp": batch_time,
                "fileSize": len(file_bytes),
                "fileName": filename,
            },
            "develop": {
                "crsVersion": CRS_VERSION,
                "processingModel": "lightroom",
            },
        }
    }
    revision_json = json.dumps(revision, ensure_ascii=False)

    boundary = "----WebKitFormBoundary" + "".join(
        random.choices(string.ascii_letters + string.digits, k=16)
    )
    safe_name = filename.replace('"', "_").replace("\r", "").replace("\n", "")
    parts = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="revision"\r\n\r\n'
            f"{revision_json}\r\n"
        ).encode("utf-8"),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="master"; '
            f'filename="{safe_name}"\r\nContent-Type: {guess_mime(file_path)}\r\n\r\n'
        ).encode("utf-8"),
        file_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ]
    return boundary, b"".join(parts)


def extract_asset_id(location: str | None, data: Any) -> str | None:
    if location and "/revisions/" in location:
        return location.split("/revisions/")[0].rstrip("/").split("/")[-1]
    if isinstance(data, dict):
        href = ((data.get("links") or {}).get("self") or {}).get("href", "")
        if "/revisions/" in href:
            return href.split("/revisions/")[0].rstrip("/").split("/")[-1]
    return None


def upload_asset(
    client: LightroomClient,
    catalog_id: str,
    file_path: str,
    retry_on_duplicate: bool = True,
    send_if_none_match: bool = True,
) -> tuple[str, dict[str, Any]]:
    with open(file_path, "rb") as fh:
        file_bytes = fh.read()
    if not file_bytes:
        raise LightroomError(f"文件为空: {file_path}")

    batch_time = iso_now()
    boundary, body = build_upload_body(file_path, file_bytes, batch_time)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "x-generate-proxies": "all",
    }
    if send_if_none_match:
        headers["if-none-match"] = "*"

    print(f"[上传] {os.path.basename(file_path)} ({len(file_bytes)} bytes) ...")

    def post_upload() -> tuple[int, dict[str, str], Any]:
        status, resp_headers, raw = client.request(
            "POST",
            f"/v2/catalogs/{catalog_id}/assets",
            body=body,
            headers=headers,
            timeout=600,
        )
        try:
            data = parse_json_body(raw)
        except LightroomError:
            data = raw.decode("utf-8", "replace")[:500]
        return status, resp_headers, data

    def post_with_retries() -> tuple[int, dict[str, str], Any]:
        last_error: Exception | None = None
        for network_attempt in range(1, 4):
            try:
                return post_upload()
            except LightroomError as exc:
                last_error = exc
                if network_attempt < 3 and "网络错误" in str(exc):
                    print(
                        f"      上传网络错误，重试 {network_attempt + 1}/3 ..."
                    )
                    time.sleep(2)
                    continue
                raise
        raise last_error  # pragma: no cover

    status, resp_headers, data = post_with_retries()
    # If-None-Match: * makes the API answer 412 when the same content already
    # exists.  The CLI retries without the precondition; the GUI uses
    # retry_on_duplicate=False to turn 412 into a "skip this image" signal.
    if status == 412:
        if not retry_on_duplicate:
            existing_id = None
            if isinstance(data, dict):
                for revision in data.get("revisions") or []:
                    href = (
                        ((revision.get("links") or {}).get("/rels/asset") or {})
                        .get("href")
                        or ""
                    )
                    if href.startswith("assets/"):
                        existing_id = href.split("/")[1]
                        break
                    if "/assets/" in href:
                        existing_id = href.split("/assets/")[1].split("/")[0]
                        break
            raise AlreadyUploadedError(
                f"图片已上传过（HTTP 412，已有 asset: {existing_id or '未知'}）",
                existing_id,
            )
        headers.pop("if-none-match", None)
        status, resp_headers, data = post_with_retries()
    if status not in (200, 201, 202):
        raise LightroomError(f"上传失败: HTTP {status} {data or ''}")

    asset_id = extract_asset_id(resp_headers.get("location"), data)
    if not asset_id:
        raise LightroomError(f"上传响应中找不到 asset id: {data}")
    print(f"      asset id: {asset_id}")

    deadline = time.time() + 180
    asset: dict[str, Any] = {}
    while time.time() < deadline:
        status, _, asset = client.json_request(
            "GET", f"/v2/catalogs/{catalog_id}/assets/{asset_id}"
        )
        payload = (asset or {}).get("payload") or {}
        import_source = payload.get("importSource") or {}
        if status == 200 and asset.get("revision_ids") and import_source.get("originalWidth"):
            print("      图片已处理完成")
            return asset_id, asset
        time.sleep(1.5)
    raise LightroomError(f"等待图片处理超时: {asset_id}")


def create_share(
    client: LightroomClient,
    catalog_id: str,
    asset_id: str,
    asset: dict[str, Any],
) -> dict[str, Any]:
    account_id = ((asset.get("payload") or {}).get("importSource") or {}).get("importedBy")
    author = account_name(client, account_id)

    now = datetime.now()
    default_name = f"{MONTHS_EN[now.month - 1]} {now.day}, {now.year}"
    album_id = uuid.uuid4().hex
    space_id = uuid.uuid4().hex
    created = iso_now()

    print(f"[共享] 创建共享相册 {album_id} ...")
    client.json_write(
        "PUT",
        f"/v2/catalogs/{catalog_id}/albums/{album_id}",
        {
            "subtype": "collection",
            "payload": {
                "hideInOrganize": True,
                "name": default_name,
                "userCreated": created,
                "userUpdated": created,
            },
        },
        expected=(200, 201),
    )

    presentation_payload: dict[str, Any] = {
        "presentation": {
            "options": {
                "showTitle": True,
                "showAuthor": True,
                "showCover": False,
                "cover": "background",
            },
            "explore": {"version": 1, "themeId": "Gallery", "styleId": "Dark"},
        }
    }
    if author:
        presentation_payload["story"] = {"author": author}
    client.json_write(
        "POST",
        f"/v2/catalogs/{catalog_id}/albums/{album_id}",
        {"payload": presentation_payload},
        expected=(204, 200),
    )

    add_time = iso_now()
    client.json_write(
        "PUT",
        f"/v2/catalogs/{catalog_id}/albums/{album_id}/assets",
        {
            "resources": [
                {
                    "id": asset_id,
                    "payload": {"userCreated": add_time, "userUpdated": add_time},
                }
            ]
        },
        expected=(200, 201, 202, 204),
    )

    print(f"[共享] 创建公开共享空间 {space_id} ...")
    client.json_write(
        "PUT",
        f"/v2/catalogs/{catalog_id}/spaces/{space_id}",
        {
            "payload": {
                "download": False,
                "private": False,
                "metadata": False,
                "location": False,
                "hide_url": False,
                "presentationFilters": True,
                "allowInviteRequests": True,
                "userCreated": created,
                "userUpdated": created,
            },
            "subtype": "shared",
            "resources": [{"id": album_id, "type": "album"}],
        },
        expected=(200, 201, 202, 204),
    )

    share_url = f"{BASE_URL}/shares/{space_id}"
    for _ in range(10):
        status, _, space = client.json_request(
            "GET",
            f"/v2/catalogs/{catalog_id}/spaces/{space_id}"
            "?embed=owner%3Bspace_user%3Bfirst_resource",
        )
        if status == 200 and isinstance(space, dict):
            href = ((space.get("links") or {}).get("/rels/web_space") or {}).get("href")
            if href:
                share_url = href
                break
        time.sleep(1)

    return {
        "asset_id": asset_id,
        "album_id": album_id,
        "space_id": space_id,
        "share_url": share_url,
    }


def set_asset_title(
    client: LightroomClient, catalog_id: str, asset_id: str, title: str
) -> dict[str, Any]:
    """Set the photo's own Title (XMP dc:title) by creating a child revision.

    Mirrors the web app's CatalogAsset.setTitleP(): merge the existing xmp.dc
    object, replace `title`, then PUT a new child revision under the latest
    revision.
    """
    status, _, asset = client.json_request(
        "GET", f"/v2/catalogs/{catalog_id}/assets/{asset_id}"
    )
    if status != 200 or not isinstance(asset, dict):
        raise LightroomError(f"读取 asset 失败: HTTP {status}")
    revision_ids = asset.get("revision_ids") or []
    if not revision_ids:
        raise LightroomError(f"asset 还没有 revision: {asset_id}")
    latest_revision = revision_ids[-1]

    dc = ((asset.get("payload") or {}).get("xmp") or {}).get("dc") or {}
    dc = json.loads(json.dumps(dc))  # deep copy
    dc["title"] = title

    child_revision_id = uuid.uuid4().hex
    path = (
        f"/v2/catalogs/{catalog_id}/assets/{asset_id}"
        f"/revisions/{latest_revision}/children/{child_revision_id}"
    )
    print(f"[Title] 修改照片 Title 为 {title!r} ...")
    client.json_write(
        "PUT",
        path,
        {"payload": {"xmp": {"dc": dc}, "userUpdated": iso_now()}},
        expected=(200, 201, 202, 204),
        headers={"prefer": "update-atomic"},
    )

    # Confirm the new value landed on the asset.
    for _ in range(15):
        status, _, updated = client.json_request(
            "GET", f"/v2/catalogs/{catalog_id}/assets/{asset_id}"
        )
        got = (((updated or {}).get("payload") or {}).get("xmp") or {}).get("dc") or {}
        if status == 200 and got.get("title") == title:
            print("       照片 Title 已生效")
            return updated
        time.sleep(1)
    raise LightroomError(f"Title 请求已提交，但轮询时未在 asset 上生效: {asset_id}")


def set_share_title(
    client: LightroomClient, catalog_id: str, album_id: str, title: str
) -> None:
    print(f"[共享] 修改共享页标题（相册名）为 {title!r} ...")
    client.json_write(
        "POST",
        f"/v2/catalogs/{catalog_id}/albums/{album_id}",
        {"payload": {"name": title, "userUpdated": iso_now()}},
        expected=(200, 204),
    )


# ---------------------------------------------------------------------------
# Namespace used by the GUI layer below; all code lives in this single file.
# ---------------------------------------------------------------------------
import types

lr = types.SimpleNamespace(
    AlreadyUploadedError=AlreadyUploadedError,
    LightroomClient=LightroomClient,
    LightroomError=LightroomError,
    QuotaExceededError=QuotaExceededError,
    create_share=create_share,
    discover_account_id=discover_account_id,
    discover_catalog=discover_catalog,
    ensure_space_for=ensure_space_for,
    format_bytes=format_bytes,
    get_storage_info=get_storage_info,
    set_asset_title=set_asset_title,
    set_share_title=set_share_title,
    upload_asset=upload_asset,
)



import base64
import contextlib
import csv
import hashlib
import io
import json
import os
import queue
import random
import re
import threading
import time
import traceback
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from tkinter.scrolledtext import ScrolledText

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font
except ImportError:  # Excel 统计需要 openpyxl
    Workbook = None
    Font = None


APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".lightroom_auto_gui")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
DEFAULT_OUTPUT_DIR = os.path.join(APP_DIR, "output")

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif",
    ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2",
    ".orf", ".rw2", ".raf", ".pef", ".srw", ".3fr", ".fff", ".mrw", ".iiq",
    ".x3f",
}

RESULTS_FILENAME = "results.csv"
LOG_FILENAME = "run.log"
STATS_FILENAME = "结果统计.xlsx"
RESULT_HEADERS = [
    "时间", "账号", "账号标识", "图片文件", "标题",
    "asset_id", "album_id", "space_id", "共享链接",
]
STATS_HEADERS = ["CK", "成功数", "失败数", "总数"]
STATS_FLUSH_INTERVAL = 20


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def natural_key(value: str):
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", value)]


def scan_images(root: str) -> list[str]:
    root = os.path.abspath(root)
    images: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            (d for d in dirnames if not d.startswith(".")), key=natural_key
        )
        for name in filenames:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                images.append(os.path.join(dirpath, name))
    images.sort(key=lambda p: natural_key(os.path.relpath(p, root)))
    return images


RAW_EXTENSIONS = {
    "3fr", "arq", "arw", "axr", "cr2", "cr3", "crw", "dcr", "dng", "erf",
    "fff", "iiq", "k25", "kdc", "mef", "mos", "mrw", "nef", "nrw", "orf",
    "pef", "ptx", "pxn", "r3d", "raf", "raw", "rw2", "rwl", "sr2", "srf",
    "srw", "x3f",
}


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_raw_filename(name: str) -> bool:
    return os.path.splitext(name)[1].lower().lstrip(".") in RAW_EXTENSIONS


def _same_duplicate_name(remote_name: str, local_name: str) -> bool:
    if _is_raw_filename(local_name):
        return os.path.splitext(remote_name)[0] == os.path.splitext(local_name)[0]
    return remote_name == local_name


def find_duplicate_asset(
    client, catalog_id: str, filename: str, digest: str
):
    """Mirror the web app: same sha256 AND same filename means duplicate."""
    status, _, data = client.json_request(
        "GET",
        f"/v2/catalogs/{catalog_id}/assets"
        f"?subtype=image%3Bvideo&sha256={digest}&limit=1000",
    )
    if status != 200:
        raise LightroomError(f"重复检查失败: HTTP {status}")
    for resource in (data or {}).get("resources") or []:
        source = ((resource.get("payload") or {}).get("importSource") or {})
        if digest not in (source.get("sha256"), source.get("originalSHA256")):
            continue
        remote_name = source.get("fileName") or ""
        if _same_duplicate_name(remote_name, filename):
            return resource
    return None


def decode_text_file(path: str) -> str | None:
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def read_title_file(path: str) -> list[str]:
    text = decode_text_file(path)
    if text is None:
        return []
    titles: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        title = line.strip().lstrip("﻿")
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(title)
    return titles


def cookie_identity(header: str) -> str:
    """Stable per-account key: the IMS user id from lrw_token when possible."""
    match = re.search(r"(?:^|;\s*)lrw_token=([^;]*)", header)
    if match:
        token = match.group(1).strip().strip('"')
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            uid = claims.get("user_id") or claims.get("aa_id")
            if uid:
                return str(uid)
        except Exception:
            pass
    return "cookie-" + hashlib.sha256(header.encode("utf-8")).hexdigest()[:16]


def _strip_cookie_prefix(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("cookie:"):
        value = value[7:].strip()
    return value


def _domain_specificity(domain: str) -> int:
    d = str(domain or "").lstrip(".").lower()
    if d == "lightroom.adobe.com" or d.endswith(".lightroom.adobe.com"):
        return 2
    if d == "adobe.com":
        return 1
    return 0


def _header_from_cookie_list(cookies: list) -> str:
    applicable = []
    for cookie in cookies:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        specificity = _domain_specificity(cookie.get("domain"))
        if specificity:
            applicable.append((specificity, cookie))
    applicable.sort(key=lambda item: -item[0])

    parts: list[str] = []
    seen: set[str] = set()
    for _, cookie in applicable:
        name = str(cookie["name"])
        if name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={cookie.get('value', '')}")
    return "; ".join(parts)


def _entries_from_json(data) -> list[str]:
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            header = _header_from_cookie_list(data["cookies"])
            return [header] if header else []
        if "header" in data or "cookie" in data:
            header = _strip_cookie_prefix(
                str(data.get("header") or data.get("cookie") or "")
            )
            return [header] if header else []
        pairs = [
            f"{k}={v}" for k, v in data.items() if isinstance(v, (str, int, float))
        ]
        return ["; ".join(pairs)] if pairs else []
    if isinstance(data, list):
        if not data:
            return []
        if all(isinstance(x, dict) and "name" in x for x in data):
            header = _header_from_cookie_list(data)
            return [header] if header else []
        result: list[str] = []
        for item in data:
            result.extend(_entries_from_json(item))
        return [x for x in result if x]
    if isinstance(data, str):
        header = _strip_cookie_prefix(data)
        return [header] if header else []
    return []


def parse_cookie_entries(text: str) -> list[dict]:
    """Return [{'header': ..., 'identity': ...}, ...], one entry per account.

    Accepted inputs:
      * one account per line: `name=value; name2=value2`
      * a JSON object (name -> value)
      * a JSON cookie dump: {"cookies": [{name, value, domain, ...}, ...]}
      * a JSON list of strings or cookie objects
    """
    text = (text or "").strip()
    if not text:
        return []

    headers: list[str] = []
    if text[0] in "{[":
        try:
            headers = _entries_from_json(json.loads(text))
        except json.JSONDecodeError:
            headers = []

    if not headers:
        for line in text.splitlines():
            line = _strip_cookie_prefix(line)
            if not line:
                continue
            if line[0] in "{[":
                try:
                    headers.extend(_entries_from_json(json.loads(line)))
                    continue
                except json.JSONDecodeError:
                    pass
            headers.append(line)

    entries: list[dict] = []
    seen_headers: set[str] = set()
    seen_identities: set[str] = set()
    for header in headers:
        header = header.strip()
        if not header or header in seen_headers:
            continue
        seen_headers.add(header)
        identity = cookie_identity(header)
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        entries.append({"header": header, "identity": identity})
    return entries


def read_cookie_entries_from_file(path: str) -> list[dict]:
    """Read a txt file and return one CK entry per non-empty line."""
    text = decode_text_file(path)
    if text is None:
        return []
    return [
        entry
        for entry in parse_cookie_entries(text)
        if "=" in entry["header"]
    ]


def ensure_config_dir() -> None:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def atomic_write_json(path: str, data, mode: int = 0o600) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def append_result_csv(output_dir: str, row: list) -> None:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, RESULTS_FILENAME)
    new_file = not os.path.exists(path)
    with open(
        path, "a", encoding="utf-8-sig" if new_file else "utf-8", newline=""
    ) as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(RESULT_HEADERS)
        writer.writerow(row)


# ---------------------------------------------------------------------------
# title pool
# ---------------------------------------------------------------------------

class TitlePool:
    def __init__(self, files: list[str]):
        self.parts: list[list[str]] = []
        self.file_names: list[str] = []
        for path in files:
            titles = read_title_file(path)
            if not titles:
                raise ValueError(f"标题文件没有内容: {os.path.basename(path)}")
            self.parts.append(titles)
            self.file_names.append(os.path.basename(path))
        total = 1
        for part in self.parts:
            total *= len(part)
        self.total = total

    def random_title(self) -> str:
        return "".join(random.choice(part) for part in self.parts)


def excel_safe(value: str) -> str:
    """Strip characters / lengths Excel refuses to store in a cell."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))
    return text[:32767]


class ExcelStatsWriter:
    """Per-CK success/failure counters stored in a single .xlsx file."""

    def __init__(self, path: str, accounts: list["Account"]):
        if Workbook is None:
            raise RuntimeError(
                "缺少 openpyxl，无法写入 Excel。请先执行: pip install openpyxl"
            )
        self.path = path
        self.workbook = Workbook()
        sheet = self.workbook.active
        sheet.title = "统计"
        sheet.append(STATS_HEADERS)
        if Font is not None:
            for cell in sheet[1]:
                cell.font = Font(bold=True)
        sheet.column_dimensions["A"].width = 90
        sheet.column_dimensions["B"].width = 10
        sheet.column_dimensions["C"].width = 10
        sheet.column_dimensions["D"].width = 10
        sheet.freeze_panes = "A2"
        self.sheet = sheet
        self.rows: dict[int, int] = {}
        for account in accounts:
            self.add_account(account)
        self.save()

    def add_account(self, account: "Account") -> None:
        row = self.sheet.max_row + 1
        self.sheet.cell(row=row, column=1, value=excel_safe(account.header))
        for column in (2, 3, 4):
            self.sheet.cell(row=row, column=column, value=0)
        self.rows[account.index] = row

    def update(
        self, account: "Account", success: int, failed: int, total: int
    ) -> None:
        row = self.rows.get(account.index)
        if row is None:
            self.add_account(account)
            row = self.rows[account.index]
        self.sheet.cell(row=row, column=2, value=success)
        self.sheet.cell(row=row, column=3, value=failed)
        self.sheet.cell(row=row, column=4, value=total)
        self.save()

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.workbook.save(self.path)


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------

class LogStream(io.TextIOBase):
    """Redirect print() output from lightroom_auto into the GUI log."""

    def __init__(self, log_fn):
        self.log_fn = log_fn
        self._buffer = ""

    def write(self, text):
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip()
            if line:
                self.log_fn(line)
        return len(text)

    def flush(self):
        if self._buffer.strip():
            self.log_fn(self._buffer.strip())
            self._buffer = ""


class Account:
    def __init__(self, index: int, entry: dict):
        self.index = index
        self.header = entry["header"]
        self.key = entry["identity"]
        self.label = f"CK{index + 1}"
        self.client = lr.LightroomClient(cookie_header=self.header)
        self.catalog_id: str | None = None
        self.account_id: str | None = None
        self.storage: dict | None = None
        self.storage_at = 0.0
        self.healthy = True
        self.exhausted = False
        self.stats = {"success": 0, "failed": 0, "total": 0}
        self.flushed_total = 0

    def describe(self) -> str:
        return f"{self.label} [{self.key}]"


class Worker(threading.Thread):
    def __init__(self, config: dict, ui_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.config = config
        self.ui_queue = ui_queue
        self.stop_event = stop_event
        self.output_dir = config["output_dir"]
        self.pool: TitlePool | None = None
        self.accounts: list[Account] = []
        self.stats_writer: ExcelStatsWriter | None = None

    # -- logging --------------------------------------------------------
    def log(self, message: str) -> None:
        text = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self.ui_queue.put(("log", text))
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            with open(
                os.path.join(self.output_dir, LOG_FILENAME), "a", encoding="utf-8"
            ) as fh:
                fh.write(text + "\n")
        except Exception:
            pass

    # -- main -----------------------------------------------------------
    def run(self) -> None:
        stats = {
            "success": 0, "failed": 0, "skipped": 0,
            "duplicate": 0, "deleted": 0, "total": 0,
        }
        try:
            images = scan_images(self.config["image_dir"])
            entries = self.config["cookie_entries"]
            self.accounts = [
                Account(index, entry) for index, entry in enumerate(entries)
            ]
            total_tasks = len(images) * len(self.accounts)
            stats["total"] = total_tasks
            self.log(
                f"图片 {len(images)} 张，标题文件 "
                f"{len(self.config['title_files'])} 个，CK {len(self.accounts)} 个"
            )
            if not images:
                self.log("图片目录里没有可识别的图片，结束")
                return
            try:
                self.pool = TitlePool(self.config["title_files"])
            except ValueError as exc:
                self.log(f"标题文件错误: {exc}")
                return
            self.log(
                f"标题组合总数: {self.pool.total}（图片 {len(images)} 张 × "
                f"CK {len(self.accounts)} 个 = 约 {total_tasks} 个标题）"
            )
            if self.pool.total < total_tasks:
                self.log(
                    f"提示：标题组合数 {self.pool.total} 少于本轮标题生成"
                    f"次数 {total_tasks}，部分标题会重复使用（不影响上传）"
                )
            if self.config["delete_images"] and len(self.accounts) > 1:
                self.log(
                    "提示：已勾选删除源图片，只会在最后一个 CK 成功处理"
                    "每张图片后删除，前面的 CK 不会删除"
                )

            self.open_stats_writer()

            done_tasks = 0
            for acct in self.accounts:
                if self.stop_event.is_set():
                    self.log("收到停止信号，结束")
                    break
                self.setup_account(acct)
                if not acct.healthy:
                    self.flush_account(acct)
                    self.log(f"{acct.label} 不可用，跳过该 CK")
                    continue
                self.log(f"开始使用 {acct.describe()}")

                for image_index, image_path in enumerate(images):
                    if self.stop_event.is_set():
                        self.log("收到停止信号，结束")
                        break
                    if acct.exhausted or not acct.healthy:
                        break
                    filename = os.path.basename(image_path)
                    relpath = os.path.relpath(
                        image_path, self.config["image_dir"]
                    )
                    try:
                        size = os.path.getsize(image_path)
                    except OSError as exc:
                        self.log(f"读取文件失败 {relpath}: {exc}")
                        stats["failed"] += 1
                        done_tasks += 1
                        continue
                    outcome, deleted = self.process_image(
                        acct, image_index, image_path, relpath, filename, size
                    )
                    if outcome == "switch":
                        break
                    self.record_outcome(acct, outcome)
                    stats[outcome] += 1
                    done_tasks += 1
                    if deleted:
                        stats["deleted"] += 1
                    progress = (
                        f"进度 {done_tasks}/{total_tasks} | "
                        f"CK {acct.index + 1}/{len(self.accounts)} | "
                        f"图片 {image_index + 1}/{len(images)} | "
                        f"成功 {stats['success']} | 重复 {stats['duplicate']} | "
                        f"失败 {stats['failed']}"
                    )
                    self.ui_queue.put(("progress", progress))
                    time.sleep(0.4)

                self.flush_account(acct)
                st = acct.stats
                self.log(
                    f"{acct.label} 结束：成功 {st['success']}，"
                    f"失败 {st['failed']}，总数 {st['total']}"
                )
                if self.stop_event.is_set():
                    break

            self.log(
                f"结束：成功 {stats['success']}，重复跳过 {stats['duplicate']}，"
                f"失败 {stats['failed']}，跳过 {stats['skipped']}，"
                f"删除 {stats['deleted']}"
            )
            if self.stats_writer:
                self.log(
                    f"统计已写入 {os.path.join(self.output_dir, STATS_FILENAME)}"
                )
        except Exception as exc:
            self.log(f"运行异常: {exc}")
            self.log(traceback.format_exc())
        finally:
            self.flush_all_accounts()
            self.ui_queue.put(("done", stats))

    # -- accounts -------------------------------------------------------
    def open_stats_writer(self) -> None:
        path = os.path.join(self.output_dir, STATS_FILENAME)
        try:
            self.stats_writer = ExcelStatsWriter(path, self.accounts)
            self.log(
                f"统计文件: {path}（每个 CK 每 "
                f"{STATS_FLUSH_INTERVAL} 张更新一次）"
            )
        except Exception as exc:
            self.stats_writer = None
            self.log(f"创建 Excel 统计文件失败: {exc}")

    def setup_account(self, acct: Account) -> None:
        try:
            acct.catalog_id = lr.discover_catalog(acct.client)
            acct.account_id = lr.discover_account_id(acct.client, acct.catalog_id)
            if acct.account_id:
                acct.storage = lr.get_storage_info(acct.client, acct.account_id)
                acct.storage_at = time.time()
            self.log(
                f"{acct.describe()} 就绪，catalog={acct.catalog_id}"
                + (f"，account={acct.account_id}" if acct.account_id else "")
            )
            if acct.storage:
                used = int(acct.storage.get("used") or 0)
                limit = int(
                    acct.storage.get("display_limit")
                    or acct.storage.get("limit")
                    or 0
                )
                self.log(
                    f"{acct.label} 存储：已用 {lr.format_bytes(used)} / "
                    f"{lr.format_bytes(limit)}"
                )
        except Exception as exc:
            acct.healthy = False
            self.log(f"{acct.describe()} 不可用（cookie 无效或网络错误）: {exc}")

    def record_outcome(self, acct: Account, outcome: str) -> None:
        st = acct.stats
        if outcome == "success":
            st["success"] += 1
        elif outcome in ("failed", "duplicate"):
            st["failed"] += 1
        else:
            return
        st["total"] += 1
        if st["total"] % STATS_FLUSH_INTERVAL == 0:
            self.flush_account(acct)

    def flush_account(self, acct: Account) -> None:
        if not self.stats_writer:
            return
        st = acct.stats
        if st["total"] == acct.flushed_total:
            return
        try:
            self.stats_writer.update(
                acct, st["success"], st["failed"], st["total"]
            )
            acct.flushed_total = st["total"]
        except Exception as exc:
            self.log(f"写入 Excel 统计失败: {exc}")

    def flush_all_accounts(self) -> None:
        for acct in self.accounts:
            self.flush_account(acct)

    def refresh_storage(self, acct: Account, max_age: float = 30.0):
        if not acct.account_id:
            return None
        if acct.storage and time.time() - acct.storage_at < max_age:
            return acct.storage
        try:
            acct.storage = lr.get_storage_info(acct.client, acct.account_id)
            acct.storage_at = time.time()
        except lr.LightroomError as exc:
            self.log(f"{acct.label} 读取配额失败: {exc}")
        return acct.storage

    # -- per image ------------------------------------------------------
    def process_image(
        self,
        acct: Account,
        index: int,
        image_path: str,
        relpath: str,
        filename: str,
        size: int,
    ) -> tuple[str, bool]:
        # Mirror the web app: only same content hash AND same filename counts
        # as a duplicate.  A renamed copy of an uploaded photo is allowed.
        try:
            digest = sha256_file(image_path)
            duplicate = find_duplicate_asset(
                acct.client, acct.catalog_id, filename, digest
            )
        except Exception as exc:
            self.log(f"重复检查失败，继续上传 {relpath}: {exc}")
            duplicate = None
        if duplicate is not None:
            self.log(
                f"跳过 {relpath}: 已存在同名同内容图片"
                f"（asset {duplicate.get('id')}）"
            )
            return "duplicate", False

        title = self.pool.random_title()
        # Always fetch the latest quota right before uploading.
        storage = self.refresh_storage(acct, max_age=0)
        if storage:
            try:
                lr.ensure_space_for(storage, size)
            except lr.QuotaExceededError as exc:
                acct.exhausted = True
                self.log(f"{acct.label} 配额不足: {exc}")
                return "switch", False

        self.log(f"处理 {relpath} -> {acct.label}，标题: {title}")
        try:
            status, info = self.run_pipeline(
                acct, image_path, filename, title
            )
        except Exception as exc:
            self.log(f"处理 {relpath} 异常，跳过该图片: {exc}")
            self.log(traceback.format_exc())
            return "failed", False

        if status == "ok":
            final_title = info["title"]
            try:
                append_result_csv(
                    self.output_dir,
                    [
                        now_str(),
                        acct.label,
                        acct.key,
                        relpath,
                        final_title,
                        info["asset_id"],
                        info["album_id"],
                        info["space_id"],
                        info["share_url"],
                    ],
                )
            except OSError as exc:
                self.log(f"写入 results.csv 失败: {exc}")
            self.log(f"成功 {relpath} | {final_title} | {info['share_url']}")
            deleted = False
            # With several CKs the source file is needed by every CK, so only
            # delete after the last CK has successfully processed the image.
            is_last_ck = acct.index == len(self.accounts) - 1
            if self.config["delete_images"] and is_last_ck:
                try:
                    os.remove(image_path)
                    deleted = True
                    self.log(f"已删除源图片: {relpath}")
                except OSError as exc:
                    self.log(f"删除源图片失败 {relpath}: {exc}")
            return "success", deleted
        if status == "duplicate":
            return "duplicate", False
        if status == "quota_stop":
            acct.exhausted = True
            return "switch", False
        if status == "retry_account":
            acct.healthy = False
            return "switch", False
        return "failed", False

    # -- pipeline -------------------------------------------------------
    def run_pipeline(
        self, acct: Account, image_path: str, filename: str, title: str
    ) -> tuple[str, dict | None]:
        stream = LogStream(self.log)
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            return self._run_pipeline(acct, image_path, filename, title)

    def _run_pipeline(
        self, acct: Account, image_path: str, filename: str, title: str
    ) -> tuple[str, dict | None]:
        try:
            asset_id, asset = lr.upload_asset(
                acct.client,
                acct.catalog_id,
                image_path,
                retry_on_duplicate=False,
                send_if_none_match=False,
            )
        except lr.AlreadyUploadedError as exc:
            self.log(f"{acct.label} 图片已上传过，继续下一张: {filename}（{exc}）")
            return "duplicate", None
        except lr.LightroomError as exc:
            message = str(exc)
            self.log(f"{acct.label} 上传失败: {message}")
            if "HTTP 401" in message or "HTTP 403" in message:
                # A 403 can mean quota exhaustion or a permission problem.
                storage = self.refresh_storage(acct, max_age=0)
                if storage:
                    try:
                        lr.ensure_space_for(storage, os.path.getsize(image_path))
                    except lr.QuotaExceededError as quota_exc:
                        acct.exhausted = True
                        self.log(f"{acct.label} 配额不足: {quota_exc}")
                        return "quota_stop", None
                acct.healthy = False
                self.log(f"{acct.label} 可能 cookie 失效或无权限，跳过该账号")
                return "retry_account", None
            return "failed", None

        if not acct.account_id:
            imported_by = (
                ((asset.get("payload") or {}).get("importSource") or {})
                .get("importedBy")
            )
            if imported_by:
                acct.account_id = imported_by
                self.refresh_storage(acct, max_age=0)

        share = None
        for attempt in range(3):
            try:
                share = lr.create_share(
                    acct.client, acct.catalog_id, asset_id, asset
                )
                break
            except lr.LightroomError as exc:
                self.log(
                    f"{acct.label} 共享失败（第 {attempt + 1} 次）: {exc}"
                )
                if attempt == 2:
                    self.log(f"{filename} 已上传，但没有创建共享链接")
                    return "failed", None
                time.sleep(2)

        # Title update. The Lightroom API does not currently reject duplicate
        # titles, but if it (or a transient error) fails, generate a new title
        # and retry until success.
        final_title = title
        attempted = {final_title}
        max_title_attempts = 50
        title_set = False
        for attempt in range(1, max_title_attempts + 1):
            try:
                lr.set_asset_title(
                    acct.client, acct.catalog_id, asset_id, final_title
                )
                title_set = True
                break
            except lr.LightroomError as exc:
                message = str(exc)
                self.log(
                    f"{acct.label} 修改标题失败（第 {attempt} 次，"
                    f"标题 {final_title!r}）: {message}"
                )
                if "HTTP 401" in message or "HTTP 403" in message:
                    acct.healthy = False
                    self.log(f"{acct.label} 权限异常，停止该账号")
                    return "failed", None
                if attempt >= max_title_attempts:
                    self.log(
                        f"{filename} 已共享，但更换 {max_title_attempts} "
                        f"次标题仍未成功"
                    )
                    return "failed", None

                new_title = self.pool.random_title()
                guard = 0
                while (
                    new_title in attempted
                    and self.pool.total > len(attempted)
                    and guard < 200
                ):
                    new_title = self.pool.random_title()
                    guard += 1
                attempted.add(new_title)
                final_title = new_title
                self.log(f"换标题重试: {final_title!r}")
                time.sleep(1)

        if not title_set:
            return "failed", None

        # The share page's tab title is the shared album name.  The album was
        # created with the default date name, so rename it to the same title.
        for attempt in range(1, 4):
            try:
                lr.set_share_title(
                    acct.client,
                    acct.catalog_id,
                    share["album_id"],
                    final_title,
                )
                break
            except lr.LightroomError as exc:
                self.log(
                    f"{acct.label} 修改共享页标题失败（第 {attempt} 次）: {exc}"
                )
                if attempt == 3:
                    self.log(
                        f"{filename} 照片 Title 已设置，但共享页标题更新失败"
                    )
                    return "failed", None
                time.sleep(2)

        return "ok", {
            "asset_id": asset_id,
            "title": final_title,
            **share,
        }


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

BG = "#F3F4F6"
CARD = "#FFFFFF"
BORDER = "#E5E7EB"
TEXT = "#111827"
MUTED = "#6B7280"
ACCENT = "#2563EB"
ACCENT_DARK = "#1D4ED8"
DANGER = "#DC2626"
LOG_BG = "#0F172A"
LOG_FG = "#E2E8F0"
if sys.platform.startswith("win"):
    UI_FAMILY = "Segoe UI"
    MONO_FAMILY = "Consolas"
    SYMBOL_FAMILY = "Segoe UI Symbol"
    UI_SIZE, SMALL_SIZE, SECTION_SIZE = 11, 9, 13
    TITLE_SIZE, MONO_SIZE = 20, 10
elif sys.platform == "darwin":
    UI_FAMILY = "Helvetica Neue"
    MONO_FAMILY = "Menlo"
    SYMBOL_FAMILY = "Helvetica Neue"
    UI_SIZE, SMALL_SIZE, SECTION_SIZE = 13, 11, 15
    TITLE_SIZE, MONO_SIZE = 22, 11
else:
    UI_FAMILY = "DejaVu Sans"
    MONO_FAMILY = "DejaVu Sans Mono"
    SYMBOL_FAMILY = "DejaVu Sans"
    UI_SIZE, SMALL_SIZE, SECTION_SIZE = 12, 10, 14
    TITLE_SIZE, MONO_SIZE = 21, 10
UI_FONT = (UI_FAMILY, UI_SIZE)
SMALL_FONT = (UI_FAMILY, SMALL_SIZE)
SECTION_FONT = (UI_FAMILY, SECTION_SIZE, "bold")
TITLE_FONT = (UI_FAMILY, TITLE_SIZE, "bold")
MONO_FONT = (MONO_FAMILY, MONO_SIZE)
SYMBOL_FONT = (SYMBOL_FAMILY, UI_SIZE + 1, "bold")


class CheckRow(tk.Frame):
    """A checkbox that draws a blue check mark instead of the clam X."""

    def __init__(self, master, text, variable, command=None, bg=CARD):
        super().__init__(master, bg=bg)
        self.variable = variable
        self.command = command
        self.indicator = tk.Label(
            self, text="", width=2, height=1, bd=0, relief="flat",
            bg="#F8FAFC", fg="#FFFFFF", font=SYMBOL_FONT,
            highlightthickness=2, highlightbackground="#94A3B8",
            padx=1, pady=1,
        )
        self.indicator.pack(side="left")
        self.label = tk.Label(
            self, text=text, bg=bg, fg=TEXT, font=UI_FONT, anchor="w"
        )
        self.label.pack(side="left", padx=(8, 0))
        for widget in (self, self.indicator, self.label):
            widget.bind("<Button-1>", self._on_click)
            widget.configure(cursor="hand2")
        self.indicator.bind("<Enter>", self._on_enter)
        self.indicator.bind("<Leave>", self._on_leave)
        self.variable.trace_add("write", self._on_var_change)
        self._refresh()

    def _on_var_change(self, *_args):
        self._refresh()

    def _refresh(self):
        if self.variable.get():
            self.indicator.configure(
                text="✓", bg=ACCENT, fg="#FFFFFF",
                highlightbackground=ACCENT,
            )
        else:
            self.indicator.configure(
                text="", bg="#F8FAFC", fg="#FFFFFF",
                highlightbackground="#94A3B8",
            )

    def _on_click(self, _event=None):
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()
        return "break"

    def _on_enter(self, _event=None):
        if not self.variable.get():
            self.indicator.configure(highlightbackground=ACCENT)

    def _on_leave(self, _event=None):
        if not self.variable.get():
            self.indicator.configure(highlightbackground="#94A3B8")

    def toggle(self):
        self._on_click()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Lightroom 自动发布")
        self.state_data = load_state()
        geometry = self.state_data.get("window_geometry") or "1040x900"
        try:
            self.geometry(geometry)
        except Exception:
            self.geometry("1040x900")
        self.minsize(900, 700)

        self.ui_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Worker | None = None

        self.txt_files: list[str] = []
        self.title_vars: dict[str, tk.BooleanVar] = {}
        self.title_order: list[str] = []
        self.order_labels: dict[str, tk.Label] = {}

        self.image_dir_var = tk.StringVar()
        self.title_dir_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.cookie_file_var = tk.StringVar()
        self.delete_var = tk.BooleanVar(value=False)
        self.progress_var = tk.StringVar(value="就绪")

        self._setup_style()
        self._build_ui()
        self._restore_state()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self._poll_queue)
        self.after(20000, self._autosave)

    # -- UI -------------------------------------------------------------
    def _setup_style(self) -> None:
        self.configure(bg=BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=UI_FONT, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=UI_FONT)
        style.configure(
            "TButton", font=UI_FONT, padding=(12, 6), background=CARD,
            foreground=TEXT, bordercolor=BORDER, lightcolor=CARD,
            darkcolor=CARD, relief="flat",
        )
        style.map(
            "TButton",
            background=[("active", "#F3F4F6")],
            foreground=[("disabled", "#9CA3AF")],
        )
        style.configure(
            "Secondary.TButton", font=UI_FONT, padding=(12, 7),
            background=CARD, foreground=TEXT, bordercolor=BORDER,
            lightcolor=CARD, darkcolor=CARD, relief="flat",
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#F3F4F6")],
            foreground=[("disabled", "#9CA3AF")],
        )
        style.configure(
            "Accent.TButton", font=(UI_FONT[0], 13, "bold"),
            padding=(20, 10), background=ACCENT, foreground="#FFFFFF",
            bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
            relief="flat",
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_DARK), ("disabled", "#93C5FD")],
            foreground=[("disabled", "#EFF6FF")],
        )
        style.configure(
            "Danger.TButton", font=UI_FONT, padding=(12, 7),
            background="#FEF2F2", foreground=DANGER,
            bordercolor="#FECACA", lightcolor="#FEF2F2",
            darkcolor="#FEF2F2", relief="flat",
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#FEE2E2"), ("disabled", "#F9FAFB")],
            foreground=[("disabled", "#FCA5A5")],
        )
        style.configure(
            "Card.TCheckbutton", background=CARD, foreground=TEXT,
            font=UI_FONT, padding=4, indicatorbackground=CARD,
            indicatorforeground=ACCENT, indicatorrelief="flat",
            focuscolor=CARD,
        )
        style.map(
            "Card.TCheckbutton",
            background=[("active", CARD)],
            foreground=[("disabled", "#9CA3AF")],
        )
        style.configure(
            "Card.TEntry", fieldbackground=CARD, background=CARD,
            foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER,
            darkcolor=BORDER, padding=7, insertcolor=ACCENT,
        )
        style.map(
            "Card.TEntry",
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)],
        )
        style.configure(
            "Vertical.TScrollbar", background="#CBD5E1",
            troughcolor="#F1F5F9", bordercolor="#F1F5F9",
            arrowcolor="#64748B", gripcount=0, arrowsize=12,
        )
        style.configure(
            "Accent.Horizontal.TProgressbar", background=ACCENT,
            troughcolor="#E5E7EB", bordercolor="#E5E7EB",
            lightcolor=ACCENT, darkcolor=ACCENT, thickness=8,
        )

    def _card(self, parent) -> tk.Frame:
        return tk.Frame(
            parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1
        )

    def _section(
        self, card: tk.Frame, number: str, title: str, subtitle: str = ""
    ) -> tk.Frame:
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(
            row, text=number, bg="#EFF6FF", fg=ACCENT,
            font=(UI_FONT[0], 11, "bold"), width=3, pady=2,
        ).pack(side="left", padx=(0, 10))
        tk.Label(row, text=title, bg=CARD, fg=TEXT, font=SECTION_FONT).pack(
            side="left"
        )
        if subtitle:
            tk.Label(
                row, text=subtitle, bg=CARD, fg=MUTED, font=SMALL_FONT
            ).pack(side="left", padx=(10, 0))
        return row

    def _build_ui(self) -> None:
        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=16, pady=12)

        header = tk.Frame(main, bg=BG)
        header.pack(fill="x", pady=(0, 10))
        tk.Label(
            header, text="Lightroom 自动发布", bg=BG, fg=TEXT,
            font=TITLE_FONT,
        ).pack(anchor="w")
        sub = tk.Frame(header, bg=BG)
        sub.pack(anchor="w", pady=(4, 0))
        tk.Label(
            sub, text="上传图片 · 创建公开共享链接 · 修改标题",
            bg=BG, fg=MUTED, font=UI_FONT,
        ).pack(side="left")
        tk.Label(
            sub, text="多 CK 依次使用", bg="#E0E7FF", fg="#3730A3",
            font=SMALL_FONT, padx=8, pady=2,
        ).pack(side="left", padx=(10, 0))

        content = tk.Frame(main, bg=BG)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(3, weight=1)

        dir_card = self._card(content)
        dir_card.grid(
            row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 12)
        )
        self._section(dir_card, "1", "选择目录", "图片、标题、输出")
        body = tk.Frame(dir_card, bg=CARD)
        body.pack(fill="x", padx=16, pady=(0, 14))
        body.columnconfigure(1, weight=1)
        rows = [
            ("图片目录", self.image_dir_var, self.choose_image_dir),
            ("标题目录", self.title_dir_var, self.choose_title_dir),
            ("输出目录", self.output_dir_var, self.choose_output_dir),
        ]
        for index, (label, var, command) in enumerate(rows):
            tk.Label(
                body, text=label, bg=CARD, fg=MUTED, font=UI_FONT,
                width=8, anchor="w",
            ).grid(row=index, column=0, sticky="w", pady=4)
            ttk.Entry(
                body, textvariable=var, style="Card.TEntry"
            ).grid(row=index, column=1, sticky="ew", padx=(8, 8), pady=4)
            ttk.Button(
                body, text="浏览", style="Secondary.TButton", command=command
            ).grid(row=index, column=2, sticky="e", pady=4)

        tk.Label(
            dir_card,
            text="支持 JPG / PNG / HEIC / DNG / CR2 / NEF / ARW 等格式",
            bg=CARD, fg=MUTED, font=SMALL_FONT,
        ).pack(anchor="w", padx=16, pady=(0, 12))

        title_card = self._card(content)
        title_card.grid(
            row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 12)
        )
        self._section(title_card, "2", "标题文件")
        self.selected_count_var = tk.StringVar(value="已选 0 个")
        self.selected_count_label = tk.Label(
            title_card, textvariable=self.selected_count_var,
            bg="#EFF6FF", fg=ACCENT, font=(UI_FONT[0], 11, "bold"),
            padx=8, pady=3,
        )
        self.selected_count_label.place(relx=1.0, x=-16, y=14, anchor="ne")
        tk.Label(
            title_card,
            text="勾选顺序 = 拼接顺序；取消再勾选可调整",
            bg=CARD, fg=MUTED, font=SMALL_FONT,
        ).pack(side="bottom", anchor="w", padx=16, pady=(0, 10))
        title_wrap = tk.Frame(title_card, bg=CARD)
        title_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.title_canvas = tk.Canvas(
            title_wrap, height=120, bg=CARD, highlightthickness=0, bd=0
        )
        title_scroll = ttk.Scrollbar(
            title_wrap, orient="vertical", command=self.title_canvas.yview,
            style="Vertical.TScrollbar",
        )
        self.title_canvas.configure(yscrollcommand=title_scroll.set)
        title_scroll.pack(side="right", fill="y")
        self.title_canvas.pack(side="left", fill="both", expand=True)
        self.title_inner = tk.Frame(self.title_canvas, bg=CARD)
        self.title_window = self.title_canvas.create_window(
            (0, 0), window=self.title_inner, anchor="nw"
        )
        self.title_inner.bind(
            "<Configure>",
            lambda e: self.title_canvas.configure(
                scrollregion=self.title_canvas.bbox("all")
            ),
        )
        self.title_canvas.bind(
            "<Configure>",
            lambda e: self.title_canvas.itemconfigure(
                self.title_window, width=e.width
            ),
        )
        self.title_canvas.bind(
            "<Enter>",
            lambda e: self.title_canvas.bind_all(
                "<MouseWheel>", self._on_title_wheel
            ),
        )
        self.title_canvas.bind(
            "<Leave>", lambda e: self.title_canvas.unbind_all("<MouseWheel>")
        )

        cookie_card = self._card(content)
        cookie_card.grid(
            row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 12)
        )
        cookie_head = self._section(
            cookie_card, "3", "Cookie 文件", "每行一个 CK，依次使用"
        )
        ttk.Button(
            cookie_head, text="浏览", style="Secondary.TButton",
            command=self.choose_cookie_file,
        ).pack(side="right")
        cookie_body = tk.Frame(cookie_card, bg=CARD)
        cookie_body.pack(fill="x", padx=16, pady=(0, 8))
        cookie_body.columnconfigure(0, weight=1)
        self.cookie_file_entry = ttk.Entry(
            cookie_body, textvariable=self.cookie_file_var, style="Card.TEntry"
        )
        self.cookie_file_entry.grid(row=0, column=0, sticky="ew")
        self.cookie_file_entry.bind(
            "<Return>",
            lambda _event: self._refresh_cookie_file_info(log_changes=True),
        )
        self.cookie_file_entry.bind(
            "<FocusOut>", lambda _event: self._refresh_cookie_file_info()
        )
        self.cookie_count_var = tk.StringVar(value="未选择 Cookie 文件")
        tk.Label(
            cookie_body, textvariable=self.cookie_count_var,
            bg=CARD, fg=MUTED, font=SMALL_FONT, anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        tk.Label(
            cookie_card,
            text="按行读取，每行一个完整 Cookie；每个 CK 都会把所有图片"
                 "完整处理一遍，再切换到下一个 CK",
            bg=CARD, fg=MUTED, font=SMALL_FONT,
            wraplength=300, justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 12))

        options_card = self._card(content)
        options_card.grid(
            row=2, column=0, columnspan=2, sticky="nsew", pady=(0, 12)
        )
        options = tk.Frame(options_card, bg=CARD)
        options.pack(fill="x", padx=16, pady=(12, 6))
        self.delete_check = CheckRow(
            options, "成功后删除源图片", self.delete_var, self.save_state
        )
        self.delete_check.pack(side="left")
        self.start_btn = ttk.Button(
            options, text="开始发布", style="Accent.TButton", command=self.start
        )
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(
            options, text="停止", style="Danger.TButton",
            command=self.stop, state="disabled",
        )
        self.stop_btn.pack(side="right", padx=(0, 10))

        progress_row = tk.Frame(options_card, bg=CARD)
        progress_row.pack(fill="x", padx=16, pady=(0, 12))
        progress_row.columnconfigure(1, weight=1)
        tk.Label(
            progress_row, textvariable=self.progress_var, bg=CARD,
            fg=MUTED, font=SMALL_FONT, anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(
            progress_row, style="Accent.Horizontal.TProgressbar",
            mode="determinate", maximum=100, value=0,
        )
        self.progress_bar.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        log_card = self._card(content)
        log_card.grid(
            row=3, column=0, columnspan=2, sticky="nsew"
        )
        log_head = tk.Frame(log_card, bg=CARD)
        log_head.pack(fill="x", padx=16, pady=(10, 6))
        tk.Label(
            log_head, text="运行日志", bg=CARD, fg=TEXT, font=SECTION_FONT
        ).pack(side="left")
        ttk.Button(
            log_head, text="清空", style="Secondary.TButton",
            command=self.clear_log,
        ).pack(side="right")
        log_body = tk.Frame(log_card, bg=CARD)
        log_body.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.log_text = tk.Text(
            log_body, height=8, state="disabled", wrap="word",
            bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_FG,
            selectbackground="#334155", relief="flat", font=MONO_FONT,
            padx=10, pady=8,
        )
        log_scroll = ttk.Scrollbar(
            log_body, orient="vertical", command=self.log_text.yview,
            style="Vertical.TScrollbar",
        )
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("info", foreground=LOG_FG)
        self.log_text.tag_configure("success", foreground="#4ADE80")
        self.log_text.tag_configure("warn", foreground="#FBBF24")
        self.log_text.tag_configure("error", foreground="#F87171")
        self.log_text.tag_configure("muted", foreground="#94A3B8")

    def choose_cookie_file(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=(
                os.path.dirname(self.cookie_file_var.get())
                if self.cookie_file_var.get()
                else os.path.expanduser("~")
            ),
            title="选择 Cookie 文件（每行一个）",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.cookie_file_var.set(path)
        self._refresh_cookie_file_info(log_changes=True)
        self.save_state()

    def _refresh_cookie_file_info(self, log_changes: bool = False) -> None:
        path = self.cookie_file_var.get().strip()
        if not path:
            self.cookie_count_var.set("未选择 Cookie 文件")
            return
        if not os.path.isfile(path):
            self.cookie_count_var.set("文件不存在")
            return
        try:
            entries = read_cookie_entries_from_file(path)
        except OSError as exc:
            self.cookie_count_var.set(f"读取失败: {exc}")
            return
        self.cookie_count_var.set(f"文件内共 {len(entries)} 个 CK")
        if log_changes:
            self.log(f"Cookie 文件: {path}（{len(entries)} 个 CK）")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _restore_state(self) -> None:
        sd = self.state_data
        self.image_dir_var.set(sd.get("image_dir", "") or "")
        self.title_dir_var.set(sd.get("title_dir", "") or "")
        self.output_dir_var.set(sd.get("output_dir") or DEFAULT_OUTPUT_DIR)
        self.cookie_file_var.set(sd.get("cookie_file") or "")
        self.delete_var.set(bool(sd.get("delete_images", False)))
        self._refresh_cookie_file_info()

        if self.image_dir_var.get() and os.path.isdir(self.image_dir_var.get()):
            try:
                count = len(scan_images(self.image_dir_var.get()))
                self.log(f"已加载图片目录，共 {count} 张图片")
            except Exception:
                pass

        if self.title_dir_var.get() and os.path.isdir(self.title_dir_var.get()):
            self.load_title_files(
                restore_order=list(sd.get("selected_titles") or [])
            )
        else:
            tk.Label(
                self.title_inner,
                text="请选择标题目录",
                bg=CARD,
                fg=MUTED,
                font=SMALL_FONT,
            ).pack(anchor="w", padx=6, pady=6)

    def _on_title_wheel(self, event) -> None:
        self.title_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    # -- log ------------------------------------------------------------
    def log(self, message: str) -> None:
        tag = "info"
        if any(
            key in message
            for key in ("失败", "错误", "异常", "Traceback", "不可用", "权限")
        ):
            tag = "error"
        elif "成功" in message and "结束" not in message:
            tag = "success"
        elif any(
            key in message for key in ("重复", "跳过", "停止", "配额", "警告")
        ):
            tag = "warn"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", str(message) + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- title files ----------------------------------------------------
    def choose_image_dir(self) -> None:
        directory = filedialog.askdirectory(
            initialdir=self.image_dir_var.get() or os.path.expanduser("~"),
            title="选择图片目录",
        )
        if not directory:
            return
        self.image_dir_var.set(directory)
        try:
            count = len(scan_images(directory))
        except Exception:
            count = 0
        self.log(f"图片目录: {directory}（{count} 张图片）")
        self.save_state()

    def choose_title_dir(self) -> None:
        directory = filedialog.askdirectory(
            initialdir=self.title_dir_var.get() or os.path.expanduser("~"),
            title="选择标题目录",
        )
        if not directory:
            return
        saved_dir = os.path.abspath(self.state_data.get("title_dir") or "")
        restore = (
            list(self.state_data.get("selected_titles") or [])
            if saved_dir and os.path.abspath(directory) == saved_dir
            else []
        )
        self.title_dir_var.set(directory)
        self.load_title_files(restore_order=restore)
        self.save_state()

    def choose_output_dir(self) -> None:
        directory = filedialog.askdirectory(
            initialdir=self.output_dir_var.get() or os.path.expanduser("~"),
            title="选择结果输出目录",
        )
        if not directory:
            return
        self.output_dir_var.set(directory)
        self.log(f"输出目录: {directory}")
        self.save_state()

    def load_title_files(self, restore_order: list[str] | None = None) -> None:
        for child in self.title_inner.winfo_children():
            child.destroy()
        self.txt_files = []
        self.title_vars.clear()
        self.title_order = []
        self.order_labels.clear()

        directory = self.title_dir_var.get().strip()
        if not directory or not os.path.isdir(directory):
            tk.Label(
                self.title_inner,
                text="请选择标题目录",
                bg=CARD,
                fg=MUTED,
                font=SMALL_FONT,
            ).pack(anchor="w", padx=6, pady=6)
            return

        try:
            names = [
                name
                for name in os.listdir(directory)
                if name.lower().endswith(".txt")
                and os.path.isfile(os.path.join(directory, name))
            ]
        except OSError as exc:
            self.log(f"读取标题目录失败: {exc}")
            names = []
        names.sort(key=natural_key)
        self.txt_files = names

        if not names:
            tk.Label(
                self.title_inner,
                text="该目录下没有 .txt 文件",
                bg=CARD,
                fg=MUTED,
                font=SMALL_FONT,
            ).pack(anchor="w", padx=6, pady=6)
            return

        for name in names:
            row = tk.Frame(self.title_inner, bg=CARD)
            row.pack(fill="x", padx=4, pady=2)
            order_label = tk.Label(
                row,
                text="",
                width=3,
                anchor="center",
                bg=CARD,
                fg=ACCENT,
                font=(UI_FONT[0], 11, "bold"),
                padx=2,
                pady=2,
            )
            order_label.pack(side="left")
            var = tk.BooleanVar(value=False)
            check = CheckRow(
                row,
                name,
                var,
                lambda n=name: self.on_title_toggle(n),
            )
            check.pack(side="left", anchor="w", padx=(8, 0), fill="x", expand=True)
            order_label.bind("<Button-1>", lambda e, c=check: c.toggle())
            order_label.configure(cursor="hand2")
            self.title_vars[name] = var
            self.order_labels[name] = order_label

        if restore_order:
            for name in restore_order:
                if name in self.title_vars and name not in self.title_order:
                    self.title_order.append(name)
            for name in self.title_order:
                self.title_vars[name].set(True)
            self._update_order_labels()

        self.title_inner.update_idletasks()
        self.title_canvas.configure(scrollregion=self.title_canvas.bbox("all"))
        self.log(f"标题目录: {directory}（{len(names)} 个 txt）")

    def on_title_toggle(self, name: str) -> None:
        if self.title_vars[name].get():
            if name not in self.title_order:
                self.title_order.append(name)
        else:
            if name in self.title_order:
                self.title_order.remove(name)
        self._update_order_labels()
        self.save_state()

    def _update_order_labels(self) -> None:
        positions = {name: i + 1 for i, name in enumerate(self.title_order)}
        for name, label in self.order_labels.items():
            if name in positions:
                label.configure(
                    text=str(positions[name]), bg="#EFF6FF", fg=ACCENT
                )
            else:
                label.configure(text="", bg=CARD, fg=ACCENT)
        if hasattr(self, "selected_count_var"):
            self.selected_count_var.set(
                f"已选 {len(self.title_order)} 个"
            )

    # -- state ----------------------------------------------------------
    def save_state(self) -> None:
        if not hasattr(self, "cookie_file_entry"):
            return
        data = {
            "image_dir": self.image_dir_var.get().strip(),
            "title_dir": self.title_dir_var.get().strip(),
            "selected_titles": list(self.title_order),
            "delete_images": bool(self.delete_var.get()),
            "output_dir": self.output_dir_var.get().strip(),
            "cookie_file": self.cookie_file_var.get().strip(),
            "window_geometry": self.geometry(),
        }
        try:
            ensure_config_dir()
            atomic_write_json(STATE_PATH, data, mode=0o600)
            self.state_data = data
        except Exception as exc:
            if hasattr(self, "log_text"):
                self.log(f"保存设置失败: {exc}")

    def _autosave(self) -> None:
        try:
            self.save_state()
        finally:
            self.after(20000, self._autosave)

    # -- run ------------------------------------------------------------
    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        image_dir = self.image_dir_var.get().strip()
        title_dir = self.title_dir_var.get().strip()
        output_dir = self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR

        if not os.path.isdir(image_dir):
            messagebox.showerror("错误", "请选择有效的图片目录")
            return
        if not os.path.isdir(title_dir):
            messagebox.showerror("错误", "请选择有效的标题目录")
            return
        if not self.title_order:
            messagebox.showerror("错误", "请至少勾选一个标题文件")
            return
        title_paths = [os.path.join(title_dir, name) for name in self.title_order]
        for path in title_paths:
            if not os.path.isfile(path):
                messagebox.showerror("错误", f"标题文件不存在:\n{path}")
                return
            if not read_title_file(path):
                messagebox.showerror(
                    "错误", f"标题文件没有内容:\n{os.path.basename(path)}"
                )
                return

        cookie_file = self.cookie_file_var.get().strip()
        if not cookie_file or not os.path.isfile(cookie_file):
            messagebox.showerror("错误", "请选择有效的 Cookie 文件（.txt）")
            return
        try:
            cookie_entries = read_cookie_entries_from_file(cookie_file)
        except OSError as exc:
            messagebox.showerror("错误", f"读取 Cookie 文件失败:\n{exc}")
            return
        if not cookie_entries:
            messagebox.showerror(
                "错误", "Cookie 文件中没有有效的 Cookie（每行一个）"
            )
            return
        if Workbook is None:
            messagebox.showerror(
                "错误",
                "缺少 openpyxl，无法输出 Excel 统计。\n"
                "请先执行: pip install openpyxl",
            )
            return

        images = scan_images(image_dir)
        if not images:
            messagebox.showerror("错误", "图片目录中没有可识别的图片")
            return

        try:
            os.makedirs(output_dir, exist_ok=True)
            test_path = os.path.join(output_dir, ".write_test")
            with open(test_path, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(test_path)
        except OSError as exc:
            messagebox.showerror("错误", f"输出目录不可写:\n{exc}")
            return

        self.save_state()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.stop_event.clear()
        self.progress_bar.configure(value=0)
        self.progress_var.set(f"准备处理 {len(images)} 张图片...")
        self.log("=" * 70)
        self.log(
            f"开始：{len(images)} 张图片，{len(cookie_entries)} 个 CK，"
            f"{len(title_paths)} 个标题文件"
        )
        self.log(f"Cookie 文件: {cookie_file}")

        config = {
            "image_dir": image_dir,
            "title_files": title_paths,
            "output_dir": output_dir,
            "cookie_entries": cookie_entries,
            "delete_images": bool(self.delete_var.get()),
        }
        self.worker = Worker(config, self.ui_queue, self.stop_event)
        self.worker.start()

    def stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.stop_btn.configure(state="disabled")
            self.progress_var.set("正在停止（当前图片处理完后停止）...")
            self.log("收到停止请求，当前图片处理完后停止")

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "progress":
                    self.progress_var.set(payload)
                    match = re.search(r"进度\s+(\d+)/(\d+)", payload)
                    if match:
                        done = int(match.group(1))
                        total = int(match.group(2))
                        self.progress_bar.configure(
                            value=(done / total * 100) if total else 0
                        )
                elif kind == "done":
                    self._on_worker_done(payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _on_worker_done(self, stats: dict) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        total = stats.get("total", 0)
        processed = (
            stats.get("success", 0)
            + stats.get("failed", 0)
            + stats.get("duplicate", 0)
            + stats.get("skipped", 0)
        )
        if total and processed < total:
            self.progress_bar.configure(value=processed / total * 100)
        else:
            self.progress_bar.configure(value=100)
        summary = (
            f"完成：成功 {stats.get('success', 0)}，"
            f"重复跳过 {stats.get('duplicate', 0)}，"
            f"失败 {stats.get('failed', 0)}，跳过 {stats.get('skipped', 0)}，"
            f"删除 {stats.get('deleted', 0)}"
        )
        unprocessed = total - processed
        if unprocessed > 0:
            summary += f"，未处理 {unprocessed}"
        self.progress_var.set(summary)
        self.log(summary)
        self.worker = None

    # -- close ----------------------------------------------------------
    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "退出", "任务正在运行，确定退出吗？\n（会在当前图片处理完后停止）"
            ):
                return
            self.stop_event.set()
            self.worker.join(timeout=2)
        self.save_state()
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
