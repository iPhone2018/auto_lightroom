#!/usr/bin/env python3
"""Upload a photo to Adobe Lightroom Web, create a public share link, and set
the photo's own Title metadata (XMP dc:title).

The flow mirrors what the Lightroom web app does:

  1. POST /v2/catalogs/{catalogId}/assets        (multipart: revision + master)
  2. PUT  /v2/catalogs/{catalogId}/albums/{albumId}          (create share album)
  3. POST /v2/catalogs/{catalogId}/albums/{albumId}          (presentation settings)
  4. PUT  /v2/catalogs/{catalogId}/albums/{albumId}/assets   (add the asset)
  5. PUT  /v2/catalogs/{catalogId}/spaces/{spaceId}          (make it public)
  6. GET  /v2/catalogs/{catalogId}/spaces/{spaceId}?embed=... (read the share URL)
  7. PUT  /v2/catalogs/{catalogId}/assets/{assetId}/revisions/{revisionId}/children/{childId}
     (set the photo Title; body: {"payload":{"xmp":{"dc":{"title":"..."}}, ...}})

Only the Python standard library is used.  Authentication is plain browser
cookies exported from an authenticated lightroom.adobe.com session.

Usage:
    # Upload + public share + set the photo's Title
    python3 lightroom_auto.py --image photo.jpg --title "黄昏落日2"

    # Only change the Title of an existing photo
    python3 lightroom_auto.py --asset-id 9d26e55495601fd510186ccf2a378121 --title "黄昏落日2"

    # Optionally also set the share page title (shared album name)
    python3 lightroom_auto.py --image photo.jpg --title "黄昏落日2" --share-title "我的共享相册"
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
        "if-none-match": "*",
    }

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="上传图片到 Lightroom Web，创建公开共享链接，并修改照片自身的 Title"
    )
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--image", help="本地图片路径（上传并共享）")
    source.add_argument("--asset-id", help="已有照片的 asset id（只改 Title）")
    parser.add_argument("--title", help="照片自身的 Title（XMP dc:title）")
    parser.add_argument(
        "--share-title", help="可选：同时修改共享页标题（共享相册名称）"
    )
    parser.add_argument(
        "--no-share", action="store_true", help="只上传并改 Title，不创建公开共享"
    )
    parser.add_argument(
        "--check-quota", action="store_true", help="只查询账号存储配额后退出"
    )
    parser.add_argument(
        "--account-id",
        help="Lightroom account id（配额检查用；不填则从已有照片自动获取）",
    )
    parser.add_argument(
        "--skip-quota-check", action="store_true", help="上传前不检查剩余存储"
    )
    parser.add_argument(
        "--cookies",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures", "cookies-adobe.json"),
        help="Adobe cookies JSON（默认 captures/cookies-adobe.json）",
    )
    parser.add_argument("--catalog", help="catalog id（不填则自动获取）")
    args = parser.parse_args()

    if not args.image and not args.asset_id and not args.check_quota:
        parser.error("需要 --image、--asset-id 或 --check-quota")
    if args.check_quota and (args.image or args.asset_id):
        parser.error("--check-quota 需要单独使用")
    if not args.check_quota and not args.title:
        parser.error("--title 必填")
    if args.image and not os.path.isfile(args.image):
        print(f"文件不存在: {args.image}", file=sys.stderr)
        return 2
    if args.no_share and args.share_title:
        print("--no-share 和 --share-title 不能同时使用", file=sys.stderr)
        return 2
    if not os.path.isfile(args.cookies):
        print(f"cookies 文件不存在: {args.cookies}", file=sys.stderr)
        return 2

    try:
        client = LightroomClient(args.cookies)
        catalog_id = args.catalog or discover_catalog(client)
        print(f"catalog id: {catalog_id}")

        account_id = args.account_id or discover_account_id(client, catalog_id)
        if account_id:
            print(f"account id: {account_id}")

        if args.check_quota:
            if not account_id:
                print(
                    "无法自动获取 account id（图库为空）。请用 --account-id 指定。",
                    file=sys.stderr,
                )
                return 1
            print_quota(get_storage_info(client, account_id))
            return 0

        if args.asset_id:
            set_asset_title(client, catalog_id, args.asset_id, args.title)
            print("\n完成")
            print(f"  asset id  : {args.asset_id}")
            print(f"  照片 Title: {args.title}")
            return 0

        file_size = os.path.getsize(args.image)
        if not args.skip_quota_check:
            if account_id:
                try:
                    storage = get_storage_info(client, account_id)
                    print_quota(storage)
                    ensure_space_for(storage, file_size)
                except QuotaExceededError:
                    raise
                except LightroomError as exc:
                    # Quota lookup failure shouldn't block the upload; the
                    # server enforces the quota anyway.
                    print(f"[配额] 检查失败，继续上传: {exc}", file=sys.stderr)
            else:
                print("[配额] 无法自动获取 account id，跳过配额检查", file=sys.stderr)

        asset_id, asset = upload_asset(client, catalog_id, args.image)
        result: dict[str, Any] = {"asset_id": asset_id}
        if not args.no_share:
            result.update(create_share(client, catalog_id, asset_id, asset))
            if args.share_title:
                set_share_title(client, catalog_id, result["album_id"], args.share_title)
                result["share_title"] = args.share_title

        set_asset_title(client, catalog_id, asset_id, args.title)
    except QuotaExceededError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        print(
            "提示: 可在 Lightroom 的 Deleted 中永久删除照片，或升级存储计划。",
            file=sys.stderr,
        )
        return 1
    except LightroomError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print("\n完成")
    print(f"  asset id  : {result['asset_id']}")
    if "album_id" in result:
        print(f"  album id  : {result['album_id']}")
        print(f"  space id  : {result['space_id']}")
        print(f"  共享链接  : {result['share_url']}")
    if "share_title" in result:
        print(f"  共享页标题: {result['share_title']}")
    print(f"  照片 Title: {args.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
