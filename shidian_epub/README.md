# 识典古籍 → EPUB

通用的识典古籍文字版下载与 EPUB3 生成工具。

- 纯 `requests` + `BeautifulSoup`，不使用 Playwright / Chromium。
- 输入识典**任意正文章节 URL**，自动读取 loader 目录。
- 普通单书：自动下载整个 `catalog`。
- 合集型书籍：利用识典自己的 `chapterGroups + interval(volumeId)` 自动判断当前章节属于哪一部，只下载该分组。
- 支持断点缓存、失败重试、完整性报告和 EPUB 结构校验。
- 下载不完整时直接失败，不生成“看起来完整”的残本。

## 安装

```bash
pip install -r shidian_epub/requirements.txt
```

依赖只有：

```text
requests
beautifulsoup4
```

## 最简单的用法

把你正在阅读的识典章节 URL 直接交给脚本：

```bash
python shidian_epub/shidian_to_epub.py "https://www.shidianguji.com/book/LS0026/chapter/1k7geysfcta1x"
```

程序会自动识别书名/分组，并在当前目录生成：

```text
太祖高皇帝實錄.epub
太祖高皇帝實錄.report.json
```

也可以自行指定输出文件名：

```bash
python shidian_epub/shidian_to_epub.py \
  "https://www.shidianguji.com/book/LS0026/chapter/1k7geysfcta1x" \
  -o "明太祖实录.epub"
```

## 合集如何自动判断

以识典 `LS0026`《明實錄》为例，loader 的 `catalog` 包含整套明实录，但同时提供 `chapterGroups`。每个 group 都有：

```text
chapterName
interval = [起始 volumeId, 结束 volumeId]
```

脚本先把 `interval` 映射到 `catalog` 的实际顺序，再根据你提供的章节 `chapterId` 所在位置判断所属 group。

例如输入《明太祖实录》中的任意一章，会自动选中：

```text
太祖高皇帝實錄
```

而不会把后面的《太宗文皇帝實錄》《英宗睿皇帝實錄》等一起下载。

## 只检查识别结果，不下载正文

```bash
python shidian_epub/shidian_to_epub.py "章节URL" --dry-run
```

会生成 `.report.json`，可以先查看：

```text
book_name
selection_mode
group_index
group_name
selected_count
selected_first
selected_last
```

## 多分组合集的手工选择

通常给一个正文章节 URL 就能自动定位。如果只有合集主页、无法从 URL 判断具体分组，可以使用：

```bash
python shidian_epub/shidian_to_epub.py "URL" --group-index 2
```

或：

```bash
python shidian_epub/shidian_to_epub.py "URL" --group-name "太宗文皇帝實錄"
```

`--group-index` 从 1 开始。

## 高级兜底：手工指定起止章节

遇到识典目录结构特殊、`chapterGroups` 无法映射的书，可以明确指定：

```bash
python shidian_epub/shidian_to_epub.py "URL" \
  --from-chapter-id START_ID \
  --to-chapter-id END_ID
```

两个参数必须同时提供。

## 下载参数

默认参数偏保守，以减少 SSR 页面偶发返回空壳内容：

```text
workers      = 2
attempts     = 6
min-interval = 0.45 秒（全局请求最小间隔）
timeout      = 30 秒
```

可按需覆盖，例如：

```bash
python shidian_epub/shidian_to_epub.py "章节URL" \
  --workers 2 \
  --attempts 6 \
  --min-interval 0.45
```

每个线程使用独立 `requests.Session`，失败时会在普通 `/book/...` 与 `/zh/book/...` 页面之间交替重试。

## 断点续传

正文缓存默认保存到：

```text
.shidian_cache/<book_id>/
```

每个章节独立保存为 JSON。再次运行相同书籍时，已成功章节直接读取缓存，只补缺失章节。

在仓库的 GitHub Actions 测试中，也使用 cache 保留已成功章节。

## 完整性保护

下载结束前会检查：

1. `成功页数 == 自动选择的目录页数`
2. `failures == 0`
3. EPUB 是有效 ZIP/EPUB 容器
4. `mimetype` 位于 ZIP 第一项且不压缩
5. `container.xml / content.opf / nav.xhtml / toc.ncx` 均存在
6. EPUB 内 XHTML 章节数与下载页数一致

同时生成 `.report.json`，记录目录范围、字符数、失败页、重复正文、所有标题和 URL。

## 已验证案例

`LS0026`《明實錄》中的《太祖高皇帝實錄》：

```text
输入：任意《太祖高皇帝實錄》章节 URL
识别：chapterGroups 第 1 组
选择：263 页
成功：263 / 263
失败：0
重复：0
正文字符：1,173,073
首篇：太祖高皇帝實錄序
末篇：大明太祖高皇帝實錄卷之二百五十七
```

GitHub Actions 已通过完整下载、自动分组识别、EPUB 生成和最终校验。
