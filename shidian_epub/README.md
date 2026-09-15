# 识典古籍 -> EPUB

纯 `requests` + `BeautifulSoup` 版本，不使用 Playwright。

测试目标：`LS0026`《明太祖实录》。脚本沿章节页中的 `Next` 链顺序抓取正文，生成 EPUB3 和 JSON 报告。
