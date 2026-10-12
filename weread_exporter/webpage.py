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
        """Close browser and reset page-related state."""
        if self._browser:
            await self._browser.close()
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
        """Enable request interception once per browser page (avoid duplicate handlers)."""
        if self._request_interception_ready:
            return
        await self._page.setRequestInterception(True)
        self._page.on("request", self.handle_request)
        self._request_interception_ready = True

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

    async def _check_next_page(self, page_timeout=30):
        """Wait for chapter content, then flip pages if legacy footer UI exists.

        New WeRead web UI often has no `button.readerFooter_button` (continuous
        scroll). In that case we only wait for Canvas capture and return.
        """
        await self.wait_for_canvas_complete(timeout=min(40, max(15, page_timeout)))

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
                await self._page.evaluate(
                    """() => {
                        canvasContextHandler.data.complete = false;
                        canvasContextHandler.data.markdown += '\\n\\n';
                    }"""
                )
                await self._page.click("button.readerFooter_button")
                ready = await self.wait_for_canvas_complete(
                    timeout=min(40, max(15, page_timeout))
                )
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
