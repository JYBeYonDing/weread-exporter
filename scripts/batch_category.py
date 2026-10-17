#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sequentially export every book in a WeRead category.

One book at a time — never in parallel. Each book is exported with the same
`-o md -o pdf` invocation you would type by hand, into a chosen output directory
(default `Z:\\share\\books`), then the script waits for it to finish before
starting the next one. PDF images are left uncompressed (no
`--compress-pdf-images`).

Why a script instead of a shell loop: WeRead caps how deep the category listing
can be paged (`/web/bookListInCategory/<id>?maxIndex=N` returns an empty page
past a few hundred entries), book titles need `utils.format_filename` to match
what the exporter writes on disk, and a dead cookie must stop the whole run
instead of failing 500 books one by one.

Usage (from the repo root, with this clone's venv):

    .venv\\Scripts\\python.exe scripts/batch_category.py --category 1100001 \\
        --output-dir Z:\\share\\books

    # see what would be downloaded, touch nothing
    .venv\\Scripts\\python.exe scripts/batch_category.py --list-only

    # first 3 books only, to prove the pipeline end to end
    .venv\\Scripts\\python.exe scripts/batch_category.py --limit 3

Resumable: books already present in the output dir (md bundle + PDF) are
skipped, so re-running the same command continues where the last run stopped.
Progress goes to stdout and to logs/batch-category-<id>.run.log.

Exit codes: 0 finished (possibly with per-book failures recorded in the state
file), 2 stopped because the cookie expired.
"""
from __future__ import print_function

import argparse
import asyncio
import io
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from weread_exporter import utils  # noqa: E402

DEFAULT_CATEGORY = "1100001"
DEFAULT_OUTPUT_DIR = r"Z:\share\books"
PAGE_SIZE = 20
# The listing endpoint silently returns an empty page past this depth, so a
# safety ceiling keeps a changed API from turning into an endless loop.
MAX_PAGES = 400


def _force_utf8_stdio():
    """Keep Chinese titles readable when stdout is a GBK console or a pipe."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log_line(line):
    """Print and flush so a long run still shows progress when piped."""
    print(line, flush=True)


class RunLog(object):
    """stdout + UTF-8 file, so a multi-day run leaves a readable transcript."""

    def __init__(self, path):
        self.path = path
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        self.fp = io.open(path, "a", encoding="utf-8")

    def __call__(self, line):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_line(line)
        self.fp.write("[%s] %s\n" % (stamp, line))
        self.fp.flush()

    def close(self):
        self.fp.close()


def load_state(path):
    if not os.path.isfile(path):
        return {}
    try:
        with io.open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path, state):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fp:
        fp.write(json.dumps(state, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


async def fetch_category_books(category_id):
    """Page the category listing until it runs dry.

    Returns (books, total_count). `books` carries only what the batch needs.

    The listing's `bookId` is the numeric product id (e.g. 3300083256); the
    exporter and the cache are keyed by the **encoded** id from `readerLink`
    (e.g. 95332ad0813ab8705g016ce7). Both are kept: the encoded one is what
    `weread_exporter -b` and `cache/<id>/` expect.

    Duplicates are dropped — the endpoint can repeat an entry across page
    boundaries when its underlying ranking shifts.
    """
    books = []
    seen = set()
    total_count = None
    for page in range(MAX_PAGES):
        max_index = page * PAGE_SIZE
        url = "https://weread.qq.com/web/bookListInCategory/%s?maxIndex=%d" % (
            category_id,
            max_index,
        )
        raw = await utils.fetch(url)
        payload = json.loads(raw.decode("utf-8"))
        if total_count is None:
            total_count = payload.get("totalCount")
        entries = payload.get("books") or []
        if not entries:
            break
        for entry in entries:
            info = entry.get("bookInfo") or {}
            encode_id = _encode_id(info)
            if not encode_id or encode_id in seen:
                continue
            seen.add(encode_id)
            books.append(
                {
                    "bookId": encode_id,
                    "numericId": info.get("bookId"),
                    "title": info.get("title") or encode_id,
                    "author": info.get("author") or "",
                    "payType": info.get("payType"),
                    "soldout": info.get("soldout"),
                }
            )
        if not payload.get("hasMore"):
            break
        await asyncio.sleep(0.3)
    return books, total_count


def _encode_id(book_info):
    """Encoded book id (the one `-b` takes) from readerLink, or deepLink's `v=`."""
    reader_link = book_info.get("readerLink") or ""
    if reader_link:
        tail = reader_link.rstrip("/").split("/")[-1]
        if tail and "?" not in tail:
            return tail
    deep_link = book_info.get("deepLink") or ""
    pos = deep_link.find("v=")
    if pos >= 0:
        return deep_link[pos + 2 :].split("&")[0]
    return ""


def resolve_title(book, cache_root):
    """Title as the exporter will write it on disk.

    The reader page is authoritative (`cache/<id>/meta.json` once the book has
    been fetched at least once); the listing title is the fallback. Both are run
    through the same escaping the exporter uses for Windows paths.
    """
    meta_path = os.path.join(cache_root, book["bookId"], "meta.json")
    title = book["title"]
    if os.path.isfile(meta_path):
        try:
            with io.open(meta_path, encoding="utf-8") as fp:
                cached = json.load(fp).get("title")
            if cached:
                title = cached
        except Exception:
            pass
    return utils.format_filename(title)


def already_exported(output_dir, title, want_md=True, want_pdf=True):
    if want_md and not os.path.isdir(os.path.join(output_dir, title)):
        return False
    if want_pdf and not os.path.isfile(os.path.join(output_dir, "%s.pdf" % title)):
        return False
    return True


def read_summary(cache_root, book_id):
    """The exporter's own verdict for this book, when it wrote one."""
    path = os.path.join(cache_root, book_id, "export_summary.json")
    if not os.path.isfile(path):
        return None
    try:
        with io.open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return None


def verdict_text(summary):
    if not summary:
        return ""
    unresolved = summary.get("unresolved") or []
    skipped = summary.get("skipped") or []
    if skipped:
        return "INCOMPLETE（%d 章跳过）" % len(skipped)
    if unresolved:
        return "INCOMPLETE（%d 章有缺口）" % len(unresolved)
    return "OK"


def run_export(book, output_dir, args, log):
    """Run one book through the exporter. Returns (exit_code, seconds)."""
    cmd = [
        sys.executable,
        "-m",
        "weread_exporter",
        "-b",
        book["bookId"],
        "-o",
        "md",
        "-o",
        "pdf",
        "--output-dir",
        output_dir,
        "--load-interval",
        str(args.load_interval),
    ]
    if args.load_timeout:
        cmd += ["--load-timeout", str(args.load_timeout)]
    if args.no_check:
        cmd += ["--no-check"]
    started = time.time()
    log("    命令: %s" % " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=args.book_timeout,
        )
        code = proc.returncode
    except subprocess.TimeoutExpired:
        log("    ! 单本超过 %ds 未完成，已放弃（导出进程已被终止）" % args.book_timeout)
        code = -9
    return code, time.time() - started


def format_duration(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh%02dm" % (hours, minutes)
    if minutes:
        return "%dm%02ds" % (minutes, secs)
    return "%ds" % secs


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="batch_category",
        description="一本一本导出微信读书某个类目下的所有书籍（md + pdf）",
    )
    ap.add_argument("--category", default=DEFAULT_CATEGORY, help="类目 ID（默认 1100001）")
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="成品目录")
    ap.add_argument("--limit", type=int, default=0, help="本次最多导几本（0 = 全部）")
    ap.add_argument("--skip-first", type=int, default=0, help="跳过清单里的前 N 本（续跑用）")
    ap.add_argument(
        "--load-interval",
        type=int,
        default=20,
        help="章间平均间隔秒数，透传给导出器（默认 20）",
    )
    ap.add_argument("--load-timeout", type=int, default=0, help="单章超时秒数（0 = 用导出器默认）")
    ap.add_argument(
        "--book-timeout",
        type=int,
        default=4 * 3600,
        help="单本总超时秒数（默认 4 小时）",
    )
    ap.add_argument(
        "--retries",
        type=int,
        default=1,
        help="单本失败后的重试次数（默认 1；重跑会从已抓章节续抓）",
    )
    ap.add_argument(
        "--sleep-between-books",
        type=int,
        default=15,
        help="两本之间的停顿秒数（默认 15）",
    )
    ap.add_argument("--force", action="store_true", help="忽略已导出判断，重新导出")
    ap.add_argument("--list-only", action="store_true", help="只列清单，不导出")
    ap.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=3,
        help="连续失败多少本后停手（默认 3，防止整批空转）",
    )
    ap.add_argument(
        "--state-file",
        default="",
        help="进度文件（默认 cache/batch-category-<category>.json）",
    )
    ap.add_argument("--no-check", action="store_true", help="跳过导出后的完整性校验")
    args = ap.parse_args(argv)
    _force_utf8_stdio()

    output_dir = os.path.abspath(args.output_dir)
    cache_root = os.path.join(REPO, "cache")
    state_path = args.state_file or os.path.join(
        cache_root, "batch-category-%s.json" % args.category
    )
    log = RunLog(
        os.path.join(REPO, "logs", "batch-category-%s.run.log" % args.category)
    )

    log("=" * 72)
    log("类目 %s → %s" % (args.category, output_dir))
    if not os.path.isdir(output_dir):
        log("! 输出目录不存在，尝试创建")
        try:
            os.makedirs(output_dir)
        except Exception as ex:
            log("! 无法创建输出目录：%s" % ex)
            return 1

    log("拉取类目书单…")
    books, total_count = asyncio.run(fetch_category_books(args.category))
    log(
        "清单共 %d 本（接口声明 totalCount=%s；微信读书的类目列表只开放到有限深度）"
        % (len(books), total_count)
    )
    if not books:
        log("! 清单为空，检查类目 ID 或网络")
        return 1

    # Persist the listing so a later run (or a human) can see what was covered.
    manifest_path = os.path.join(cache_root, "category-%s.books.json" % args.category)
    save_state(manifest_path, {"categoryId": args.category, "books": books})
    log("清单已存 %s" % manifest_path)

    pending = books[args.skip_first:]
    if args.limit:
        pending = pending[: args.limit]

    todo = []
    skipped_done = 0
    for book in pending:
        title = resolve_title(book, cache_root)
        book["_title"] = title
        if not args.force and already_exported(output_dir, title):
            skipped_done += 1
            continue
        todo.append(book)

    log(
        "本次待导 %d 本（已存在跳过 %d 本）"
        % (len(todo), skipped_done)
    )
    if args.list_only:
        todo_ids = set(book["bookId"] for book in todo)
        for index, book in enumerate(pending, 1):
            mark = "待导出" if book["bookId"] in todo_ids else "已存在"
            log("  %3d. [%s] %s  %s" % (index, mark, book["bookId"], book["_title"]))
        log.close()
        return 0

    state = load_state(state_path)
    state.setdefault("category", args.category)
    state.setdefault("output_dir", output_dir)
    state.setdefault("books", {})

    started_all = time.time()
    done = failed = 0
    consecutive_failures = 0
    interrupted_cookie = False
    durations = []

    for index, book in enumerate(todo, 1):
        title = book["_title"]
        prefix = "[%d/%d]" % (index, len(todo))

        if consecutive_failures >= args.max_consecutive_failures:
            log(
                "! 已连续失败 %d 本，停手。排查后用同一命令续跑即可。"
                % consecutive_failures
            )
            break

        log("")
        log("%s 开始 %s（%s）" % (prefix, title, book["bookId"]))
        attempt = 0
        code = None
        seconds = 0.0
        while attempt <= args.retries:
            if attempt:
                log("    第 %d 次重试…" % attempt)
                time.sleep(10)
            code, seconds = run_export(book, output_dir, args, log)
            if code == 0:
                break
            if code == 2:
                # Cookie dead: every following book would fail the same way.
                interrupted_cookie = True
                break
            attempt += 1

        record = {
            "bookId": book["bookId"],
            "title": title,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": int(seconds),
            "exit_code": code,
        }

        if interrupted_cookie:
            record["status"] = "cookie-expired"
            state["books"][book["bookId"]] = record
            save_state(state_path, state)
            log("")
            log(
                "! Cookie 失效或需要登录，整批停止。更新 cache/cookie.txt "
                "（或加 --force-login 单跑一本）后用同一命令续跑。"
            )
            break

        exported = already_exported(output_dir, title)
        summary = read_summary(cache_root, book["bookId"])
        record["verdict"] = verdict_text(summary)

        if code == 0 and exported:
            record["status"] = "ok"
            done += 1
            consecutive_failures = 0
            durations.append(seconds)
            avg = sum(durations) / len(durations)
            remaining = len(todo) - index
            eta = format_duration(avg * remaining) if remaining else "0s"
            log(
                "%s 完成 %s（用时 %s，%s）— 已成功 %d 本，预计剩余 %s"
                % (prefix, title, format_duration(seconds), record["verdict"], done, eta)
            )
        elif code == 0 and not exported:
            # The exporter returns 0 for a sold-out / unreadable book and simply
            # writes nothing. Not a crash, but nothing was delivered either.
            record["status"] = "nothing-exported"
            consecutive_failures += 1
            log("%s 未产出任何文件（可能已下架或无权限），跳过" % prefix)
        else:
            record["status"] = "failed"
            failed += 1
            consecutive_failures += 1
            log("%s 失败（退出码 %s，用时 %s）" % (prefix, code, format_duration(seconds)))

        state["books"][book["bookId"]] = record
        save_state(state_path, state)

        if index < len(todo):
            time.sleep(args.sleep_between_books)

    elapsed = time.time() - started_all
    log("")
    log("=" * 72)
    log(
        "本次结束：成功 %d，失败 %d，用时 %s。进度文件 %s"
        % (done, failed, format_duration(elapsed), state_path)
    )
    if interrupted_cookie:
        log("停止原因：cookie 失效")
    log.close()
    return 2 if interrupted_cookie else 0


if __name__ == "__main__":
    sys.exit(main())
