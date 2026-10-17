# -*- coding: utf-8 -*-
"""Completeness gate for an exported book.

Used after each book finishes exporting, and as a CLI:

    python -m weread_exporter.check_book <encodeId>

Exit 0 when the book is complete, exit 1 when it is not.

Why this exists: the exporter detects short chapters itself, but a batch driver
that only counts files will happily call a book finished while individual
chapters carry a fraction of their text. This gate is what makes a "done"
claim mean something.

How it decides, in order:

1. Every chapter file must exist and none may be a load-failure placeholder.
2. The exporter's own verdict (`cache/<id>/export_summary.json`) is honoured when
   present. Only real gaps (`unresolved` / `skipped`) fail the gate;
   `declared_count_diff` only warns.
3. Without a summary, fall back to the ratio scan: below hard_ratio fails,
   between hard_ratio and warn_ratio only warns.
4. Optionally (CLI only) require output/ md bundle or PDF — the exporter itself
   only checks cache/.
"""
from __future__ import print_function

import argparse
import glob
import io
import json
import logging
import os
import re
import sys

# Keep in step with WeReadExporter.short_chapter_* in export.py.
DEFAULT_MIN_WORDS = 200
DEFAULT_MIN_SHORTFALL = 200
DEFAULT_MIN_SHORTFALL_RATIO = 0.10
DEFAULT_HARD_RATIO = 0.90
DEFAULT_WARN_RATIO = 0.95


def strip_count(text):
    """Count visible characters, dropping markdown scaffolding and whitespace."""
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.M)
    return len(re.sub(r"\s+", "", text))


def load_summary(cache_dir):
    """Load cache/<id>/export_summary.json when present."""
    path = os.path.join(cache_dir, "export_summary.json")
    if not os.path.isfile(path):
        return None
    try:
        return json.load(io.open(path, encoding="utf-8"))
    except Exception:
        return None


def check(book_id, hard_ratio=DEFAULT_HARD_RATIO, warn_ratio=DEFAULT_WARN_RATIO,
          min_words=DEFAULT_MIN_WORDS, min_shortfall=DEFAULT_MIN_SHORTFALL,
          min_shortfall_ratio=DEFAULT_MIN_SHORTFALL_RATIO, root=".",
          require_pdf=False, require_md=False, pdf_suffix=""):
    """Judge whether captured chapters under cache/<id>/ are complete enough.

    By default only cache/ is checked (chapter files + export_summary). Set
    require_pdf / require_md to also enforce files under output/ (manual CLI).
    pdf_suffix: e.g. "-compressed" when looking for a compressed PDF.
    """
    cache_dir = os.path.join(root, "cache", book_id)
    meta_path = os.path.join(cache_dir, "meta.json")
    if not os.path.isfile(meta_path):
        return {"state": "not-started", "reason": "no meta.json in cache"}

    meta = json.load(io.open(meta_path, encoding="utf-8"))
    # Match the exporter's on-disk title (colon/slash escaped for Windows paths).
    from . import utils

    title = utils.format_filename(meta["title"])
    problems = []
    warnings = []
    captured = 0

    for index, chapter in enumerate(meta["chapters"]):
        ch_title = chapter.get("title") or ""
        declared = chapter.get("words") or 0
        files = glob.glob(os.path.join(cache_dir, "chapters", "%d-*.md" % (index + 1)))
        if not files:
            problems.append("chapter %d %s: file missing" % (index + 1, ch_title))
            continue
        text = io.open(files[0], encoding="utf-8", errors="ignore").read()
        if "load failed" in text[:80]:
            problems.append(
                "chapter %d %s: load-failure placeholder" % (index + 1, ch_title)
            )
            continue
        captured += 1
        if declared < min_words or "版权" in ch_title:
            continue
        got = strip_count(text)
        ratio = got / declared
        shortfall_limit = max(min_shortfall, declared * min_shortfall_ratio)
        if ratio < hard_ratio and (declared - got) >= shortfall_limit:
            problems.append(
                "chapter %d %s: %d/%d chars (%.0f%%)"
                % (index + 1, ch_title, got, declared, ratio * 100)
            )
        elif ratio < warn_ratio:
            warnings.append(
                "chapter %d %s: %d/%d chars (%.0f%%)"
                % (index + 1, ch_title, got, declared, ratio * 100)
            )

    pdf = os.path.join(root, "output", "%s%s.pdf" % (title, pdf_suffix or ""))
    bundle = os.path.join(root, "output", title)
    if require_pdf and not os.path.isfile(pdf):
        problems.append("no PDF at output/%s%s.pdf" % (title, pdf_suffix or ""))
    if require_md and not os.path.isdir(bundle):
        problems.append("no md bundle at output/%s/" % title)

    summary = load_summary(cache_dir)
    if summary:
        for item in summary.get("unresolved", []):
            problems.append(
                "chapter %s %s: exporter gave up at %s/%s chars"
                % (
                    item.get("index"),
                    item.get("title"),
                    item.get("captured"),
                    item.get("declared"),
                )
            )
        for item in summary.get("skipped", []):
            problems.append(
                "chapter %s: skipped after %s restarts (%s)"
                % (item.get("title"), item.get("restarts"), item.get("reason"))
            )
        for item in summary.get("declared_count_diff", []):
            warnings.append(
                "chapter %s %s: %s/%s chars — WeRead declares more than it renders"
                % (
                    item.get("index"),
                    item.get("title"),
                    item.get("captured"),
                    item.get("declared"),
                )
            )

    return {
        "state": "OK" if not problems else "INCOMPLETE",
        "title": title,
        "book_id": book_id,
        "chapters": len(meta["chapters"]),
        "captured": captured,
        "problems": problems,
        "warnings": warnings,
        "summary": bool(summary),
    }


def format_result_lines(result):
    """Return human-readable conclusion lines for a check() result."""
    if result["state"] == "not-started":
        return [
            "NOT-STARTED %s: %s"
            % (result.get("book_id") or "?", result.get("reason") or "unknown")
        ]
    lines = []
    if result["state"] == "OK":
        extra = ""
        if result["warnings"]:
            extra = " | %d declared-count warning(s)" % len(result["warnings"])
        lines.append(
            "OK %s | %d/%d chapters captured%s"
            % (result["title"], result["captured"], result["chapters"], extra)
        )
    else:
        lines.append(
            "INCOMPLETE %s | %d/%d chapters captured | %d problem(s)"
            % (
                result["title"],
                result["captured"],
                result["chapters"],
                len(result["problems"]),
            )
        )
    for p in result.get("problems") or []:
        lines.append("   ! %s" % p)
    for w in result.get("warnings") or []:
        lines.append("   ~ %s" % w)
    return lines


def log_result(result):
    """Write the completeness conclusion through the logging system."""
    lines = format_result_lines(result)
    if not lines:
        return
    header = "Completeness: %s" % lines[0]
    if result.get("state") == "OK":
        logging.info(header)
        for line in lines[1:]:
            logging.info(line)
    elif result.get("state") == "not-started":
        logging.warning(header)
    else:
        logging.error(header)
        for line in lines[1:]:
            if line.startswith("   !"):
                logging.error(line)
            else:
                logging.warning(line)


def _force_utf8_stdio():
    """Force stdout/stderr to UTF-8 so Chinese conclusions survive Windows pipes.

    Same reason as weread_exporter/__main__._force_utf8_stdio: the console code
    page is GBK on zh-CN, so `print()` of a Chinese title came out as mojibake
    whenever this module was run standalone
    (`python -m weread_exporter.check_book <id>`). The log path used by the
    exporter is unaffected — it already writes through a UTF-8 FileHandler.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None):
    """CLI entry: check one book and print the conclusion."""
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(prog="weread-exporter.check_book")
    ap.add_argument("book_id")
    ap.add_argument("--hard-ratio", type=float, default=DEFAULT_HARD_RATIO)
    ap.add_argument("--warn-ratio", type=float, default=DEFAULT_WARN_RATIO)
    ap.add_argument("--min-words", type=int, default=DEFAULT_MIN_WORDS)
    ap.add_argument("--min-shortfall", type=int, default=DEFAULT_MIN_SHORTFALL)
    ap.add_argument(
        "--min-shortfall-ratio",
        type=float,
        default=DEFAULT_MIN_SHORTFALL_RATIO,
    )
    ap.add_argument("--root", default=".")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--require-pdf",
        action="store_true",
        help="also require output/<title>.pdf (off by default; cache/ is enough)",
    )
    ap.add_argument(
        "--require-md",
        action="store_true",
        help="also require output/<title>/ md bundle (off by default)",
    )
    ap.add_argument(
        "--pdf-suffix",
        default="",
        help='suffix before .pdf, e.g. "-compressed"',
    )
    args = ap.parse_args(argv)

    r = check(
        args.book_id,
        args.hard_ratio,
        args.warn_ratio,
        args.min_words,
        args.min_shortfall,
        args.min_shortfall_ratio,
        args.root,
        require_pdf=args.require_pdf,
        require_md=args.require_md,
        pdf_suffix=args.pdf_suffix,
    )
    if r["state"] == "not-started":
        print("NOT-STARTED %s: %s" % (args.book_id, r["reason"]))
        return 1
    if r["state"] == "OK":
        if not args.quiet:
            for line in format_result_lines(r):
                print(line)
        return 0
    for line in format_result_lines(r):
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
