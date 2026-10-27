#!/usr/bin/env python3
"""Tkinter GUI for the Lightroom upload / share / title pipeline.

Features:
  * choose an image directory and a title directory
  * ordered selection of .txt title files (checking order = concatenation order)
  * per-account local history: uploaded file names and used titles never repeat
  * cookies box (one account per line, or a JSON cookie dump)
  * optional delete-source-image after a fully successful pipeline
  * result CSV + run log in the output directory
  * all last-used settings (including cookies, mode 0600) are persisted

Run:
    python3 lightroom_gui.py
"""

from __future__ import annotations

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

import lightroom_auto as lr

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".lightroom_auto_gui")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
DEFAULT_OUTPUT_DIR = os.path.join(APP_DIR, "output")

MODE_ROUND_ROBIN = "轮流使用"
MODE_SEQUENTIAL = "顺序使用"
MODES = (MODE_ROUND_ROBIN, MODE_SEQUENTIAL)

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif",
    ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2",
    ".orf", ".rw2", ".raf", ".pef", ".srw", ".3fr", ".fff", ".mrw", ".iiq",
    ".x3f",
}

RESULTS_FILENAME = "results.csv"
LOG_FILENAME = "run.log"
SHARE_LINKS_FILENAME = "结果链接.txt"
RESULT_HEADERS = [
    "时间", "账号", "账号标识", "图片文件", "标题",
    "asset_id", "album_id", "space_id", "共享链接",
]


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


def read_title_file(path: str) -> list[str]:
    with open(path, "rb") as fh:
        raw = fh.read()
    text: str | None = None
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig", "replace")
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", "replace")
    else:
        for encoding in ("utf-8", "gb18030", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
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


def append_share_link(output_dir: str, url: str) -> None:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, SHARE_LINKS_FILENAME)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(url.rstrip() + "\n")


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
        self.label = f"账号{index + 1}"
        self.client = lr.LightroomClient(cookie_header=self.header)
        self.catalog_id: str | None = None
        self.account_id: str | None = None
        self.storage: dict | None = None
        self.storage_at = 0.0
        self.healthy = True
        self.exhausted = False
        self.uploaded_count = 0

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
        self.quota_stop = False

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
            stats["total"] = len(images)
            self.log(
                f"图片 {len(images)} 张，标题文件 {len(self.config['title_files'])} 个，"
                f"账号 {len(self.config['cookie_entries'])} 个"
            )
            if not images:
                self.log("图片目录里没有可识别的图片，结束")
                return
            try:
                self.pool = TitlePool(self.config["title_files"])
            except ValueError as exc:
                self.log(f"标题文件错误: {exc}")
                return
            self.log(f"标题组合总数: {self.pool.total}")

            self.setup_accounts()
            if not any(a.healthy and not a.exhausted for a in self.accounts):
                self.log("没有可用账号，结束")
                return

            for index, image_path in enumerate(images):
                if self.quota_stop:
                    self.log("配额不足，已停止全部流程")
                    break
                if self.stop_event.is_set():
                    self.log("收到停止信号，结束")
                    break
                filename = os.path.basename(image_path)
                relpath = os.path.relpath(image_path, self.config["image_dir"])
                try:
                    size = os.path.getsize(image_path)
                except OSError as exc:
                    self.log(f"读取文件失败 {relpath}: {exc}")
                    stats["failed"] += 1
                    continue
                outcome, deleted = self.process_image(
                    index, image_path, relpath, filename, size
                )
                if outcome == "quota_stop":
                    self.log("配额不足，已停止全部流程")
                    break
                stats[outcome] += 1
                if deleted:
                    stats["deleted"] += 1
                progress = (
                    f"进度 {index + 1}/{len(images)} | 成功 {stats['success']} | "
                    f"重复 {stats['duplicate']} | 失败 {stats['failed']} | "
                    f"跳过 {stats['skipped']} | 删除 {stats['deleted']}"
                )
                self.ui_queue.put(("progress", progress))
                time.sleep(0.4)
            self.log(
                f"结束：成功 {stats['success']}，重复跳过 {stats['duplicate']}，"
                f"失败 {stats['failed']}，跳过 {stats['skipped']}，"
                f"删除 {stats['deleted']}"
            )
        except Exception as exc:
            self.log(f"运行异常: {exc}")
            self.log(traceback.format_exc())
        finally:
            self.ui_queue.put(("done", stats))

    # -- accounts -------------------------------------------------------
    def setup_accounts(self) -> None:
        for entry in self.config["cookie_entries"]:
            acct = Account(len(self.accounts), entry)
            self.accounts.append(acct)
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
                self.log(f"{acct.label} 不可用（cookie 无效或网络错误）: {exc}")

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

    def candidate_accounts(self, index: int) -> list[Account]:
        count = len(self.accounts)
        if not count:
            return []
        if self.config["multi_mode"] == MODE_ROUND_ROBIN:
            start = index % count
            return [self.accounts[(start + i) % count] for i in range(count)]
        return list(self.accounts)

    # -- per image ------------------------------------------------------
    def process_image(
        self, index: int, image_path: str, relpath: str, filename: str, size: int
    ) -> tuple[str, bool]:
        reasons: list[str] = []
        for acct in self.candidate_accounts(index):
            if not acct.healthy or acct.exhausted:
                continue

            title = self.pool.random_title()
            # Always fetch the latest quota right before uploading.
            storage = self.refresh_storage(acct, max_age=0)
            if storage:
                try:
                    lr.ensure_space_for(storage, size)
                except lr.QuotaExceededError as exc:
                    self.quota_stop = True
                    self.log(f"{acct.label} 配额不足，停止全部流程: {exc}")
                    return "quota_stop", False

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
                acct.uploaded_count += 1
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
                self.log(
                    f"成功 {relpath} | {final_title} | {info['share_url']}"
                )
                deleted = False
                if self.config["delete_images"]:
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
                return "quota_stop", False
            if status == "retry_account":
                reasons.append(f"{acct.label} 不可用")
                continue
            return "failed", False

        if reasons:
            self.log(f"跳过 {relpath}: " + "；".join(reasons))
        else:
            self.log(f"跳过 {relpath}: 没有可用账号")
        return "skipped", False

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
                        self.quota_stop = True
                        self.log(
                            f"{acct.label} 配额不足，停止全部流程: {quota_exc}"
                        )
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

        # Save every generated share link as soon as it exists, even if the
        # title update below fails.
        try:
            append_share_link(self.output_dir, share["share_url"])
            self.log(
                f"共享链接已写入 {SHARE_LINKS_FILENAME}: {share['share_url']}"
            )
        except OSError as exc:
            self.log(f"写入 {SHARE_LINKS_FILENAME} 失败: {exc}")

        # Title update. The Lightroom API does not currently reject duplicate
        # titles, but if it (or a transient error) fails, generate a new title
        # and retry until success.
        final_title = title
        attempted = {final_title}
        max_title_attempts = 50
        for attempt in range(1, max_title_attempts + 1):
            try:
                lr.set_asset_title(
                    acct.client, acct.catalog_id, asset_id, final_title
                )
                return "ok", {
                    "asset_id": asset_id,
                    "title": final_title,
                    **share,
                }
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

        return "failed", None


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Lightroom 自动上传 / 共享 / 改标题")
        self.state_data = load_state()
        geometry = self.state_data.get("window_geometry") or "1000x900"
        try:
            self.geometry(geometry)
        except Exception:
            self.geometry("1000x900")
        self.minsize(820, 700)

        self.ui_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Worker | None = None

        self.txt_files: list[str] = []
        self.title_vars: dict[str, tk.BooleanVar] = {}
        self.title_order: list[str] = []
        self.order_labels: dict[str, ttk.Label] = {}

        self.image_dir_var = tk.StringVar()
        self.title_dir_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.delete_var = tk.BooleanVar(value=False)
        self.multi_mode_var = tk.StringVar(value=MODE_ROUND_ROBIN)
        self.progress_var = tk.StringVar(value="就绪")

        self._build_ui()
        self._restore_state()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self._poll_queue)
        self.after(20000, self._autosave)

    # -- UI -------------------------------------------------------------
    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)

        dirs = ttk.Frame(main)
        dirs.pack(fill="x")
        dirs.columnconfigure(1, weight=1)

        ttk.Label(dirs, text="图片目录").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Entry(dirs, textvariable=self.image_dir_var).grid(
            row=0, column=1, sticky="ew", pady=3
        )
        ttk.Button(dirs, text="选择...", command=self.choose_image_dir).grid(
            row=0, column=2, padx=(8, 0), pady=3
        )

        ttk.Label(dirs, text="标题目录").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Entry(dirs, textvariable=self.title_dir_var).grid(
            row=1, column=1, sticky="ew", pady=3
        )
        ttk.Button(dirs, text="选择...", command=self.choose_title_dir).grid(
            row=1, column=2, padx=(8, 0), pady=3
        )

        ttk.Label(dirs, text="输出目录").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Entry(dirs, textvariable=self.output_dir_var).grid(
            row=2, column=1, sticky="ew", pady=3
        )
        ttk.Button(dirs, text="选择...", command=self.choose_output_dir).grid(
            row=2, column=2, padx=(8, 0), pady=3
        )

        title_frame = ttk.LabelFrame(
            main, text="标题文件（勾选顺序 = 拼接顺序；取消再勾选可调整顺序）"
        )
        title_frame.pack(fill="both", expand=False, pady=(8, 0))
        self.title_canvas = tk.Canvas(title_frame, height=150, highlightthickness=0)
        title_scroll = ttk.Scrollbar(
            title_frame, orient="vertical", command=self.title_canvas.yview
        )
        self.title_canvas.configure(yscrollcommand=title_scroll.set)
        title_scroll.pack(side="right", fill="y")
        self.title_canvas.pack(side="left", fill="both", expand=True)
        self.title_inner = ttk.Frame(self.title_canvas)
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

        cookies_frame = ttk.LabelFrame(
            main, text="Cookies（每行一个账号；也可以粘贴一个 JSON）"
        )
        cookies_frame.pack(fill="x", pady=(8, 0))
        hint = ttk.Label(
            cookies_frame,
            text="Cookie 会保存在 ~/.lightroom_auto_gui/state.json（权限 600）",
            foreground="#888",
        )
        hint.pack(side="bottom", anchor="w", padx=4, pady=(0, 2))
        cookie_text_frame = ttk.Frame(cookies_frame)
        cookie_text_frame.pack(fill="both", expand=True, padx=2, pady=2)
        self.cookies_text = tk.Text(
            cookie_text_frame, height=5, wrap="word", undo=True
        )
        cookie_scroll = ttk.Scrollbar(
            cookie_text_frame, orient="vertical", command=self.cookies_text.yview
        )
        self.cookies_text.configure(yscrollcommand=cookie_scroll.set)
        cookie_scroll.pack(side="right", fill="y")
        self.cookies_text.pack(side="left", fill="both", expand=True)

        options = ttk.Frame(main)
        options.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(
            options,
            text="成功后删除源图片",
            variable=self.delete_var,
            command=self.save_state,
        ).pack(side="left")
        ttk.Label(options, text="    多账号:").pack(side="left", padx=(16, 4))
        self.multi_combo = ttk.Combobox(
            options,
            textvariable=self.multi_mode_var,
            values=list(MODES),
            state="readonly",
            width=10,
        )
        self.multi_combo.pack(side="left")
        self.multi_combo.bind(
            "<<ComboboxSelected>>", lambda e: self.save_state()
        )
        self.start_btn = ttk.Button(options, text="开始", command=self.start)
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(
            options, text="停止", command=self.stop, state="disabled"
        )
        self.stop_btn.pack(side="right", padx=(0, 8))
        ttk.Label(options, textvariable=self.progress_var).pack(
            side="left", padx=(20, 0)
        )

        log_frame = ttk.LabelFrame(main, text="运行日志")
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = ScrolledText(
            log_frame, height=12, state="disabled", wrap="word"
        )
        self.log_text.pack(fill="both", expand=True, padx=2, pady=2)

    def _restore_state(self) -> None:
        sd = self.state_data
        self.image_dir_var.set(sd.get("image_dir", "") or "")
        self.title_dir_var.set(sd.get("title_dir", "") or "")
        self.output_dir_var.set(sd.get("output_dir") or DEFAULT_OUTPUT_DIR)
        self.delete_var.set(bool(sd.get("delete_images", False)))
        mode = sd.get("multi_mode") or MODE_ROUND_ROBIN
        if mode not in MODES:
            mode = MODE_ROUND_ROBIN
        self.multi_mode_var.set(mode)
        cookies = sd.get("cookies") or ""
        if cookies:
            self.cookies_text.insert("1.0", cookies)

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
            ttk.Label(
                self.title_inner,
                text="请选择标题目录",
                foreground="#888",
            ).pack(anchor="w", padx=6, pady=6)

    def _on_title_wheel(self, event) -> None:
        self.title_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    # -- log ------------------------------------------------------------
    def log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", str(message) + "\n")
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
            ttk.Label(
                self.title_inner, text="请选择标题目录", foreground="#888"
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
            ttk.Label(
                self.title_inner,
                text="该目录下没有 .txt 文件",
                foreground="#888",
            ).pack(anchor="w", padx=6, pady=6)
            return

        for name in names:
            row = ttk.Frame(self.title_inner)
            row.pack(fill="x", padx=4, pady=1)
            order_label = ttk.Label(row, width=4, anchor="e")
            order_label.pack(side="left")
            var = tk.BooleanVar(value=False)
            check = ttk.Checkbutton(
                row,
                text=name,
                variable=var,
                command=lambda n=name: self.on_title_toggle(n),
            )
            check.pack(side="left", anchor="w")
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
            label.configure(text=f"{positions[name]}." if name in positions else "")

    # -- state ----------------------------------------------------------
    def save_state(self) -> None:
        if not hasattr(self, "cookies_text"):
            return
        data = {
            "image_dir": self.image_dir_var.get().strip(),
            "title_dir": self.title_dir_var.get().strip(),
            "selected_titles": list(self.title_order),
            "delete_images": bool(self.delete_var.get()),
            "output_dir": self.output_dir_var.get().strip(),
            "cookies": self.cookies_text.get("1.0", "end").rstrip("\n"),
            "multi_mode": self.multi_mode_var.get(),
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

        cookie_entries = [
            entry
            for entry in parse_cookie_entries(
                self.cookies_text.get("1.0", "end").strip()
            )
            if "=" in entry["header"]
        ]
        if not cookie_entries:
            messagebox.showerror("错误", "请填写有效的 Cookies")
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
        self.progress_var.set(f"准备处理 {len(images)} 张图片...")
        self.log("=" * 70)
        self.log(
            f"开始：{len(images)} 张图片，{len(cookie_entries)} 个账号，"
            f"{len(title_paths)} 个标题文件，模式 {self.multi_mode_var.get()}"
        )

        config = {
            "image_dir": image_dir,
            "title_files": title_paths,
            "output_dir": output_dir,
            "cookie_entries": cookie_entries,
            "delete_images": bool(self.delete_var.get()),
            "multi_mode": self.multi_mode_var.get(),
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
                elif kind == "done":
                    self._on_worker_done(payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _on_worker_done(self, stats: dict) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        summary = (
            f"完成：成功 {stats.get('success', 0)}，"
            f"重复跳过 {stats.get('duplicate', 0)}，"
            f"失败 {stats.get('failed', 0)}，跳过 {stats.get('skipped', 0)}，"
            f"删除 {stats.get('deleted', 0)}"
        )
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
