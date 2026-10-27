#!/usr/bin/env python3
"""Stream important GUI / run.log events to stdout (for the Monitor tool)."""

from __future__ import annotations

import json
import os
import re
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(os.path.expanduser("~"), ".lightroom_auto_gui", "state.json")
DEFAULT_OUTPUT_DIR = os.path.join(APP_DIR, "output")
GUI_STDOUT = "/tmp/lightroom_gui_stdout.log"

IMPORTANT = re.compile(
    r"图片 \d+ 张|标题组合总数|图片已上传过|配额不足|已停止全部流程|"
    r"结束：|运行异常|Traceback|不可用|标题文件错误|没有可用账号|"
    r"上传失败|网络错误|共享失败|修改标题失败|换标题重试|"
    r"写入 .*失败|删除源图片失败|收到停止|失败 |跳过 "
)
SUCCESS = re.compile(r"\] 成功 ")
GUI_ERROR = re.compile(r"Traceback|Error|Exception|错误|异常", re.IGNORECASE)


def emit(text: str) -> None:
    print(text.rstrip(), flush=True)


def configured_output_dir() -> str:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        value = (data.get("output_dir") or "").strip()
        if value:
            return value
    except Exception:
        pass
    return DEFAULT_OUTPUT_DIR


def read_new(path: str, offset: int) -> tuple[int, list[str], bool]:
    """Return (new_offset, lines, truncated)."""
    if not os.path.exists(path):
        return 0, [], False
    try:
        size = os.path.getsize(path)
        truncated = offset > size
        if truncated:
            offset = 0
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            data = fh.read()
            new_offset = fh.tell()
    except OSError:
        return offset, [], False
    return new_offset, data.splitlines(), truncated


def main() -> None:
    run_dir = configured_output_dir()
    run_path = os.path.join(run_dir, "run.log")
    run_offset = os.path.getsize(run_path) if os.path.exists(run_path) else 0
    out_offset = os.path.getsize(GUI_STDOUT) if os.path.exists(GUI_STDOUT) else 0
    in_run = False
    success_count = 0

    emit(f"[监控] 已启动，运行日志: {run_path}")
    while True:
        if not in_run:
            new_dir = configured_output_dir()
            new_path = os.path.join(new_dir, "run.log")
            if os.path.abspath(new_path) != os.path.abspath(run_path):
                run_path = new_path
                run_offset = os.path.getsize(run_path) if os.path.exists(run_path) else 0
                emit(f"[监控] 切换运行日志: {run_path}")

        out_offset, out_lines, out_trunc = read_new(GUI_STDOUT, out_offset)
        if out_trunc:
            emit("[监控] GUI stdout 日志被重建")
        for line in out_lines:
            if GUI_ERROR.search(line):
                emit(f"[GUI] {line}")

        run_offset, run_lines, run_trunc = read_new(run_path, run_offset)
        if run_trunc:
            emit("[监控] run.log 被重建，从头读取")

        for line in run_lines:
            if "图片 " in line and " 张，" in line:
                in_run = True
                success_count = 0
                emit(line)
                continue
            if SUCCESS.search(line) and "结束：" not in line:
                success_count += 1
                if success_count == 1 or success_count % 10 == 0:
                    emit(f"[进度] 已成功 {success_count} 张 | {line}")
                continue
            if IMPORTANT.search(line):
                emit(line)
                if "结束：" in line or "运行异常" in line:
                    in_run = False
                    success_count = 0

        time.sleep(0.8)


if __name__ == "__main__":
    main()
