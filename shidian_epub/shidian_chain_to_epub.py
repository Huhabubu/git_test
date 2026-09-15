#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.shidianguji.com"
LOADER_QUERY = "page_from=home_page&__loader=__session%2F%28lang%24%29%2Fbook%2F%24&__ssrDirect=true"
BOOK_RE = re.compile(r"/book/([^/?#]+)")
CH_RE = re.compile(r"/book/([^/?#]+)/chapter/([^/?#]+)")
NEXT_LABELS = {"next", "下一篇", "下一页"}
_tls = threading.local()


@dataclass
class Chapter:
    order: int
    chapter_id: str
    title: str


@dataclass
class Page:
    index: int
    url: str
    chapter_id: str
    title: str
    paragraphs: list[str]
    sha256: str


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def norm_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}" if p.scheme else url


def book_id(url: str) -> str:
    m = BOOK_RE.search(url)
    if not m:
        raise ValueError(f"无法识别 book_id: {url}")
    return m.group(1)


def chapter_id(url: str) -> str:
    m = CH_RE.search(url)
    return m.group(2) if m else ""


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=0.8,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Referer": BASE + "/",
    })
    return s


def thread_session() -> requests.Session:
    if not hasattr(_tls, "session"):
        _tls.session = make_session()
    return _tls.session


def fetch_text(url: str, timeout: float = 30) -> str:
    r = thread_session().get(url, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = "utf-8"
    if len(r.text) < 100:
        raise RuntimeError(f"页面异常短: {url}")
    return r.text


def clean(s: str) -> str:
    return re.sub(r"[ \t\xa0]+", " ", s.replace("\u200b", "").replace("\ufeff", "")).strip()


def chapter_name(raw: Any) -> str:
    if isinstance(raw, str):
        return clean(raw)
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict) and item.get("content"):
                parts.append(str(item["content"]))
            elif isinstance(item, str):
                parts.append(item)
        return clean("".join(parts))
    if isinstance(raw, dict):
        return clean(str(raw.get("content", "")))
    return ""


def discover_catalog(seed_url: str, timeout: float = 30) -> tuple[list[Chapter], str]:
    clean_url = norm_url(seed_url)
    loader_url = clean_url + "?" + LOADER_QUERY
    r = make_session().get(loader_url, timeout=timeout)
    r.raise_for_status()
    data = r.json()

    found: list[Chapter] = []
    seen: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            cid = obj.get("chapterId")
            cname = obj.get("chapterName")
            if cid and cname:
                cid = str(cid)
                if cid not in seen:
                    title = chapter_name(cname) or f"章节 {cid}"
                    found.append(Chapter(len(found) + 1, cid, title))
                    seen.add(cid)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return found, loader_url


def title_of(soup: BeautifulSoup) -> str:
    h = soup.find("h2")
    if h:
        t = clean(h.get_text(" ", strip=True))
        if t:
            return t
    if soup.title and soup.title.string:
        return clean(soup.title.string).split("-")[0].strip()
    return "未命名章节"


def paragraphs_of(soup: BeautifulSoup, title: str) -> list[str]:
    h = soup.find("h2")
    if not h:
        return []

    out: list[str] = []
    seen: set[str] = set()
    ignore = {title, "All Books", "Log in for a better reading experience", "Next", "下一篇", "下一页"}

    for node in h.next_elements:
        if isinstance(node, Tag) and node.name == "a":
            if clean(node.get_text(" ", strip=True)).lower() in NEXT_LABELS:
                break
        if not isinstance(node, NavigableString):
            continue

        parent = node.parent if isinstance(node.parent, Tag) else None
        if parent and parent.name in {"script", "style", "noscript", "svg", "nav", "header", "footer", "button"}:
            continue

        a = parent.find_parent("a") if parent else None
        if a and clean(a.get_text(" ", strip=True)).lower() in NEXT_LABELS:
            break

        text = clean(str(node))
        if not text or text in ignore:
            continue
        if text not in seen:
            out.append(text)
            seen.add(text)

    return out


def parse_page(text: str, url: str, index: int, expected_title: str = "") -> Page:
    soup = BeautifulSoup(text, "html.parser")
    title = title_of(soup) or expected_title
    paragraphs = paragraphs_of(soup, title)
    if not paragraphs:
        raise RuntimeError(f"未提取到正文: {url}")
    digest = hashlib.sha256("\n".join(paragraphs).encode("utf-8")).hexdigest()
    return Page(index, norm_url(url), chapter_id(url), title, paragraphs, digest)


def cache_path(cache_dir: Path, ch: Chapter) -> Path:
    return cache_dir / f"{ch.order:04d}_{ch.chapter_id}.json"


def load_cached(cache_dir: Path, ch: Chapter) -> Optional[Page]:
    p = cache_path(cache_dir, ch)
    if not p.exists():
        return None
    try:
        return Page(**json.loads(p.read_text("utf-8")))
    except Exception:
        return None


def save_cached(cache_dir: Path, ch: Chapter, page: Page) -> None:
    p = cache_path(cache_dir, ch)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(page), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def download_one(bid: str, ch: Chapter, cache_dir: Path, timeout: float) -> Page:
    cached = load_cached(cache_dir, ch)
    if cached:
        return cached
    url = f"{BASE}/book/{bid}/chapter/{ch.chapter_id}"
    page = parse_page(fetch_text(url, timeout), url, ch.order, ch.title)
    save_cached(cache_dir, ch, page)
    return page


def download_catalog(bid: str, chapters: list[Chapter], cache_dir: Path, workers: int, timeout: float) -> tuple[list[Page], list[dict]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    results: dict[int, Page] = {}
    failures: list[dict] = []

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as ex:
        future_map = {
            ex.submit(download_one, bid, ch, cache_dir, timeout): ch
            for ch in chapters
        }
        done = 0
        total = len(chapters)
        for fut in as_completed(future_map):
            ch = future_map[fut]
            done += 1
            try:
                page = fut.result()
                results[ch.order] = page
                log(f"✓ [{done}/{total}] {page.title}")
            except Exception as e:
                failures.append({"order": ch.order, "chapter_id": ch.chapter_id, "title": ch.title, "error": repr(e)})
                log(f"× [{done}/{total}] {ch.title}: {e}")

    pages = [results[i] for i in sorted(results)]
    return pages, failures


def xhtml_doc(title: str, body: str) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN"><head><meta charset="utf-8"/><title>{html.escape(title)}</title><link rel="stylesheet" href="style.css" type="text/css"/></head><body>{body}</body></html>'''


def build_epub(output: Path, title: str, bid: str, pages: list[Page]) -> None:
    if not pages:
        raise RuntimeError("没有正文，无法生成 EPUB")

    uid = f"urn:uuid:{uuid.uuid4()}"
    files: dict[str, bytes] = {}
    manifest: list[str] = []
    spine: list[str] = []
    nav: list[str] = []
    ncx: list[str] = []

    files["OEBPS/title.xhtml"] = xhtml_doc(title, f"<h1>{html.escape(title)}</h1><p class='meta'>识典古籍文字重排版</p>").encode()
    manifest.append('<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>')
    spine.append('<itemref idref="title"/>')

    for i, page in enumerate(pages, 1):
        fn = f"chapter_{i:04d}.xhtml"
        body = "<h1>" + html.escape(page.title) + "</h1>" + "".join(f"<p>{html.escape(p)}</p>" for p in page.paragraphs)
        files["OEBPS/" + fn] = xhtml_doc(page.title, body).encode()
        manifest.append(f'<item id="c{i}" href="{fn}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
        nav.append(f'<li><a href="{fn}">{html.escape(page.title)}</a></li>')
        ncx.append(f'<navPoint id="n{i}" playOrder="{i+1}"><navLabel><text>{html.escape(page.title)}</text></navLabel><content src="{fn}"/></navPoint>')

    files["OEBPS/nav.xhtml"] = xhtml_doc("目录", '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><h1>目录</h1><ol>' + ''.join(nav) + '</ol></nav>').encode()
    files["OEBPS/style.css"] = b'body{font-family:serif;line-height:1.75;margin:5%;text-align:justify}h1{text-align:center;font-size:1.35em}p{text-indent:2em;margin:.32em 0}.meta{text-align:center;text-indent:0}'
    files["OEBPS/content.opf"] = (f'''<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">{uid}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>zh-CN</dc:language><dc:source>{BASE}/book/{bid}</dc:source><meta property="dcterms:modified">{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="style.css" media-type="text/css"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>{''.join(manifest)}</manifest><spine toc="ncx">{''.join(spine)}</spine></package>''').encode()
    files["OEBPS/toc.ncx"] = (f'''<?xml version="1.0" encoding="UTF-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="{uid}"/><meta name="dtb:depth" content="1"/></head><docTitle><text>{html.escape(title)}</text></docTitle><navMap><navPoint id="nt" playOrder="1"><navLabel><text>{html.escape(title)}</text></navLabel><content src="title.xhtml"/></navPoint>{''.join(ncx)}</navMap></ncx>''').encode()
    files["META-INF/container.xml"] = b'<?xml version="1.0" encoding="UTF-8"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in files.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)


def main() -> int:
    ap = argparse.ArgumentParser(description="识典古籍 loader 目录 + 并发章节下载 -> EPUB")
    ap.add_argument("url", help="任意识典章节 URL；可带 __loader 参数")
    ap.add_argument("-o", "--output", default="识典古籍.epub")
    ap.add_argument("--title", default="识典古籍")
    ap.add_argument("--cache-dir", default=".shidian_cache")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=30)
    args = ap.parse_args()

    seed = norm_url(args.url)
    bid = book_id(seed)
    cache_dir = Path(args.cache_dir) / bid

    chapters, loader_url = discover_catalog(seed, args.timeout)
    log(f"loader: {loader_url}")
    log(f"发现章节节点: {len(chapters)}")
    if not chapters:
        raise RuntimeError("loader JSON 中没有发现 chapterId/chapterName")

    pages, failures = download_catalog(bid, chapters, cache_dir, args.workers, args.timeout)
    hashes: dict[str, str] = {}
    duplicates: list[dict] = []
    for p in pages:
        if p.sha256 in hashes:
            duplicates.append({"title": p.title, "url": p.url, "same_as": hashes[p.sha256]})
        else:
            hashes[p.sha256] = p.url

    output = Path(args.output)
    build_epub(output, args.title, bid, pages)

    report = {
        "book_id": bid,
        "title": args.title,
        "catalog_count": len(chapters),
        "pages": len(pages),
        "failures": failures,
        "duplicates": duplicates,
        "has_volume_1": any("卷之一" in p.title for p in pages),
        "has_volume_257": any("卷之二百五十七" in p.title for p in pages),
        "first_titles": [p.title for p in pages[:10]],
        "last_titles": [p.title for p in pages[-10:]],
    }
    output.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"完成: 目录 {len(chapters)} / 成功 {len(pages)} / 失败 {len(failures)}")
    log(f"EPUB: {output} ({output.stat().st_size / 1024 / 1024:.2f} MiB)")
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
