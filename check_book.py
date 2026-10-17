#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Completeness gate for an exported book.

Usage:
    python check_book.py <encodeId> [--hard-ratio 0.90] [--warn-ratio 0.97]

Exit 0 when the book is complete, exit 1 when it is not (and print what is
missing).

Why this exists: the exporter detects short chapters itself, but a batch driver
that only counts files will happily call a book finished while individual
chapters carry a fraction of their text. 《销售读心术》 was reported as 9/9
chapters done while chapters 3/6/7 held 14%/26%/2% of their content — the
missing text was invisible until the PDF was read. This gate is what makes a
"done" claim mean something.

How it decides, in order:

1. Every chapter file must exist and none may be a load-failure placeholder.
2. The md bundle and the PDF must both exist.
3. The exporter's own verdict (`cache/<id>/export_summary.json`) is honoured when
   present. It separates the two kinds of short chapter, which a bare ratio
   cannot: a chapter whose text *changed* across re-fetches is a real gap
   (`unresolved`), while one that never changed is just WeRead declaring more
   words than it renders (`declared_count_diff`) — measured on healthy chapters
   at 94%-97%, versus 2%-26% for the genuine gaps. Only the former fails the gate.
4. Without a summary, fall back to the ratio scan: below --hard-ratio fails,
   between --hard-ratio and --warn-ratio only warns.
"""
import argparse
import glob
import io
import json
import os
import re
import sys

# Chapters smaller than this are unreliable to judge (front matter, part
# dividers) — the same rule the exporter applies.
DEFAULT_MIN_WORDS = 200
# A chapter is only a real gap when it is *both* proportionally short and missing a
# lot, and the second half scales with the chapter. Over 682 chapters judged
# complete, the worst shortfall was 2 characters on a 2k chapter but 703 on a 17k
# one (up to 4.9% of the declared length); every real truncation lost 39%-98%.
# Keep these in step with WeReadExporter.short_chapter_min_shortfall /
# short_chapter_shortfall_ratio.
DEFAULT_MIN_SHORTFALL = 200
DEFAULT_MIN_SHORTFALL_RATIO = 0.10


def strip_count(text):
    """Count visible characters, dropping markdown scaffolding and whitespace."""
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.M)
    return len(re.sub(r"\s+", "", text))


def load_summary(cache_dir):
    path = os.path.join(cache_dir, "export_summary.json")
    if not os.path.isfile(path):
        return None
    try:
        return json.load(io.open(path, encoding="utf-8"))
    except Exception:
        return None


def check(book_id, hard_ratio=0.90, warn_ratio=0.97, min_words=DEFAULT_MIN_WORDS,
          min_shortfall=DEFAULT_MIN_SHORTFALL,
          min_shortfall_ratio=DEFAULT_MIN_SHORTFALL_RATIO, root="."):
    cache_dir = os.path.join(root, "cache", book_id)
    meta_path = os.path.join(cache_dir, "meta.json")
    if not os.path.isfile(meta_path):
        return {"state": "not-started", "reason": "no meta.json in cache"}

    meta = json.load(io.open(meta_path, encoding="utf-8"))
    title = meta["title"]
    problems = []   # hard failures
    warnings = []   # worth printing, not worth failing on
    captured = 0

    for index, chapter in enumerate(meta["chapters"]):
        ch_title = chapter.get("title") or ""
        declared = chapter.get("words") or 0
        files = glob.glob(os.path.join(cache_dir, "chapters", "%d-*.md" % (index + 1)))
        if not files:
            problems.append(("chapter %d %s: file missing" % (index + 1, ch_title)))
            continue
        text = io.open(files[0], encoding="utf-8", errors="ignore").read()
        if "load failed" in text[:80]:
            problems.append(("chapter %d %s: load-failure placeholder" % (index + 1, ch_title)))
            continue
        captured += 1
        if declared < min_words or "版权" in ch_title:
            continue
        got = strip_count(text)
        ratio = got / declared
        # A chapter can be a few percent short of the declared count and still be
        # complete — WeRead's declared counts run high on intact chapters, worst
        # seen 4.9% (100 chars on a 2053-char chapter, 703 on a 17103-char one).
        # What separates that from a real gap is the size of the shortfall, which
        # grows with the chapter: real truncations lost 39%-98%, i.e. thousands of
        # characters. Requiring both a low ratio and a large shortfall keeps the
        # gate from failing a whole book over a rounding difference.
        shortfall_limit = max(min_shortfall, declared * min_shortfall_ratio)
        if ratio < hard_ratio and (declared - got) >= shortfall_limit:
            problems.append(
                "chapter %d %s: %d/%d chars (%.0f%%)" % (index + 1, ch_title, got, declared, ratio * 100)
            )
        elif ratio < warn_ratio:
            warnings.append(
                "chapter %d %s: %d/%d chars (%.0f%%)" % (index + 1, ch_title, got, declared, ratio * 100)
            )

    pdf = os.path.join(root, "output", title + ".pdf")
    bundle = os.path.join(root, "output", title)
    if not os.path.isfile(pdf):
        problems.append("no PDF at output/%s.pdf" % title)
    if not os.path.isdir(bundle):
        problems.append("no md bundle at output/%s/" % title)

    summary = load_summary(cache_dir)
    if summary:
        for item in summary.get("unresolved", []):
            problems.append(
                "chapter %s %s: exporter gave up at %s/%s chars"
                % (item.get("index"), item.get("title"), item.get("captured"), item.get("declared"))
            )
        for item in summary.get("skipped", []):
            problems.append(
                "chapter %s: skipped after %s restarts (%s)"
                % (item.get("title"), item.get("restarts"), item.get("reason"))
            )
        for item in summary.get("declared_count_diff", []):
            warnings.append(
                "chapter %s %s: %s/%s chars — WeRead declares more than it renders"
                % (item.get("index"), item.get("title"), item.get("captured"), item.get("declared"))
            )

    return {
        "state": "OK" if not problems else "INCOMPLETE",
        "title": title,
        "chapters": len(meta["chapters"]),
        "captured": captured,
        "problems": problems,
        "warnings": warnings,
        "summary": bool(summary),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("book_id")
    ap.add_argument("--hard-ratio", type=float, default=0.90)
    ap.add_argument("--warn-ratio", type=float, default=0.97)
    ap.add_argument("--min-words", type=int, default=DEFAULT_MIN_WORDS)
    ap.add_argument("--min-shortfall", type=int, default=DEFAULT_MIN_SHORTFALL)
    ap.add_argument("--min-shortfall-ratio", type=float,
                    default=DEFAULT_MIN_SHORTFALL_RATIO)
    ap.add_argument("--root", default=".")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    r = check(args.book_id, args.hard_ratio, args.warn_ratio, args.min_words,
              args.min_shortfall, args.min_shortfall_ratio, args.root)
    if r["state"] == "not-started":
        print("NOT-STARTED %s: %s" % (args.book_id, r["reason"]))
        return 1

    if r["state"] == "OK":
        if not args.quiet:
            print("OK %s | %d/%d chapters captured%s"
                  % (r["title"], r["captured"], r["chapters"],
                     "" if not r["warnings"] else " | %d declared-count warning(s)" % len(r["warnings"])))
            for w in r["warnings"]:
                print("   ~ %s" % w)
        return 0

    print("INCOMPLETE %s | %d/%d chapters captured | %d problem(s)"
          % (r["title"], r["captured"], r["chapters"], len(r["problems"])))
    for p in r["problems"]:
        print("   ! %s" % p)
    for w in r["warnings"]:
        print("   ~ %s" % w)
    return 1


if __name__ == "__main__":
    sys.exit(main())
