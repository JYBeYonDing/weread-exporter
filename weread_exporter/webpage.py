"""
WebRead WebPage
"""

import asyncio
import json
import logging
import os
import random
import sys
import time
import urllib.parse

import pyppeteer

from . import utils


class WeReadWebPage(object):
    """WebRead WebPage"""

    root_url = "https://weread.qq.com"
    window_size = (1920, 1080)
    # Hard ceiling for one "下一页" turn (click + canvas re-render). Without it a
    # stalled turn hangs for good: pyppeteer's click has no timeout of its own.
    page_turn_timeout = 60
    # Ceiling for tearing a browser down; a wedged CDP link must not block it.
    browser_close_timeout = 30
    # Hard ceiling for one in-place chapter reload (see _soft_reload_chapter).
    soft_reload_timeout = 45
    # Random pause (seconds) before a soft reload, so the retry does not hit the
    # server at a machine-regular moment right after the failed load.
    soft_reload_pause = (3, 6)

    def __init__(self, book_id, cookie_path=None, webcache_path=None):
        self._book_id = book_id
        self._cookie_path = cookie_path
        self._cookie = {}
        self._webcache_path = webcache_path or "cache"
        if not os.path.isdir(self._webcache_path):
            os.makedirs(self._webcache_path)
        self._home_url = "%s/web/bookDetail/%s" % (
            self.__class__.root_url,
            book_id,
        )
        self._chapter_root_url = self.__class__.root_url + "/web/reader/"
        self._browser = None
        self._page = None
        self._request_interception_ready = False
        # Per-chapter page-turn stall count. Lives on this object, not on the
        # page, so it survives the browser restarts the exporter performs.
        self._page_turn_stalls = {}
        # Per-chapter soft-reload count. Also kept on this object so it survives
        # browser restarts: a chapter gets one cheap reload, and after a restart
        # the restart itself is already the escalation, so it is not repeated.
        self._soft_reloads = {}
        self._chapter_id = None
        # Set when the current chapter had no text layer at all and was salvaged
        # by harvesting its <img> elements instead (see _check_next_page). The
        # exporter uses it to give such a chapter its title as a heading, since
        # no canvas means no title was drawn either.
        self._image_only_chapter = False
        self._load_cookie()
        self._url = ""

    async def get_book_info(self):
        html = (await utils.fetch(self._home_url)).decode()
        pos1 = html.find("window.__INITIAL_STATE__")
        if pos1 <= 0:
            raise RuntimeError("Unexpected html: %s" % self._html)
        pos1 = html.find("=", pos1)
        pos2 = html.find("};", pos1)
        data = html[pos1 + 1 : pos2 + 1].strip()
        data = json.loads(data)
        book_info = {}
        book_info["title"] = data["reader"]["bookInfo"]["title"]
        book_info["author"] = data["reader"]["bookInfo"]["author"]
        book_info["cover"] = data["reader"]["bookInfo"]["cover"]
        book_info["intro"] = data["reader"]["bookInfo"]["intro"]
        book_info["chapters"] = []
        for chapter in data["reader"]["chapterInfos"]:
            chap = {
                "id": chapter["chapterUid"],
                "title": chapter["title"],
                "level": chapter["level"],
                "words": chapter["wordCount"],
                "anchors": [],
            }
            if chapter["anchors"]:
                for it in chapter["anchors"]:
                    chap["anchors"].append({"title": it["title"], "level": it["level"]})
            book_info["chapters"].append(chap)
        return book_info

    async def get_user_info(self):
        vid = self._cookie.get("wr_vid")
        if not vid:
            raise utils.InvalidUserError("Invalid cookie: %s" % self._format_cookie())
        url = "%s/web/user?userVid=%s" % (self.__class__.root_url, vid)
        headers = {"Referer": self.__class__.root_url, "Cookie": self._format_cookie()}
        rsp = await utils.fetch(url, headers=headers)
        rsp = json.loads(rsp.decode())
        if rsp.get("errCode") == -2012:
            _, rsp_headers, _ = await utils.fetch(
                self.__class__.root_url, headers=headers, respond_with_headers=True
            )
            for it in rsp_headers.getall("Set-Cookie", []):
                cookie = it.split("; ")[0]
                if "=" not in cookie:
                    logging.warning(
                        "[%s] Ignore invalid cookie: %s"
                        % (self.__class__.__name__, cookie)
                    )
                    continue
                key, value = cookie.split("=", 1)
                self._cookie[key] = value
                logging.info(
                    "[%s] Update cookie %s" % (self.__class__.__name__, cookie)
                )
            self._save_cookie()
            headers["Cookie"] = self._format_cookie()
            rsp = await utils.fetch(url, headers=headers)
            rsp = json.loads(rsp.decode())
        elif rsp.get("errCode") == -2010:
            # 用户不存在
            raise utils.InvalidUserError("User %s not found" % vid)
        elif rsp.get("errCode"):
            raise RuntimeError("Get user info failed: %s" % rsp)
        return rsp

    def _parse_cookie_string(self, cookie):
        """Parse browser cookie string like `k1=v1; k2=v2` into a dict."""
        cookie_map = {}
        for it in cookie.split(";"):
            it = it.strip()
            if not it or "=" not in it:
                continue
            key, value = it.split("=", 1)
            key = key.strip()
            if key:
                cookie_map[key] = value.strip()
        return cookie_map

    def _load_cookie(self):
        """Load cookies from txt file as browser cookie string (or legacy JSON)."""
        self._cookie = {}
        if not os.path.isfile(self._cookie_path):
            return
        with open(self._cookie_path, encoding="utf-8") as fp:
            cookie = fp.read().strip()
        if not cookie:
            return
        try:
            cookie_obj = json.loads(cookie)
        except ValueError:
            self._cookie = self._parse_cookie_string(cookie)
        else:
            if isinstance(cookie_obj, dict):
                self._cookie = {str(key): str(cookie_obj[key]) for key in cookie_obj}
            else:
                self._cookie = self._parse_cookie_string(cookie)

    def _save_cookie(self):
        """Save cookies to txt file as browser cookie string."""
        if not self._cookie_path:
            return
        with open(self._cookie_path, "w", encoding="utf-8") as fp:
            fp.write(self._format_cookie())

    def _format_cookie(self, cookie=""):
        """Format cookie dict as browser cookie header string."""
        cookies = []
        if cookie:
            cookies.append(cookie)
        for key in self._cookie:
            cookies.append("%s=%s" % (key, self._cookie[key]))
        return "; ".join(cookies)

    async def _read_cookie(self):
        cookies = await self._page.cookies()
        cookie_map = {}
        for cookie in cookies:
            cookie_map[cookie["name"]] = cookie["value"]
        return cookie_map

    async def _update_cookie(self):
        self._cookie = await self._read_cookie()

    async def check_valid(self):
        html = await utils.fetch(self._home_url)
        if b'"soldout":1' in html:
            return False
        return True

    def _check_chrome(self):
        """Locate system Chrome executable from PATH or common install locations."""
        path_list = os.environ["PATH"].split(";" if sys.platform == "win32" else ":")
        for chrome in ("chrome", "google-chrome"):
            if sys.platform == "win32":
                chrome += ".exe"
            for path in path_list:
                if os.path.isfile(os.path.join(path, chrome)):
                    return chrome

        if sys.platform == "darwin":
            chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if os.path.isfile(chrome):
                return chrome

        if sys.platform == "win32":
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
            program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
            for chrome in (
                os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
            ):
                if chrome and os.path.isfile(chrome):
                    return chrome

        if sys.platform == "win32":
            command = "where chrome"
        else:
            command = "which chrome"
        raise utils.ChromeNotInstalledError(
            "Please make sure `chrome` is installed, and the install path is added to PATH environment. \nYou can test that with `%s` command."
            % command
        )

    def _ensure_pyppeteer_chromium(self):
        """Ensure pyppeteer's bundled Chromium is downloaded; return its executable path.

        New system Chrome breaks pyppeteer's request interception. Prefer the
        bundled Chromium, and fall back to a known-downloadable revision when
        pyppeteer's default snapshot URL is gone.
        """
        # Known-good Win/Mac/Linux snapshot IDs when default REVISION 404s.
        fallback_revisions = ("1095492", "1002410", "982053")

        def _try_revision(revision=None):
            if revision:
                os.environ["PYPPETEER_CHROMIUM_REVISION"] = str(revision)
            # Import after env is set so REVISION picks up the override.
            from importlib import reload
            import pyppeteer.chromium_downloader as chromium_downloader

            reload(chromium_downloader)
            if not chromium_downloader.check_chromium():
                logging.info(
                    "[%s] Downloading pyppeteer Chromium revision %s (system Chrome unchanged)"
                    % (
                        self.__class__.__name__,
                        chromium_downloader.REVISION,
                    )
                )
                chromium_downloader.download_chromium()
            chrome = str(chromium_downloader.chromium_executable())
            if not os.path.isfile(chrome):
                raise utils.ChromeNotInstalledError(
                    "pyppeteer Chromium not found at %s" % chrome
                )
            return chrome

        preferred = os.environ.get("PYPPETEER_CHROMIUM_REVISION")
        revisions = []
        if preferred:
            revisions.append(preferred)
        revisions.extend(r for r in fallback_revisions if r not in revisions)

        last_error = None
        for revision in revisions:
            try:
                chrome = _try_revision(revision)
                logging.info(
                    "[%s] Using pyppeteer Chromium: %s"
                    % (self.__class__.__name__, chrome)
                )
                return chrome
            except Exception as ex:
                last_error = ex
                logging.warning(
                    "[%s] Chromium revision %s unavailable: %s"
                    % (self.__class__.__name__, revision, ex)
                )

        raise utils.ChromeNotInstalledError(
            "Failed to download pyppeteer Chromium. "
            "Set PYPPETEER_CHROMIUM_REVISION to a valid snapshot, or use --system-chrome. "
            "Last error: %s" % last_error
        )

    def _resolve_browser_executable(self, use_system_chrome=False):
        """Resolve browser binary: bundled Chromium by default, or system Chrome on demand."""
        if use_system_chrome:
            chrome = self._check_chrome()
            logging.warning(
                "[%s] Using system Chrome (%s). New Chrome may break request interception."
                % (self.__class__.__name__, chrome)
            )
            return chrome
        return self._ensure_pyppeteer_chromium()

    async def launch(
        self,
        headless=False,
        force_login=False,
        use_default_profile=False,
        mock_user_agent=False,
        proxy_server=None,
        use_system_chrome=False,
    ):
        """Launch browser. Default uses pyppeteer Chromium to keep CDP interception working."""
        logging.info("[%s] Launch url %s" % (self.__class__.__name__, self._home_url))
        chrome = self._resolve_browser_executable(use_system_chrome=use_system_chrome)
        args = ["--no-first-run", "--remote-allow-origins=*"]
        if sys.platform == "linux":
            # Avoid Chrome crashes caused by /dev/shm being too small (common on Linux)
            args.append("--disable-dev-shm-usage")
        if headless:
            args.append("--headless")
            if sys.platform == "linux" and os.getuid() == 0:
                args.append("--no-sandbox")
        if use_default_profile:
            args.append("--user-data-dir")
        else:
            args.append("--window-size=%d,%d" % self.__class__.window_size)
        if mock_user_agent:
            args.append('--user-agent="%s"' % utils.generate_user_agent())
        if proxy_server:
            args.append("--proxy-server=%s" % proxy_server)
        args.append("about:blank")
        logging.info(
            "[%s] Chrome args: %s %s"
            % (self.__class__.__name__, chrome, " ".join(args))
        )
        self._browser = await pyppeteer.launch(
            executablePath=chrome,
            ignoreDefaultArgs=True,
            args=args,
            defaultViewport=None,
            logLevel=logging.INFO,
        )
        self._page = (await self._browser.pages())[0]
        await self._page.evaluateOnNewDocument(
            """() => {
            if (navigator.webdriver) {
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => {
                        console.log('navigator.webdriver is called');
                        console.trace();
                        return undefined;
                    }
                });
                var _hasOwnProperty = Object.prototype.hasOwnProperty;
                Object.prototype.hasOwnProperty = function (key) {
                    if (key === 'webdriver') {
                        console.log('hasOwnProperty', key, 'is called');
                        console.trace();
                        return false;
                    }
                    return _hasOwnProperty.call(this, key);
                };
                const originalQuery = navigator.permissions.query;
                navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
                );
            }
            if (navigator.plugins.length === 0) {
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                Object.defineProperty(window, 'PluginArray', {
                    get: () => Array,
                });
            }
            if (navigator.languages.length === 0) {
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });
            }
            window.chrome = window.chrome || {
                runtime: {},
            };
        }
        """
        )

        await self._page.setViewport(
            {
                "width": 0,
                "height": 0,
                "deviceScaleFactor": 0.3,
            }
        )
        if self._cookie.get("wr_vid"):
            logging.info("[%s] Checking login user info" % self.__class__.__name__)
            try:
                user_info = await self.get_user_info()
            except utils.InvalidUserError as ex:
                logging.warning(
                    "[%s] Get user error: %s" % (self.__class__.__name__, ex)
                )
                self._cookie = {}
            except Exception as ex:
                logging.warning(
                    "[%s] Get user info failed, continue with cookie: %s"
                    % (self.__class__.__name__, ex)
                )
            else:
                logging.info(
                    "[%s] Current login user is %s"
                    % (self.__class__.__name__, user_info.get("name", "Anonymous"))
                )
        if self._cookie:
            await self._inject_cookie()

        logging.info("[%s] Opening home page %s" % (self.__class__.__name__, self._home_url))
        await self._page.goto(self._home_url)
        # await self.wait_for_selector("div.readerFooter a")
        if force_login:
            await self.login()
        if self._cookie:
            logging.info("[%s] Waiting for avatar" % self.__class__.__name__)
            await self.wait_for_avatar()
        self._page.on("console", self.handle_log)

    async def close(self):
        """Close browser and reset page-related state.

        Bounded: after a stalled page turn the CDP link can be wedged, and a
        graceful close would then hang forever — which would strand the whole
        export before the retry that is supposed to recover it.
        """
        if not self._browser:
            return
        try:
            await asyncio.wait_for(
                self._browser.close(), timeout=self.browser_close_timeout
            )
        except Exception as ex:
            logging.warning(
                "[%s] Browser close did not finish (%s); killing the process tree"
                % (self.__class__.__name__, type(ex).__name__)
            )
            proc = getattr(self._browser, "process", None)
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    logging.exception(
                        "[%s] Killing the browser process failed"
                        % self.__class__.__name__
                    )
        finally:
            self._browser = self._page = None
            self._request_interception_ready = False

    async def get_html(self):
        return await self._page.evaluate("document.documentElement.outerHTML;")

    async def screenshot(self, save_path):
        await self._page.screenshot({"path": save_path})

    async def wait_for_selector(self, selector, timeout=30, dump_on_timeout=True):
        """Wait for CSS selector. timeout is milliseconds (pyppeteer convention)."""
        try:
            return await self._page.waitForSelector(selector, timeout=timeout)
        except pyppeteer.errors.TimeoutError as ex:
            if dump_on_timeout:
                html = await self.get_html()
                html_path = "webpage.html"
                with open(html_path, "wb") as fp:
                    if not isinstance(html, bytes):
                        html = html.encode("utf8")
                    fp.write(html)
                logging.info(
                    "[%s] Current html saved to %s" % (self.__class__.__name__, html_path)
                )
                screenshot_path = "screenshot.jpg"
                await self.screenshot(screenshot_path)
                logging.info(
                    "[%s] Current screenshot saved to %s"
                    % (self.__class__.__name__, screenshot_path)
                )
            raise ex

    def handle_log(self, message):
        # 只落盘 warning/error，避免 hook.js 的 canvas 调试日志（每页数千条
        # fillRect/fillStyle）在事件循环里同步刷盘，把 CDP 消息处理饿死导致
        # page.goto 超时。此前每条 console 消息都 open/write/close 一次，
        # 一个章节就能写 18MB 日志。
        if message.type not in ("warning", "error"):
            return
        with open("%s.log" % self._book_id, "a+", encoding="utf-8") as fp:
            fp.write("[%s] %s\n" % (self._url, message.text))

    async def wait_for_avatar(self, timeout=30):
        time0 = time.time()
        while time.time() - time0 < timeout:
            avatar_url = await self._page.evaluate(
                "document.querySelector('img.wr_avatar_img') && document.querySelector('img.wr_avatar_img').getAttribute('src');"
            )
            if avatar_url is None or not avatar_url.endswith("Default.svg"):
                break
            await asyncio.sleep(5)
        else:
            raise RuntimeError("Wait for avatar timeout")

    async def _inject_cookie(self):
        for key in self._cookie:
            logging.info(
                "[%s] Inject cookie %s=%s"
                % (self.__class__.__name__, key, self._cookie[key])
            )
            await self._page.setCookie(
                {
                    "url": self.__class__.root_url,
                    "name": key,
                    "value": self._cookie[key],
                    "secure": True,
                }
            )

    async def login(self):
        selectors = [
            "button.navBar_link_Login",
            "div.readerTopBar_right button.actionItem",
        ]
        for selector in selectors:
            script = (
                "var elem = document.querySelector('%s'); elem && elem.innerText"
                % (selector)
            )
            result = await self._page.evaluate(script)
            if not result:
                continue
            if "登录" not in result:
                continue
            await self._page.click(selector)
            script = "document.querySelector('div.menu_container img.wr_avatar_img')"
            time0 = time.time()
            while time.time() - time0 < 300:
                logging.info("[%s] Waiting for login" % self.__class__.__name__)
                await asyncio.sleep(10)
                result = await self._page.evaluate(script)
                if not result:
                    continue
                logging.info("[%s] Login success" % self.__class__.__name__)
                await self._update_cookie()
                self._save_cookie()
                return True
            else:
                raise RuntimeError("Login timeout")
        return False

    def _guess_content_type(self, path):
        mime_map = {
            ".js": "text/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".wasm": "application/wasm",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".webp": "image/webp",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".ttf": "font/ttf",
            ".mp3": "audio/mpeg",
            ".mp4": "video/mp4",
        }
        ext = os.path.splitext(path)[1].lower()
        return mime_map.get(ext, "application/octet-stream")

    async def _get_from_cache_or_server(self, url, headers=None):
        u = urllib.parse.urlparse(url)
        path = os.path.join(
            self._webcache_path, "resources", u.path[1:].replace("/", os.sep)
        )
        if os.path.isfile(path):
            with open(path, "rb") as fp:
                body = fp.read()
            # 缓存命中必须带上 Content-Type 与 CORS 头，否则 wasm/font 等
            # 跨域资源（cdn.weread.qq.com）会被浏览器 CORS 拦截，导致阅读器
            # 的 wasm hash 模块初始化失败、章节内容永远加载不出来。
            resp_headers = {
                "Content-Type": self._guess_content_type(path),
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Request-Method": "*",
                "Access-Control-Allow-Headers": "*",
            }
            return 200, resp_headers, body

        dirpath = os.path.dirname(path)
        if not os.path.isdir(dirpath):
            os.makedirs(dirpath)
        status, headers, body = await utils.fetch(
            url, headers=headers, respond_with_headers=True
        )
        if status == 200:
            with open(path, "wb") as fp:
                fp.write(body)
        return status, dict(headers), body

    def _handle_request_headers(self, url, headers):
        for key in ("baggage", "sentry-trace"):
            headers.pop(key, None)
        cookie = ""
        if "/web/reader/" in url:
            cookie = "wr_useHorizonReader=0"
        headers["cookie"] = self._format_cookie(cookie)
        return headers

    def _handle_response_headers(self, url, headers):
        # cdn.weread.qq.com 的 wasm/font/js 都是跨域资源，必须放行 CORS，
        # 否则阅读器的 wasm hash 模块初始化失败、章节内容加载不出来。
        if "weread.qq.com" in url or "gtimg.cn" in url:
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Request-Method"] = "*"
            headers["Access-Control-Allow-Headers"] = "*"
        return headers

    def _handle_http_body(self, url, body):
        if url.startswith(self._chapter_root_url):
            inject_script = (
                "<script src='https://cdn.weread.qq.com/web/1.392ec47a.js'></script>\n"
            )
            return body.replace(b"</head>", inject_script.encode() + b"</head>")
        return body

    async def _handle_request(self, request):
        """Proxy/mock an intercepted browser request; always respond or abort."""
        try:
            await self._handle_request_inner(request)
        except Exception:
            logging.exception(
                "[%s] Handle request failed: %s" % (self.__class__.__name__, request.url)
            )
            try:
                await request.abort()
            except Exception:
                pass

    async def _handle_request_inner(self, request):
        """Implement request interception responses for WeRead reader pages."""
        if request.url.startswith("chrome-extension://"):
            return await request.continue_()

        if "/web/1.392ec47a.js" in request.url:
            with open(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook.js"),
                "rb",
            ) as fp:
                hook_script = fp.read()
                response = {
                    "status": 200,
                    "headers": {
                        "Content-Type": "text/javascript",
                        "Access-Control-Allow-Origin": "*",
                    },
                    "body": hook_script,
                }
                return await request.respond(response)

        # CSP report / analytics should never block page load under interception.
        if "hijack_csp_report" in request.url:
            return await request.abort()

        urlobj = urllib.parse.urlparse(request.url)
        is_resource_file = urlobj.path.endswith(
            (".js", ".css", ".jpg", ".png", ".gif", ".svg", ".woff", ".woff2", ".wasm")
        )
        status = 200
        headers = {
            "Server": "nginx/1.20.2",
        }
        body = b""
        if request.method == "OPTIONS":
            headers = {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Request-Method": "*",
                "Access-Control-Allow-Headers": "*",
            }
        elif request.method == "GET" and is_resource_file:
            status, headers, body = await self._get_from_cache_or_server(request.url)
        elif "/web/book/read" in request.url:
            body = b'{"succ":1,"synckey":%d}' % random.randint(10000000, 100000000)
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(body))
            logging.info(
                "[%s][%s] Url %s return mock data"
                % (self.__class__.__name__, request.method, request.url)
            )
        elif "sentry_key=" in request.url:
            status = 200
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(body))
            body = b"{}"
            logging.info(
                "[%s][%s] Url %s return mock data"
                % (self.__class__.__name__, request.method, request.url)
            )
        elif "/hera/logkv" in request.url or "/hera/osslog" in request.url:
            status = 204
        elif "/hera/chlog" in request.url:
            body = b'{"ret":0}'
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(body))
            logging.info(
                "[%s][%s] Url %s return mock data"
                % (self.__class__.__name__, request.method, request.url)
            )
        elif "/river/single" in request.url:
            body = b'{"err_code":0,"msg":"suc"}'
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(body))
            logging.info(
                "[%s][%s] Url %s return mock data"
                % (self.__class__.__name__, request.method, request.url)
            )
        else:
            logging.info(
                "[%s][%s] Fetch url %s"
                % (self.__class__.__name__, request.method, request.url)
            )
            headers = self._handle_request_headers(request.url, request.headers)
            time0 = time.time()
            status, headers, body = await utils.fetch(
                request.url,
                method=request.method,
                headers=headers,
                data=request.postData,
                respond_with_headers=True,
            )
            headers = dict(headers)
            logging.info(
                "[%s][%s][%.2f] Url %s return %d, body len is %d"
                % (
                    self.__class__.__name__,
                    request.method,
                    time.time() - time0,
                    request.url,
                    status,
                    len(body),
                )
            )
            if "Content-Security-Policy" in headers:
                logging.info(
                    "[%s][%s] Url %s has Content-Security-Policy: %s"
                    % (
                        self.__class__.__name__,
                        request.method,
                        request.url,
                        headers["Content-Security-Policy"],
                    )
                )
                headers.pop("Content-Security-Policy")

        headers = self._handle_response_headers(request.url, headers)
        response = {
            "status": status,
            "headers": headers,
            "body": self._handle_http_body(request.url, body),
        }
        return await request.respond(response)

    def handle_request(self, request):
        asyncio.ensure_future(self._handle_request(request))

    async def pre_load_page(self):
        """Inject hook.js into reader pages, once per browser page.

        Previously this enabled pyppeteer request interception and served
        hook.js by faking the response for the reader bundle
        `cdn.weread.qq.com/web/1.392ec47a.js`. The current WeRead reader stalls
        under interception: it never issues its content API calls
        (`/web/book/readInfo`, `/web/book/chapter/*`, `/web/book/read`) and
        never loads the wasm/font resources, so chapters stay in LOADING and
        nothing is drawn to the canvas. Injecting the hook through CDP needs no
        interception, and the reader then loads content normally.

        Note: pyppeteer's own `page.evaluateOnNewDocument()` does not take
        effect here, so the raw CDP command is used.
        """
        if self._request_interception_ready:
            return
        hook_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "hook.js"
        )
        with open(hook_path, encoding="utf-8") as fp:
            hook_script = fp.read()
        await self._page._client.send(
            "Page.addScriptToEvaluateOnNewDocument", {"source": hook_script}
        )
        self._request_interception_ready = True
        logging.info(
            "[%s] hook.js injected via CDP (request interception disabled)"
            % self.__class__.__name__
        )

    async def wait_for_canvas_complete(self, timeout=30):
        """Wait until hook.js finishes capturing the current page into markdown."""
        script = (
            "typeof canvasContextHandler !== 'undefined' && "
            "canvasContextHandler.data && canvasContextHandler.data.complete;"
        )
        time0 = time.time()
        while time.time() - time0 < timeout:
            try:
                result = await self._page.evaluate(script)
            except Exception:
                result = False
            if result:
                logging.info(
                    "[%s] Canvas markdown ready in %.1fs"
                    % (self.__class__.__name__, time.time() - time0)
                )
                return True
            await asyncio.sleep(0.5)
        logging.warning(
            "[%s] Canvas markdown not ready after %ds"
            % (self.__class__.__name__, timeout)
        )
        return False

    async def get_markdown(self):
        """Return markdown captured by the Canvas hook for the current chapter."""
        await self.wait_for_canvas_complete(timeout=15)
        script = "canvasContextHandler.data.markdown;"
        result = await self._page.evaluate(script)
        if not result:
            await self._page.evaluate("canvasContextHandler.updateMarkdown();")
            result = await self._page.evaluate(script)
            if not result:
                raise RuntimeError("Wait for creating markdown timeout")
        return result

    async def _get_footer_button_text(self):
        """Return visible reader footer button text, or empty string if absent."""
        return await self._page.evaluate(
            """() => {
                const btn = document.querySelector('button.readerFooter_button');
                return btn ? (btn.innerText || '').trim() : '';
            }"""
        )

    async def reopen_chapter_at_start(self, chapter):
        """Force the reader to render `chapter` from its beginning.

        WeRead restores the account's stored reading offset for the chapter, so a
        chapter whose saved position sits near its end renders only the tail and
        then reports "下一章" straight away. 《销售读心术》 chapters 3/6/7 came back
        at 14%/26%/2% of their declared length this way, and the text always
        started mid-paragraph with no heading — the reader had simply resumed
        where it left off. Measured on the real page: scrolling the window and
        every scrollable container to the top changes nothing, a plain URL
        re-navigation changes nothing, but re-entering the chapter from the
        reader's own catalogue re-renders it (captured text grew 477 -> 1437
        chars, canvas 3307 -> 5515px). That catalogue click is what this does.

        Returns True when a catalogue entry for the chapter was found and
        clicked; False means the caller should fall back to a plain re-navigation.
        """
        title = (chapter.get("title") or "").strip()
        if not title:
            return False
        try:
            await self._page.evaluate(
                """() => {
                    const b = [...document.querySelectorAll('button, div, span')]
                        .find(e => (e.innerText || '').trim() === '目录');
                    if (b) b.click();
                }"""
            )
        except Exception:
            logging.exception(
                "[%s] Opening the catalogue failed" % self.__class__.__name__
            )
            return False
        await asyncio.sleep(2)
        try:
            clicked = await self._page.evaluate(
                """(title) => {
                    const all = [...document.querySelectorAll(
                        '.readerCatalog_list_item, .readerCatalog_list_item_default, li, div, span')];
                    const hit = all.filter(e => (e.innerText || '').trim() === title);
                    if (!hit.length) return false;
                    // innermost match wins: the outer list rows repeat the title
                    hit[hit.length - 1].click();
                    return true;
                }""",
                title,
            )
        except Exception:
            logging.exception(
                "[%s] Clicking the catalogue entry failed" % self.__class__.__name__
            )
            return False
        if clicked:
            logging.info(
                "[%s] Re-opened chapter %s from the catalogue to reset the "
                "reading position" % (self.__class__.__name__, title)
            )
        return bool(clicked)

    async def _soft_reload_chapter(self, page_timeout):
        """Reload the current chapter page once on the same browser.

        Used as the first, cheap answer to "the reader drew nothing": a reload
        re-runs the reader's content fetch without paying for a browser relaunch
        (cookie injection, home page, login check). hook.js is registered through
        CDP for every new document, so the reload gets a fresh hook state.

        Each chapter is allowed one soft reload (the counter outlives browser
        restarts). Returns True when the canvas finished rendering after the
        reload, False when the budget is spent, the reload failed, or the reader
        still drew nothing — the caller then escalates to a browser restart.
        """
        chapter = self._chapter_id
        if self._soft_reloads.get(chapter, 0) >= 1:
            return False
        self._soft_reloads[chapter] = self._soft_reloads.get(chapter, 0) + 1

        pause = random.uniform(*self.soft_reload_pause)
        logging.warning(
            "[%s] Chapter %s drew nothing; soft reload in %.1fs before "
            "resorting to a browser restart"
            % (self.__class__.__name__, chapter, pause)
        )
        await asyncio.sleep(pause)
        try:
            await asyncio.wait_for(
                self._page.reload(waitUntil="domcontentloaded"),
                timeout=self.soft_reload_timeout,
            )
        except Exception:
            logging.warning(
                "[%s] Soft reload of chapter %s failed; escalating"
                % (self.__class__.__name__, chapter),
                exc_info=True,
            )
            return False

        ready = await self.wait_for_canvas_complete(
            timeout=min(40, max(15, page_timeout))
        )
        if ready:
            logging.info(
                "[%s] Soft reload recovered chapter %s without a browser restart"
                % (self.__class__.__name__, chapter)
            )
        return bool(ready)

    async def _advance_to_next_page(self, page_timeout):
        """Click "下一页" and wait for the canvas to re-render the next page."""
        await self._page.evaluate(
            """() => {
                canvasContextHandler.data.complete = false;
                canvasContextHandler.data.markdown += '\\n\\n';
            }"""
        )
        await self._page.click("button.readerFooter_button")
        return await self.wait_for_canvas_complete(
            timeout=min(40, max(15, page_timeout))
        )

    async def _check_next_page(self, page_timeout=30):
        """Wait for chapter content, then flip pages if legacy footer UI exists.

        New WeRead web UI often has no `button.readerFooter_button` (continuous
        scroll). In that case we only wait for Canvas capture and return.
        """
        ready = await self.wait_for_canvas_complete(
            timeout=min(40, max(15, page_timeout))
        )
        if not ready:
            # Raising here keeps the failure inside the exporter's retry loop.
            # Letting it slide meant get_markdown() blew up later and aborted the
            # whole book — which is what a single reader-side
            # FETCH_READER_CHAPTER_CONTENT failure did to 《优势谈判》.
            try:
                has_text = await self._page.evaluate(
                    "(typeof canvasContextHandler !== 'undefined' && "
                    "canvasContextHandler.data.markdown || '').length > 0"
                )
            except Exception:
                has_text = False
            if not has_text:
                # A chapter WeRead renders as pure images never gets a canvas at
                # all — the reader only paints text through canvas, and an
                # image-only page has none — so the hook waits forever. Such
                # pages do exist as a chapter's whole content (《销售是个专业活》
                # ends with a 9-page scanned 后记). Before declaring the chapter
                # unrenderable, ask the hook to harvest the <img> elements the
                # reader already laid out; that is real content, not a failure.
                try:
                    await self._page.evaluate(
                        "canvasContextHandler.updateMarkdown();"
                    )
                    has_text = await self._page.evaluate(
                        "(typeof canvasContextHandler !== 'undefined' && "
                        "canvasContextHandler.data.markdown || '').length > 0"
                    )
                except Exception:
                    has_text = False
                if has_text:
                    logging.info(
                        "[%s] No canvas drawn but images captured — treating as "
                        "an image-only chapter" % self.__class__.__name__
                    )
                    self._image_only_chapter = True
                    # Mark the page done so get_markdown() does not sit through
                    # its own 15s wait for a canvas that will never be drawn.
                    try:
                        await self._page.evaluate(
                            "canvasContextHandler.data.complete = true;"
                        )
                    except Exception:
                        pass
                else:
                    # Nothing drawn and nothing to harvest. Reloading the page on
                    # the same browser is far cheaper than a full browser restart
                    # and clears most one-off reader fetch failures; only when it
                    # fails too does the exporter escalate to a restart.
                    if not await self._soft_reload_chapter(page_timeout):
                        raise utils.ChapterContentMissingError(
                            "Chapter content never rendered (reader fetch failed?)"
                        )

        # Legacy page-turn UI: poll briefly; missing footer is normal for new UI.
        footer_wait_ms = 3000
        while True:
            try:
                await self.wait_for_selector(
                    "button.readerFooter_button",
                    timeout=footer_wait_ms,
                    dump_on_timeout=False,
                )
            except pyppeteer.errors.TimeoutError:
                logging.info(
                    "[%s] No page-turn footer button, treat as chapter complete"
                    % self.__class__.__name__
                )
                break

            result = await self._get_footer_button_text()
            if result == "下一页":
                logging.info("[%s] Go to next page" % self.__class__.__name__)
                try:
                    ready = await asyncio.wait_for(
                        self._advance_to_next_page(page_timeout),
                        timeout=self.page_turn_timeout,
                    )
                except asyncio.TimeoutError:
                    # The click round-trip can wedge; the page never reports
                    # back and nothing here would ever time out on its own.
                    chapter = self._chapter_id
                    seen = self._page_turn_stalls.get(chapter, 0) + 1
                    self._page_turn_stalls[chapter] = seen
                    if seen == 1:
                        logging.warning(
                            "[%s] Page turn in chapter %s did not finish within %ds; "
                            "restarting the browser and retrying the chapter"
                            % (
                                self.__class__.__name__,
                                chapter,
                                self.page_turn_timeout,
                            )
                        )
                        raise utils.PageTurnStalledError(
                            "Page turn stalled in chapter %s" % chapter
                        )
                    logging.error(
                        "[%s] Page turn in chapter %s stalled again (%d times) after a "
                        "browser restart; keeping the captured text and moving on — "
                        "this chapter may be INCOMPLETE"
                        % (self.__class__.__name__, chapter, seen)
                    )
                    return
                if not ready:
                    break
            elif result in ("下一章", "全书已读完", "全书完", "标记读完", "已读完所有章节"):
                logging.info("[%s] Chapter end: %r" % (self.__class__.__name__, result))
                break
            elif result.startswith("登录"):
                raise utils.LoginRequiredError()
            else:
                logging.warning(
                    "[%s] Unknown footer button: %r, treating as chapter end"
                    % (self.__class__.__name__, result)
                )
                break

    def _get_chapter_url(self, chapter_id):
        return "%s%sk%s" % (
            self._chapter_root_url,
            self._book_id,
            utils.wr_hash(str(chapter_id)),
        )

    async def goto_chapter(self, chapter_id, timeout=120):
        """Open a chapter page and flip through all pages until chapter end."""
        logging.info("[%s] Go to chapter %s" % (self.__class__.__name__, chapter_id))
        # await self.clear_cache()
        await self.pre_load_page()
        self._chapter_id = chapter_id
        self._image_only_chapter = False
        self._url = self._get_chapter_url(chapter_id)
        # domcontentloaded avoids hanging on late analytics/fonts under interception.
        logging.info("[%s] Navigate to %s" % (self.__class__.__name__, self._url))
        await self._page.goto(
            self._url, timeout=1000 * timeout, waitUntil="domcontentloaded"
        )
        logging.info("[%s] Chapter page loaded, checking pages" % self.__class__.__name__)
        page_timeout = max(20, timeout - 10)
        try:
            await self._check_next_page(page_timeout=page_timeout)
        except utils.LoginRequiredError:
            await self.login()
            return await self.goto_chapter(chapter_id, timeout=timeout)

    async def clear_cache(self):
        await self._page.evaluate("canvasContextHandler.clearCanvasCache();")
