# 微信读书导出工具

将微信读书网页版（weread.qq.com）的书籍正文导出为 markdown / epub / pdf / mobi / txt 格式。

## 实现原理

微信读书网页版的正文是以 Canvas 绘制（而非 DOM 文本）渲染的。本工具通过以下方式还原正文：

1. 使用 pyppeteer 驱动 Chromium 打开阅读器页面；
2. 拦截阅读器的网络请求，将阅读器脚本 `https://cdn.weread.qq.com/web/1.392ec47a.js` 替换为内置的 `hook.js`，其余静态资源走本地缓存，统计 / 上报类接口直接 mock 返回；
3. `hook.js` 通过 Proxy 劫持 `CanvasRenderingContext2D`，从 `fillText`、字体与颜色设置等调用中捕获文本及样式信息，按字号推断标题层级、按颜色识别高亮，并按坐标回填代码块 / 图片 / 分隔线，拼装成 markdown；
4. 逐章保存 markdown，再据此转换成 epub / pdf / txt 等最终格式；mobi 格式由 kindlegen 从 epub 转换而来。

## 安装

**必须装进虚拟环境，不要装进全局环境。** 这不是洁癖，是硬需求：

pyppeteer 2.0.0 是该库最后一个版本，它声明依赖 `websockets >=10.0,<11.0`。
而同一台机器上的其他项目要求相反方向——`codex-app-server-sdk` 要 `websockets>=16`，
`yfinance` 要 `>=13`。这两组约束在同一个 site-packages 里**无法共存**。实际发生过
的就是：全局环境里 websockets 被顶到 16.1.1，pyppeteer 的 CDP 拆除路径因此抛
`InvalidStateError` 并让导出永久卡死（详见 `weread_exporter/__main__.py` 的
`patch_connection_close`）。`requirements.txt` 已把这类包显式钉住——**不要放开成范围**。

```powershell
# Windows（推荐）
.\scripts\bootstrap.ps1

# 或手动
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
# macOS / Linux
./scripts/bootstrap.sh
# 或：python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
```

在仓库根目录用 `.venv` 跑 `-m weread_exporter` 即可（不必 `pip install -e .`）。

> 需要 Python 3.7+（实测 3.12.10）。PDF 导出依赖 WeasyPrint；Windows 下所需的 GTK 动态库已随包附带（`weread_exporter/bin/win32/`）。
>
> 提醒：Windows 的 venv 里 `.venv/Scripts/python.exe` 是个启动器，它会再拉起一个基解释器子进程——**任务管理器里看到两个 python 是正常的**，不是并发。

## 使用

**必须用本仓 `.venv` 里的解释器**（系统 `python` / 全局 `weread-exporter` 会被入口守卫拒绝）。在仓库根目录执行：

```powershell
# Windows：导出 markdown + PDF（成品默认在 .\output\）
.\.venv\Scripts\python.exe -m weread_exporter -b <book_id> -o md -o pdf

# 指定成品目录
.\.venv\Scripts\python.exe -m weread_exporter -b <book_id> -o md -o pdf --output-dir Z:\share\books

# 例
.\.venv\Scripts\python.exe -m weread_exporter -b 6da323305d18b86da0a7795 -o md -o pdf
```

```bash
# macOS / Linux
.venv/bin/python -m weread_exporter -b <book_id> -o md -o pdf
.venv/bin/python -m weread_exporter -b <book_id> -o md -o pdf --output-dir /path/to/books
```

说明：

- 每种格式单独写一次 `-o`（不要写成 `-o md pdf`）。
- 章节原文仍在仓库 `cache/<book_id>/chapters/`；成品目录默认 `output/`，可用 `--output-dir` 改（如 `Z:\share\books`）。
- 运行日志默认写到 `logs/<book_id>.run.log`（UTF-8）；浏览器控制台在 `logs/<book_id>.log`。
- 启动一本书时，先看 `<输出目录>/<书名>/export_summary.json` 决定做什么：没有该文件或 `complete` 不为 true → 完整下载；已完整但缺少所要求的格式（看 `formats`）→ 不开浏览器，直接用 `cache/` 生成缺的格式（`cache/` 章节不完好则回退为完整下载）；已完整且格式齐全 → 直接跳过。加 `--force` 可忽略并重新下载。**下载不完整时不导出任何格式**（也不写 `meta.json`），只在书目录写 `export_summary.json` 记录缺口，重跑即可续抓缺失章节。
- 每本导完会自动校验 `cache/` 完整性并打印结论；跳过校验加 `--no-check`。
- 单独复查：`.\.venv\Scripts\python.exe -m weread_exporter.check_book <book_id>`
- 运行中若 cookie 失效会打 ERROR 并以退出码 `2` 结束（**不会**再等扫码）：启动时用户接口验失败、页脚「登录」、或 canvas 一直不出时探针到登录按钮/用户接口拒绝（不扫正文，避免书中「未登录」误杀）。普通章节抓取失败仍走重试。更新 `cache/cookie.txt` 或加 `--force-login` 后重跑即可续抓。

首次使用若还没有 `.venv`，先跑一次：

```powershell
.\scripts\bootstrap.ps1          # Windows
# ./scripts/bootstrap.sh         # macOS / Linux
```

### 获取书籍 ID

在 `https://weread.qq.com/` 搜索目标书籍，进入书籍介绍页，URL 形如：

```
https://weread.qq.com/web/bookDetail/08232ac0720befa90825d88
```

其中末尾的 `08232ac0720befa90825d88` 即书籍 ID。

> 如果 ID 中包含下划线 `_`，会被当作**书单 ID** 处理，从 `https://weread.qq.com/misc/booklist/<id>` 展开为书单内的全部书籍并逐本导出。

### 输出格式

通过 `-o / --output-format` 指定，可重复多次以同时导出多种格式。取值：`md`、`epub`、`pdf`、`mobi`、`txt`，默认 `epub`。

每本书输出到 `<输出目录>/<书名>/`：

```
<书名>/
├── export_summary.json   # 整本书的导出状态（status / complete / unresolved / skipped …）
├── meta.json             # 书名、作者、章节目录（无 cover URL）
├── <书名>.pdf            # -o pdf
├── <书名>.epub / .mobi / .txt   # 对应格式
└── markdown/             # -o md
    ├── chapters/
    ├── images/
    └── cover.jpg
```

| 格式 | 产物 | 说明 |
| --- | --- | --- |
| `md` | `<书名>/markdown/` | 含 `chapters/`、`images/`、`cover.jpg` |
| `epub` | `<书名>/<书名>.epub` | 适合手机端访问 |
| `pdf` | `<书名>/<书名>.pdf` | 适合电脑端访问 |
| `mobi` | `<书名>/<书名>.mobi` | 适合 Kindle 访问；**仅 Linux 支持**（依赖内置 kindlegen），会同时生成 epub |
| `txt` | `<书名>/<书名>.txt` | 纯文本 |

`meta.json` 与 `export_summary.json` 无论选哪种格式都会写入书目录。`export_summary.json` 是单一事实来源：`formats` 记录已导出的格式，`status`（`OK` / `INCOMPLETE`）与 `complete` 表示是否完整，`problems` 列出缺口；批量脚本只读它来判断“是否已导出 / 是否完整 / 有哪些格式”，不再探测文件。成品默认写在当前目录下的 `output/`，可用 `--output-dir` 指定。若目标文件已存在，则跳过该格式（不覆盖已有文件）。

### 命令行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-b, --book-id` | 必填 | 书籍 ID（或含 `_` 的书单 ID） |
| `-o, --output-format` | `epub` | 输出格式，可重复指定 |
| `--output-dir` | `output` | 成品（md/epub/pdf 等）输出目录 |
| `--load-timeout` | `60` | 单章页面加载超时（秒） |
| `--load-interval` | `20` | 章与章之间的平均间隔（秒），实际每次在其 ±40% 内随机（默认 12~28 秒），用于降低触发风控的风险；`0` 表示不等待 |
| `--css-file` | 无 | 自定义 CSS 文件路径，用于覆盖默认样式 |
| `--headless` | 关闭 | 以无头模式运行 Chrome |
| `--force-login` | 关闭 | 强制先执行登录（扫码） |
| `--use-default-profile` | 关闭 | 使用 Chrome 默认用户目录 |
| `--mock-user-agent` | 关闭 | 使用随机生成的 User-Agent |
| `--proxy-server` | 无 | HTTP 代理，例如 `http://127.0.0.1:8888` |
| `--system-chrome` | 关闭 | 使用系统安装的 Chrome 替代内置 Chromium |
| `--compress-pdf-images` | 关闭 | 压缩 PDF 内嵌图片（降采样 + 调色板），另存为 `<书名>-compressed.pdf`，不覆盖原图与高清 PDF |
| `--pdf-image-max-side` | `1000` | 配合上一个参数：图片长边上限（像素，0 表示不缩放） |
| `--pdf-image-colors` | `256` | 配合上一个参数：调色板颜色数（0 表示保留真彩） |
| `--log-file` | `logs/<book-id>.run.log` | UTF-8 运行日志路径 |
| `--force` | 关闭 | 忽略 `export_summary.json`，强制完整重新下载 |
| `--no-check` | 关闭 | 跳过导完后的 cache 完整性校验 |

### 批量导出整个类目

`scripts/batch_category.py` 按类目书单一本一本地导出（**不并发**），每本产出 md + pdf，图片不压缩。

```powershell
# 类目 1100001（财经）→ Z:\share\books
.\.venv\Scripts\python.exe scripts\batch_category.py --category 1100001 --output-dir Z:\share\books

# 只看清单、不动文件
.\.venv\Scripts\python.exe scripts\batch_category.py --list-only

# 先跑 3 本验证
.\.venv\Scripts\python.exe scripts\batch_category.py --limit 3
```

- 书单来自 `https://weread.qq.com/web/bookListInCategory/<类目ID>?maxIndex=N`，每页 20 本。接口的 `totalCount` 是该类目下的全部藏书数（财经类 13873），但**列表只开放到有限深度**（实测 1100001 停在约 500 本），脚本以实际能翻到的为准。
- 书单里的 `bookId` 是数字 ID，导出要用的是 `readerLink` 末尾的编码 ID（如 `95332ad0813ab8705g016ce7`）——脚本自动转换。
- **可续跑、无自有状态**：脚本只调用导出器，是否已导出/是否完整/缺哪些格式全看 `<成品目录>/<书名>/export_summary.json`；已完整且 md+pdf 齐全的书直接跳过，重跑同一条命令就从上次停下的地方继续。`--list-only` 会列出每本当前的处理方式（download / formats / skip）。
- 每本导完打印一行结论（以 export_summary 的 status 为准）；完整记录在 `logs/batch-category-<类目ID>.run.log`。
- cookie 失效时整批停止（退出码 `2`），不会一本一本空转；更新 `cache/cookie.txt` 后重跑即可。
- 连续 3 本失败会自动停手，避免整批空转（`--max-consecutive-failures`）。
- 单本总超时默认 4 小时（`--book-timeout`），单本失败默认重试 1 次（`--retries`）。



默认按原图分辨率嵌入，扫描类/漫画类书籍会产出很大的 PDF（实测一本 197 页漫画约 102 MB）。
加 `--compress-pdf-images` 会生成一份压缩版：图片另存到 `cache/<book_id>/pdf-images-<长边>-<颜色数>/`，
原图与不带该参数导出的 PDF 都不受影响，可随时对照取舍。

```bash
# 同一本书：高清版 + 约 20 MB 的压缩版
$ python -m weread_exporter -b <book_id> -o pdf
$ python -m weread_exporter -b <book_id> -o pdf --compress-pdf-images --pdf-image-max-side 600
```

> 该平台（Windows）自带的图片库只有 PNG 解码器，PDF 图片只能以 PNG 形式嵌入，
> 因此这里用"降采样 + 调色板"而不是转 JPEG。实测 197 页漫画：原版 102 MB，
> `--pdf-image-max-side 600` 约 19.6 MB，正文与插图仍清晰可读。

### 浏览器选择

默认使用 pyppeteer **自带的 Chromium**（首次运行会自动下载）。新版系统 Chrome 会破坏 pyppeteer 的请求拦截，因此不再默认使用；如需改用系统 Chrome，请显式传入 `--system-chrome`。

### 登录

登录状态缓存于 `cache/cookie.txt`。首次运行（或传入 `--force-login`）时会打开登录页，请在 300 秒内完成扫码登录；登录成功后 cookie 会自动保存，后续导出直接复用。

## 免责申明

本工具仅作技术研究之用，请勿用于商业或违法用途，由于使用该工具导致的侵权或其它问题，该本工具不承担任何责任！
