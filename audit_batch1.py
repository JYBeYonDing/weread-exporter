#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次完整性审计：生成逐本明细表。

默认审第一批 12 本；传 --batch 2 审第二批（13~25 共 13 本），--batch all 审全部。

对每本核对：
  - 章节数：缓存里的 .md 数 vs meta.json 声明数
  - 正文总量：逐章实测字数（去空白/去 markdown 标记）之和 vs 声明字数之和
  - 逐章判定：每一章是否达到声明字数的门槛（复用 check_book 的规则）
  - 图片：md 里引用的图片数 vs 交付包 images/ 里的文件数
  - 产物：md 包（chapters/images/cover.jpg）与 PDF 的页数、图片对象数、大小
  - 结论：OK / 有缺口（列出具体章节）

用法：python audit_batch1.py [--markdown 输出路径]
"""
import argparse
import glob
import io
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.abspath(__file__))

# 序号 | encodeId | 声明章节数 | 书名
BATCH = [
    ("1", "742321a0813ab78f8g014429", 55, "漫画销售话术：轻松搞定客户的说话技巧"),
    ("2", "9ac322d0813ab6b15g016593", 81, "优势谈判"),
    ("3", "31032d90813ab7243g0107a3", 37, "四步说服法：高效演讲、谈判、销售、沟通的秘诀"),
    ("4", "c6432b20813abb7c7g0178be", 33, "7天成为销售高手"),
    ("5", "840325405b170c8404e1e70", 33, "每天学一点销售技巧"),
    ("6", "e5432960717eb613e548cf6", 62, "销售是个专业活：B2B 工业品"),
    ("7", "9bb321505b16bc9bbb94ee7", 9, "销售读心术"),
    ("8", "5fa32fb0813ab7604g010cde", 15, "赢单九问：策略销售实战秘籍"),
    ("9", "ff0320007159cb09ff012b4", 58, "销售就是提问题"),
    ("10", "7b932060813aba162g018f20", 21, "差异化销售"),
    ("11", "2ab323c07192ea7e2ab1a4a", 31, "成交高于一切：大客户销售十八招（全新修订版3.0）"),
    ("12", "7d032810813ab7817g012000", 45, "大客户销售：谋攻之道（全新版）"),
]

# 第二批：13~25
BATCH2 = [
    ("13", "9c0327605c85e39c067a0a0", 22, "当客户说“不”"),
    ("14", "c3d3244071e5d40ac3df945", 60, "销售圣经（套装共2册）"),
    ("15", "a4b32f20813abb0dbg012751", 28, "情绪价值型销售高手"),
    ("16", "9f532d50813abc119g017d94", 59, "销售洗脑（全能销售系列）"),
    ("17", "9c832d005c85699c860113f", 10, "失传的营销秘诀"),
    ("18", "6da323305d18b86da0a7795", 11, "一本书读懂客户心理"),
    ("19", "d1f321607210fc63d1f7277", 59, "销量公式"),
    ("20", "e8432e60813ab8b4cg013133", 24, "大客户销售心法"),
    ("21", "a0832ec0813abbe0dg019bd6", 48, "如何让用户成为你的死忠粉：雷军小米营销法"),
    ("22", "6ef32fb071c307306ef1a59", 44, "营销心理战：掌握36种购买心理"),
    ("23", "41b329805df7df41ba680f7", 60, "销售就是要玩转情商"),
    ("24", "3b632790813ab8f85g0145b7", 38, "华为营销法"),
    ("25", "bf032c7072103ce5bf0568a", 109, "哈佛经典谈判术：你一开口就赢麻了"),
]

# 与 check_book.py / export.py 保持一致
MIN_WORDS = 200
MIN_SHORTFALL = 200
MIN_SHORTFALL_RATIO = 0.10
# 上限：实测远超声明字数说明内容被重复写入（同一章抓了两遍拼在一起）。
# 健康的章节实测/声明落在 95%~103%，重复会直奔 200%~300%。
OVER_RATIO = 1.10
OVER_MIN_EXCESS = 500


def strip_count(text):
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.M)
    return len(re.sub(r"\s+", "", text))


def audit_one(idx, bid, want_chapters, label):
    cache = os.path.join(REPO, "cache", bid)
    meta_p = os.path.join(cache, "meta.json")
    row = {"idx": idx, "bid": bid, "label": label, "want_chapters": want_chapters}

    if not os.path.isfile(meta_p):
        row.update(state="未开始", title=None)
        return row

    meta = json.load(io.open(meta_p, encoding="utf-8"))
    title = meta["title"]
    row["title"] = title
    declared_words = sum((c.get("words") or 0) for c in meta["chapters"])
    row["declared_words"] = declared_words

    got_chapters = captured_words = 0
    img_refs = 0
    img_names = set()
    problems = []
    for i, ch in enumerate(meta["chapters"]):
        files = glob.glob(os.path.join(cache, "chapters", "%d-*.md" % (i + 1)))
        if not files:
            problems.append("第%d章 %s：无文件" % (i + 1, (ch.get("title") or "")[:24]))
            continue
        text = io.open(files[0], encoding="utf-8", errors="ignore").read()
        if "load failed" in text[:80]:
            problems.append("第%d章 %s：加载失败占位符" % (i + 1, (ch.get("title") or "")[:24]))
            continue
        got_chapters += 1
        captured_words += strip_count(text)
        names = re.findall(r"images/([A-Za-z0-9]+\.(?:jpg|jpeg|png|gif))", text)
        img_refs += len(names)
        img_names.update(names)

        dec = ch.get("words") or 0
        ct = ch.get("title") or ""
        if dec < MIN_WORDS or "版权" in ct:
            continue
        got = strip_count(text)
        ratio = got / dec
        limit = max(MIN_SHORTFALL, dec * MIN_SHORTFALL_RATIO)
        if ratio < 0.90 and (dec - got) >= limit:
            problems.append(
                "第%d章 %s：%d/%d 字（%.0f%%）" % (i + 1, ct[:24], got, dec, ratio * 100)
            )
        elif ratio > OVER_RATIO and (got - dec) >= OVER_MIN_EXCESS:
            problems.append(
                "第%d章 %s：%d/%d 字（%.0f%%，内容疑似重复）"
                % (i + 1, ct[:24], got, dec, ratio * 100)
            )

    row.update(got_chapters=got_chapters, captured_words=captured_words,
               img_refs=img_refs, img_names=len(img_names), problems=problems)

    # 交付包
    bundle = os.path.join(REPO, "output", title)
    row["md_bundle"] = os.path.isdir(bundle)
    if row["md_bundle"]:
        row["md_chapters"] = len(glob.glob(os.path.join(bundle, "chapters", "*.md")))
        row["bundle_images"] = len(glob.glob(os.path.join(bundle, "images", "*")))
        row["has_cover"] = os.path.isfile(os.path.join(bundle, "cover.jpg"))
        row["bundle_junk"] = [n for n in os.listdir(bundle)
                              if n in ("meta.json",) or n.endswith(".bak")]
    else:
        row["md_chapters"] = row["bundle_images"] = 0
        row["has_cover"] = False
        row["bundle_junk"] = []

    # PDF
    pdf = os.path.join(REPO, "output", title + ".pdf")
    row["pdf"] = os.path.isfile(pdf)
    if row["pdf"]:
        row["pdf_size"] = os.path.getsize(pdf)
        try:
            import fitz
            d = fitz.open(pdf)
            row["pdf_pages"] = d.page_count
            row["pdf_images"] = sum(len(p.get_images(full=True)) for p in d)
            row["pdf_pages_with_images"] = sum(1 for p in d if p.get_images())
            d.close()
        except Exception as e:
            row["pdf_pages"] = None
            row["pdf_images"] = None
            row["problems"] = problems + ["PDF 打不开：%s" % repr(e)[:60]]
    else:
        row["pdf_size"] = row["pdf_pages"] = row["pdf_images"] = None
        row["pdf_pages_with_images"] = None

    # 摘要（若可读）
    row["summary_readable"] = None
    sp = os.path.join(cache, "export_summary.json")
    if os.path.isfile(sp):
        try:
            json.loads(open(sp, "rb").read().decode("utf-8"))
            row["summary_readable"] = True
        except Exception:
            row["summary_readable"] = False

    # 结论
    ok = (got_chapters >= want_chapters and not problems and row["md_bundle"]
          and row["pdf"] and row["has_cover"] and not row["bundle_junk"])
    # 交付包里的图片必须覆盖所有被引用的图片（按去重后的文件名比，同一张图在
    # 多章被引用是正常的，不是缺失）
    if row["md_bundle"] and row["bundle_images"] < row["img_names"]:
        problems.append("交付包缺图：引用去重 %d 张，包内 %d 张"
                        % (row["img_names"], row["bundle_images"]))
        ok = False
    row["problems"] = problems
    if got_chapters < want_chapters:
        ok = False
    row["state"] = "完成" if ok else "未完成"

    # 每章比例的分位，便于看整体健康度
    ratios = []
    for i, ch in enumerate(meta["chapters"]):
        files = glob.glob(os.path.join(cache, "chapters", "%d-*.md" % (i + 1)))
        if not files:
            continue
        dec = ch.get("words") or 0
        if dec < MIN_WORDS or "版权" in (ch.get("title") or ""):
            continue
        text = io.open(files[0], encoding="utf-8", errors="ignore").read()
        ratios.append(strip_count(text) / dec)
    row["ratio_min"] = min(ratios) if ratios else None
    row["ratio_avg"] = sum(ratios) / len(ratios) if ratios else None
    return row


def pick(which):
    if which == "2":
        return BATCH2
    if which == "all":
        return BATCH + BATCH2
    return BATCH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown")
    ap.add_argument("--batch", default="1", choices=["1", "2", "all"])
    args = ap.parse_args()

    rows = [audit_one(*b) for b in pick(args.batch)]

    # 控制台明细
    for r in rows:
        if r["state"] == "未开始":
            print("%-3s %-34s 未开始" % (r["idx"], r["label"][:32]))
            continue
        print("%-3s %-34s %s章 %6d/%6d字 图%4d PDF%s页 %s" % (
            r["idx"], r["title"][:32],
            "%d/%d" % (r.get("got_chapters", 0), r["want_chapters"]),
            r.get("captured_words", 0), r.get("declared_words", 0),
            r.get("img_refs", 0),
            r.get("pdf_pages") or "--", r["state"]))
        for p in r.get("problems", []):
            print("        └ %s" % p)

    # 汇总
    done = [r for r in rows if r["state"] == "完成"]
    print()
    print("完成 %d/%d 本" % (len(done), len(rows)))
    print("章节合计 %d/%d" % (sum(r.get("got_chapters", 0) for r in rows),
                              sum(r["want_chapters"] for r in rows)))
    print("正文合计 %d / 声明 %d 字" % (sum(r.get("captured_words", 0) for r in rows),
                                        sum(r.get("declared_words", 0) for r in rows)))

    if args.markdown:
        lines = ["| # | 书名 | 章节 | 正文字数（实测/声明） | 图片 | md 包 | PDF | 判定 |",
                 "|---|---|---|---|---|---|---|---|"]
        for r in rows:
            if r["state"] == "未开始":
                lines.append("| %s | %s | — | — | — | — | — | 未开始 |"
                             % (r["idx"], r["label"]))
                continue
            md = "✓ %d章/%d图%s" % (r.get("md_chapters", 0), r.get("bundle_images", 0),
                                    "/封面✓" if r.get("has_cover") else "")
            pdf = ("✓ %s页 %.1fMB" % (r.get("pdf_pages"), (r.get("pdf_size") or 0) / 1048576)
                   if r.get("pdf") else "✗")
            verdict = "✅" if r["state"] == "完成" else "⚠️ " + "；".join(r.get("problems", []))
            lines.append("| %s | %s | %d/%d | %d / %d | %d | %s | %s | %s |" % (
                r["idx"], r["title"], r.get("got_chapters", 0), r["want_chapters"],
                r.get("captured_words", 0), r.get("declared_words", 0),
                r.get("img_refs", 0), md, pdf, verdict))
        io.open(args.markdown, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        print("\nmarkdown 已写入 %s" % args.markdown)

    return 0 if len(done) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
