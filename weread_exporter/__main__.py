import argparse
import asyncio
import json
import logging
import os
import sys

# Suffix for the compressed PDF, so it never overwrites the full-quality one.
COMPRESS_PDF_SUFFIX = "-compressed"
# Defaults tuned on a 368-image comic book: ~90 MB of pages -> under 20 MB.
DEFAULT_PDF_IMAGE_MAX_SIDE = 1000
DEFAULT_PDF_IMAGE_COLORS = 256


def _project_root():
    """Return this clone's repository root (parent of the weread_exporter package)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_project_venv():
    """Refuse to run unless the active interpreter is this clone's .venv.

    Each clone must keep its own virtualenv so a global/other-clone install cannot
    silently take over. Bootstrap with scripts/bootstrap.ps1 (or .sh), then:
      .venv\\Scripts\\python.exe -m weread_exporter ...
    """
    root = _project_root()
    expected = os.path.abspath(os.path.join(root, ".venv"))
    prefix = os.path.abspath(sys.prefix)
    executable = os.path.abspath(sys.executable)

    if sys.platform == "win32":
        expected_cmp = os.path.normcase(expected)
        prefix_cmp = os.path.normcase(prefix)
        executable_cmp = os.path.normcase(executable)
        sep = "\\"
        bootstrap = os.path.join(root, "scripts", "bootstrap.ps1")
        venv_python = os.path.join(expected, "Scripts", "python.exe")
    else:
        expected_cmp = expected
        prefix_cmp = prefix
        executable_cmp = executable
        sep = "/"
        bootstrap = os.path.join(root, "scripts", "bootstrap.sh")
        venv_python = os.path.join(expected, "bin", "python")

    in_project_venv = (
        prefix_cmp == expected_cmp
        or prefix_cmp.startswith(expected_cmp + sep)
        or executable_cmp.startswith(expected_cmp + sep)
    )
    if in_project_venv:
        return

    raise SystemExit(
        "Refusing to run outside this clone's .venv.\n"
        "  active python : %s\n"
        "  expected venv : %s\n"
        "Bootstrap once, then use that interpreter:\n"
        "  %s\n"
        "  %s -m weread_exporter -b <book-id> ..."
        % (executable, expected, bootstrap, venv_python)
    )


def patch_windows():
    bin_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "bin", "win32")
    os.environ["PATH"] += ";" + bin_path
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(bin_path)


def _force_utf8_stdio():
    """Force stdout/stderr to UTF-8 so Chinese log lines survive Windows pipes.

    PowerShell Tee-Object historically decodes child output with the ANSI code
    page (GBK on zh-CN), which turns UTF-8 chapter titles into mojibake when
    redirected to a file. Reconfigure the streams (and hint PYTHONIOENCODING)
    before any logging starts.
    """
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _configure_logging(log_file=None):
    """Attach console + optional UTF-8 file handlers to the root logger.

    The file handler is the reliable path for Chinese text: it bypasses the
    shell entirely. Console output still goes to stderr for interactive runs.
    """
    logging.root.level = logging.INFO
    fmt = "[%(asctime)s][%(levelname)s]%(message)s"
    formatter = logging.Formatter(fmt)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in logging.root.handlers):
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logging.root.addHandler(stream_handler)
    if not log_file:
        return
    abs_path = os.path.abspath(log_file)
    for handler in logging.root.handlers:
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == abs_path:
            return
    log_dir = os.path.dirname(abs_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.FileHandler(abs_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)
    logging.info("Writing UTF-8 run log to %s" % abs_path)


def patch_generateRequestHash():
    from pyppeteer import network_manager

    orig_generateRequestHash = network_manager.generateRequestHash

    def patched_generateRequestHash(request):
        request["headers"].pop("Origin", None)
        return orig_generateRequestHash(request)

    network_manager.generateRequestHash = patched_generateRequestHash


def patch_connection_close():
    """Stop pyppeteer's Connection._on_close from wedging the whole process.

    pyppeteer 2.0.0 is unmaintained and has a hole in its teardown path, hit on
    every CDP link drop. `Connection._async_send` reacts to a closed socket by
    resolving its own callback and then calling `dispose()`:

        callback.set_result(None)      # that future is now DONE
        await self.dispose()           # -> _on_close()

    `_on_close` then sweeps *every* callback and calls `set_exception` on each —
    including the one just resolved — which raises `InvalidStateError` on an
    already-settled future. The raise aborts the loop before `self._callbacks`
    is cleared, so the callbacks after it are never resolved *or* rejected.
    Anything awaiting one of them waits forever, and no timeout can help because
    the future never completes: the process stays alive with a dead CDP link.

    Observed live twice on 《赢单九问》 (02:48 and 07:27): same
    `Task exception was never retrieved ... InvalidStateError('invalid state')`,
    each time leaving the export stuck — once for 4h46m — at 9/15 chapters.

    This wraps `_on_close` so every callback is settled exactly once and no
    failure can escape the loop. With the futures properly rejected, the
    pending `page.goto` raises instead of hanging, and the exporter's own retry
    (browser restart) recovers by itself.
    """
    from pyppeteer import connection as pyppeteer_connection

    async def safe_on_close(self):
        if self._closeCallback:
            try:
                self._closeCallback()
            finally:
                self._closeCallback = None

        for callback in list(self._callbacks.values()):
            try:
                # done() also covers cancelled futures; set_exception raises on both.
                if callback.done():
                    continue
                callback.set_exception(
                    pyppeteer_connection._rewriteError(
                        callback.error,
                        "Protocol error %s: Target closed." % callback.method,
                    )
                )
            except Exception:
                logging.exception(
                    "Swallowing error while settling CDP callback %s"
                    % getattr(callback, "method", "?")
                )
        self._callbacks.clear()

        for session in list(self._sessions.values()):
            try:
                session._on_closed()
            except Exception:
                logging.exception("Swallowing error while closing CDP session")
        self._sessions.clear()

        if hasattr(self, "connection"):
            try:
                await self.connection.close()
            except Exception:
                logging.exception("Swallowing error while closing the CDP socket")
        if not self._recv_fut.done():
            self._recv_fut.cancel()

    pyppeteer_connection.Connection._on_close = safe_on_close


async def async_main():
    from . import export, utils, webpage

    parser = argparse.ArgumentParser(
        prog="weread-exporter", description="WeRead book export cmdline tool"
    )
    parser.add_argument("-b", "--book-id", help="book id", required=True)
    parser.add_argument(
        "-o",
        "--output-format",
        help="output file format",
        action="append",
        choices=["md", "epub", "pdf", "mobi", "txt"],
    )
    parser.add_argument(
        "--load-timeout",
        help="load chapter page timeout",
        type=int,
        default=60,
    )
    parser.add_argument(
        "--load-interval",
        help="mean pause (seconds) between chapters; each pause is randomised "
        "within +/-40%% of it (default 20 -> 12..28s). 0 disables the pause",
        type=int,
        default=20,
    )
    parser.add_argument(
        "--css-file",
        help="overide default css style",
    )
    parser.add_argument(
        "--headless", help="chrome headless", action="store_true", default=False
    )
    parser.add_argument(
        "--force-login", help="force login first", action="store_true", default=False
    )
    parser.add_argument(
        "--use-default-profile",
        help="use default profile",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--mock-user-agent",
        help="use mock user-agent",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--proxy-server",
        help="http proxy server, e.g. http://127.0.0.1:8888",
    )
    parser.add_argument(
        "--system-chrome",
        help="use system Chrome instead of pyppeteer Chromium (may break on new Chrome)",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--compress-pdf-images",
        help="shrink the images embedded in the PDF (downscale + palette). Writes "
        "a separate <title>%s.pdf; the uncompressed images are left in place."
        % COMPRESS_PDF_SUFFIX,
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--pdf-image-max-side",
        help="with --compress-pdf-images: cap the longest image edge (pixels, 0 = keep size)",
        type=int,
        default=DEFAULT_PDF_IMAGE_MAX_SIDE,
    )
    parser.add_argument(
        "--pdf-image-colors",
        help="with --compress-pdf-images: palette size after downscaling "
        "(0 = keep truecolor)",
        type=int,
        default=DEFAULT_PDF_IMAGE_COLORS,
    )
    parser.add_argument(
        "--log-file",
        help="UTF-8 run log path (default: logs/<book-id>.run.log). "
        "Prefer this over shell Tee on Windows to keep Chinese readable.",
    )
    parser.add_argument(
        "--no-check",
        help="skip post-export completeness check (default: check each book)",
        action="store_true",
        default=False,
    )
    args = parser.parse_args()
    args.output_format = args.output_format or ["epub"]
    if "mobi" in args.output_format and "epub" not in args.output_format:
        args.output_format.append("epub")

    log_file = args.log_file
    if not log_file:
        safe_id = args.book_id.replace("/", "_").replace("\\", "_")
        log_file = os.path.join("logs", "%s.run.log" % safe_id)
    _configure_logging(log_file)

    extra_css = None
    if args.css_file:
        if not os.path.isfile(args.css_file):
            raise RuntimeError("CSS file %s not exist" % args.css_file)
        with open(args.css_file) as fp:
            extra_css = fp.read()

    if "_" in args.book_id:
        # book list id
        book_list = [it["id"] for it in await utils.get_book_list(args.book_id)]
    else:
        book_list = [args.book_id]

    for book_id in book_list:
        logging.info("Exporting book %s" % book_id)
        page = webpage.WeReadWebPage(
            book_id,
            cookie_path=os.path.join("cache", "cookie.txt"),
            webcache_path="cache",
        )
        if not await page.check_valid():
            logging.warning("Book %s status is invalid, stop exporting" % book_id)
            continue
        save_path = os.path.join("cache", book_id)
        output_dir = "output"
        if not os.path.isdir(output_dir):
            os.mkdir(output_dir)
        exporter = export.WeReadExporter(page, save_path)
        # Restarts are budgeted per chapter (see WeReadExporter.max_chapter_restarts),
        # so the book-wide allowance only needs to be a backstop against a loop
        # that never converges — sized off the real chapter count rather than the
        # flat 10 that used to abort a book whose chapters had each been restarted
        # once for unrelated reasons.
        try:
            chapter_total = await exporter.get_chapter_count()
        except Exception:
            logging.exception("Cannot read chapter count for book %s" % book_id)
            chapter_total = 10
        max_retries = chapter_total * max(2, export.WeReadExporter.max_chapter_restarts) + 5
        logging.info(
            "Book %s has %d chapters; restart budget for this book is %d"
            % (book_id, chapter_total, max_retries)
        )
        for retry_count in range(max_retries):
            try:
                await page.launch(
                    headless=args.headless,
                    force_login=args.force_login,
                    use_default_profile=args.use_default_profile,
                    mock_user_agent=args.mock_user_agent,
                    proxy_server=args.proxy_server,
                    use_system_chrome=args.system_chrome,
                )
            except RuntimeError:
                logging.exception("Launch book %s home page failed" % book_id)
                await asyncio.sleep(2)
                continue

            try:
                await exporter.export_markdown(args.load_timeout, args.load_interval)
            except utils.LoadChapterFailedError:
                logging.warning(
                    "Load chapter failed (retry %d/%d), close browser and retry"
                    % (retry_count + 1, max_retries)
                )
                await page.close()
            else:
                await page.close()
                break
        else:
            logging.error(
                "Book %s export failed after %d retries, giving up" % (book_id, max_retries)
            )
            await page.close()
            continue

        # Surface chapters that only got captured because a page turn stalled
        # twice (browser restart did not clear it). They are kept so the book
        # still exports, but they may be short — say so loudly rather than
        # handing over a silently incomplete book.
        stalled = sorted(
            cid for cid, n in getattr(page, "_page_turn_stalls", {}).items() if n > 1
        )
        if stalled:
            logging.warning(
                "Book %s: page turn stalled twice in %d chapter(s), kept as captured "
                "and they may be INCOMPLETE: %s"
                % (book_id, len(stalled), ", ".join(str(c) for c in stalled))
            )

        # Same idea for chapters that stayed short of WeRead's declared length
        # even after a re-fetch: exporting them keeps the book usable, but the
        # gap must not pass unnoticed. A re-fetch that changed the text points at
        # a real gap; one that changed nothing usually means the declared count
        # is simply higher than what the page renders.
        incomplete = getattr(exporter, "_incomplete_chapters", [])
        if incomplete:
            real = [it for it in incomplete if not it[5]]
            stable = [it for it in incomplete if it[5]]
            logging.warning(
                "Book %s: %d chapter(s) are below the declared length even after "
                "re-fetching (%d likely real gap, %d probably a declared-count "
                "difference):"
                % (book_id, len(incomplete), len(real), len(stable))
            )
            for index, title, captured, declared, ratio, is_stable in incomplete:
                logging.warning(
                    "  chapter %d %s: %d/%d chars (%.0f%%)%s",
                    index + 1,
                    title,
                    captured,
                    declared,
                    ratio * 100,
                    " [unchanged after re-fetch]" if is_stable else " [changed after re-fetch]",
                )

        # Chapters that used up their restart allowance. The book still exports;
        # these are the holes in it and must be named, not left to be discovered
        # by reading the PDF.
        skipped = getattr(exporter, "_skipped_chapters", [])
        if skipped:
            logging.error(
                "Book %s: %d chapter(s) were SKIPPED after exhausting their restart "
                "allowance and left as placeholders:" % (book_id, len(skipped))
            )
            for title, used, reason in skipped:
                logging.error("  %s (%d restarts): %s", title, used, reason)

        # Chapters that stayed short through every re-fetch: these are the real
        # holes in the book, and they must be named rather than left for whoever
        # reads the PDF to stumble over.
        unresolved = getattr(exporter, "_unresolved_chapters", [])
        if unresolved:
            logging.error(
                "Book %s: %d chapter(s) could NOT be captured in full and are "
                "INCOMPLETE:" % (book_id, len(unresolved))
            )
            for index, title, captured, declared, ratio in unresolved:
                if declared:
                    logging.error(
                        "  chapter %d %s: only %d/%d chars (%.0f%%)",
                        index + 1, title, captured, declared, ratio * 100,
                    )
                else:
                    logging.error(
                        "  chapter %d %s: nothing captured (%d chars written)",
                        index + 1, title, captured,
                    )

        await exporter.pre_process_markdown()
        title = await exporter.get_book_title()
        title = utils.format_filename(title)

        # Machine-readable verdict for this book, so an automated driver can tell
        # "finished" from "finished with holes" without re-deriving it from the
        # log. `unresolved` and `skipped` are the real gaps; `declared_count_diff`
        # are chapters where WeRead simply declares more words than it renders.
        save_path_cache = os.path.join("cache", book_id)
        try:
            summary = {
                "book_id": book_id,
                "title": title,
                "chapters": len((await exporter._load_meta_data())["chapters"]),
                "unresolved": [
                    {"index": i + 1, "title": t, "captured": c, "declared": d}
                    for (i, t, c, d, _r) in getattr(exporter, "_unresolved_chapters", [])
                ],
                "skipped": [
                    {"title": t, "restarts": n, "reason": r}
                    for (t, n, r) in getattr(exporter, "_skipped_chapters", [])
                ],
                "declared_count_diff": [
                    {"index": i + 1, "title": t, "captured": c, "declared": d}
                    for (i, t, c, d, _r, _s) in getattr(exporter, "_incomplete_chapters", [])
                ],
            }
            # encoding is not optional: without it Python uses the locale codec
            # (GBK on this machine), and check_book reads this file back as
            # UTF-8 — so every summary was silently unreadable and the gate fell
            # back to a bare ratio scan without saying so.
            with open(os.path.join(save_path_cache, "export_summary.json"), "w",
                      encoding="utf-8") as fp:
                fp.write(json.dumps(summary, ensure_ascii=False, indent=2))
        except Exception:
            logging.exception("Could not write export_summary.json for %s" % book_id)

        if "md" in args.output_format:
            save_path = os.path.join(output_dir, title)
            await exporter.export_markdown_files(save_path)
            logging.info("Save markdown files to %s complete" % save_path)

        if "epub" in args.output_format:
            save_path = os.path.join(output_dir, "%s.epub" % title)
            if os.path.isfile(save_path):
                logging.info("File %s exist, ignore export" % save_path)
            else:
                await exporter.markdown_to_epub(save_path, extra_css=extra_css)
                logging.info("Save file %s complete" % save_path)

        if "pdf" in args.output_format:
            suffix = COMPRESS_PDF_SUFFIX if args.compress_pdf_images else ""
            save_path = os.path.join(output_dir, "%s%s.pdf" % (title, suffix))
            # Rebuild when the chapters are newer than the PDF. An existing PDF
            # used to be kept unconditionally, which meant a chapter repaired on a
            # retry never made it into the PDF — the md bundle was refreshed and
            # the PDF silently stayed stale.
            chapters_dir = os.path.join("cache", book_id, "chapters")
            try:
                newest_chapter = max(
                    os.path.getmtime(os.path.join(chapters_dir, n))
                    for n in os.listdir(chapters_dir) if n.endswith(".md")
                )
            except (OSError, ValueError):
                newest_chapter = 0
            if os.path.isfile(save_path) and os.path.getmtime(save_path) >= newest_chapter:
                logging.info("File %s exist and is up to date, ignore export" % save_path)
            else:
                image_format = "jpg"
                if sys.platform == "win32":
                    image_format = "png"
                try:
                    await exporter.markdown_to_pdf(
                        save_path,
                        extra_css=extra_css,
                        image_format=image_format,
                        compress_images=args.compress_pdf_images,
                        image_max_side=args.pdf_image_max_side,
                        image_colors=args.pdf_image_colors,
                    )
                    logging.info("Save file %s complete" % save_path)
                except PermissionError:
                    # Windows: the file is open in a viewer. Do not lose the work —
                    # write beside it and say so plainly.
                    fallback = os.path.join(output_dir, "%s-new.pdf" % title)
                    logging.error(
                        "Cannot write %s (open in another program); writing %s instead"
                        % (save_path, fallback)
                    )
                    await exporter.markdown_to_pdf(
                        fallback,
                        extra_css=extra_css,
                        image_format=image_format,
                        compress_images=args.compress_pdf_images,
                        image_max_side=args.pdf_image_max_side,
                        image_colors=args.pdf_image_colors,
                    )
                    logging.info("Save file %s complete" % fallback)

        if "mobi" in args.output_format:
            if sys.platform != "linux":
                logging.error("Only linux system supported to export mobi format")
                return -1
            epub_path = os.path.join(output_dir, "%s.epub" % title)
            save_path = os.path.join(output_dir, "%s.mobi" % title)
            if os.path.isfile(save_path):
                logging.info("File %s exist, ignore export" % save_path)
            else:
                await exporter.epub_to_mobi(epub_path, save_path)
                if not os.path.isfile(save_path):
                    logging.warning("Create mobi file failed")
                    continue
                logging.info("Save file %s complete" % save_path)

        if "txt" in args.output_format:
            save_path = os.path.join(output_dir, "%s.txt" % title)
            if os.path.isfile(save_path):
                logging.info("File %s exist, ignore export" % save_path)
            else:
                await exporter.markdown_to_txt(save_path)
                logging.info("Save file %s complete" % save_path)

        if not args.no_check:
            from . import check_book

            # Judge cache/<id>/chapters + export_summary only (not output/).
            check_book.log_result(check_book.check(book_id))
    return 0


def main():
    ensure_project_venv()
    _force_utf8_stdio()
    if sys.platform == "win32":
        patch_windows()
    patch_generateRequestHash()
    patch_connection_close()
    _configure_logging()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(async_main())
    except:
        import traceback

        traceback.print_exc()
        return


if __name__ == "__main__":
    main()
