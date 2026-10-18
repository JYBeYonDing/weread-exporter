#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sequentially export every book in a WeRead category.

A thin driver: list the category, then run `python -m weread_exporter -o md -o pdf`
for each book, one at a time. All "is it done / what is missing" logic lives in
the exporter and its `<output-dir>/<title>/export_summary.json`; this script keeps
no state of its own, so re-running the same command simply continues.

    .venv\\Scripts\\python.exe scripts/batch_category.py --category 1100001 \\
        --output-dir Z:\\share\\books
    .venv\\Scripts\\python.exe scripts/batch_category.py --list-only
    .venv\\Scripts\\python.exe scripts/batch_category.py --limit 3

Exit codes: 0 finished, 2 stopped because the cookie expired.
"""
from __future__ import print_function

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from weread_exporter import utils  # noqa: E402
from weread_exporter.__main__ import find_book_summary, plan_book  # noqa: E402

DEFAULT_CATEGORY = "1100001"
DEFAULT_OUTPUT_DIR = r"Z:\share\books"
FORMATS = ["md", "pdf"]
PAGE_SIZE = 20
# The listing endpoint returns an empty page past a few hundred entries; the cap
# keeps a changed API from becoming an endless loop.
MAX_PAGES = 400


def make_logger(path):
    """Return log(line): print to stdout and append to a UTF-8 file."""
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    fp = io.open(path, "a", encoding="utf-8")

    def log(line=""):
        print(line, flush=True)
        fp.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
        fp.flush()

    return log


def encode_id(book_info):
    """Encoded book id (what `-b` takes) from readerLink, or deepLink's `v=`."""
    tail = (book_info.get("readerLink") or "").rstrip("/").split("/")[-1]
    if tail and "?" not in tail:
        return tail
    deep_link = book_info.get("deepLink") or ""
    pos = deep_link.find("v=")
    return deep_link[pos + 2:].split("&")[0] if pos >= 0 else ""


async def fetch_category_books(category_id):
    """Page the category listing until it runs dry; returns [{bookId, title}, ...].

    The listing's numeric `bookId` is not what the exporter uses — the encoded id
    from `readerLink` is. Duplicates across page boundaries are dropped.
    """
    books, seen = [], set()
    for page in range(MAX_PAGES):
        url = "https://weread.qq.com/web/bookListInCategory/%s?maxIndex=%d" % (
            category_id, page * PAGE_SIZE)
        payload = json.loads((await utils.fetch(url)).decode("utf-8"))
        entries = payload.get("books") or []
        for entry in entries:
            info = entry.get("bookInfo") or {}
            book_id = encode_id(info)
            if book_id and book_id not in seen:
                seen.add(book_id)
                books.append({"bookId": book_id, "title": info.get("title") or book_id})
        if not entries or not payload.get("hasMore"):
            break
        await asyncio.sleep(0.3)
    return books


def run_export(book_id, output_dir, args):
    """Run the exporter for one book; returns its exit code (-9 on timeout)."""
    cmd = [sys.executable, "-m", "weread_exporter", "-b", book_id,
           "--output-dir", output_dir, "--load-interval", str(args.load_interval)]
    for name in FORMATS:
        cmd += ["-o", name]
    if args.load_timeout:
        cmd += ["--load-timeout", str(args.load_timeout)]
    for flag, enabled in (("--force", args.force), ("--no-check", args.no_check)):
        if enabled:
            cmd.append(flag)
    try:
        return subprocess.run(cmd, cwd=REPO, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=args.book_timeout).returncode
    except subprocess.TimeoutExpired:
        return -9


def main(argv=None):
    ap = argparse.ArgumentParser(prog="batch_category",
                                 description="一本一本导出微信读书某个类目下的所有书籍（md + pdf）")
    ap.add_argument("--category", default=DEFAULT_CATEGORY, help="类目 ID（默认 1100001）")
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="成品目录")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理几本（0 = 全部）")
    ap.add_argument("--list-only", action="store_true", help="只列出各书当前状态，不导出")
    ap.add_argument("--force", action="store_true", help="透传给导出器：忽略 export_summary，强制重下")
    ap.add_argument("--no-check", action="store_true", help="透传给导出器：跳过导出后的完整性校验")
    ap.add_argument("--load-interval", type=int, default=20, help="章间平均间隔秒数（默认 20）")
    ap.add_argument("--load-timeout", type=int, default=0, help="单章超时秒数（0 = 导出器默认）")
    ap.add_argument("--book-timeout", type=int, default=4 * 3600, help="单本总超时秒数（默认 4 小时）")
    ap.add_argument("--retries", type=int, default=1, help="单本失败后的重试次数（默认 1）")
    ap.add_argument("--sleep-between-books", type=int, default=15, help="两本之间的停顿秒数（默认 15）")
    ap.add_argument("--max-consecutive-failures", type=int, default=3, help="连续失败多少本后停手（默认 3）")
    args = ap.parse_args(argv)

    output_dir = os.path.abspath(args.output_dir)
    log = make_logger(os.path.join(REPO, "logs", "batch-category-%s.run.log" % args.category))
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    books = asyncio.run(fetch_category_books(args.category))
    if args.limit:
        books = books[:args.limit]
    log("类目 %s → %s，共 %d 本" % (args.category, output_dir, len(books)))
    if not books:
        return 1

    counts = {"ok": 0, "incomplete": 0, "skipped": 0, "failed": 0}
    failures_in_a_row = 0
    for index, book in enumerate(books, 1):
        book_id, prefix = book["bookId"], "[%d/%d]" % (index, len(books))
        summary, _ = find_book_summary(output_dir, book_id, utils)
        plan, reason = plan_book(summary, FORMATS)
        if args.list_only:
            log("%s %-8s %s  %s（%s）" % (prefix, plan, book_id, book["title"], reason))
            continue
        if plan == "skip" and not args.force:
            counts["skipped"] += 1
            continue
        if failures_in_a_row >= args.max_consecutive_failures:
            log("! 已连续失败 %d 本，停手。排查后用同一命令续跑即可。" % failures_in_a_row)
            break

        log("%s 开始 %s（%s）：%s" % (prefix, book["title"], book_id, reason))
        started = time.time()
        code = run_export(book_id, output_dir, args)
        for attempt in range(args.retries):
            if code in (0, 2):
                break
            log("    退出码 %s，第 %d 次重试…" % (code, attempt + 1))
            time.sleep(10)
            code = run_export(book_id, output_dir, args)
        if code == 2:
            log("! Cookie 失效或需要登录，整批停止。更新 cache/cookie.txt 后用同一命令续跑。")
            return 2

        summary, _ = find_book_summary(output_dir, book_id, utils)
        if code == 0 and summary and summary.get("complete"):
            status = "ok"
        elif code == 0 and summary:
            status = "incomplete"
        else:
            status = "failed"
        counts[status] += 1
        failures_in_a_row = 0 if status != "failed" else failures_in_a_row + 1
        log("%s 结束 %s：%s（退出码 %s，用时 %ds）" % (
            prefix, book["title"], (summary or {}).get("status") or status, code, time.time() - started))
        time.sleep(args.sleep_between_books)

    if args.list_only:
        return 0
    log("本次结束：完整 %(ok)d，不完整 %(incomplete)d，已有跳过 %(skipped)d，失败 %(failed)d" % counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
