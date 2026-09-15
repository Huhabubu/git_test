#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

import shidian_chain_to_epub as old


@dataclass
class CatalogChapter:
    catalog_order: int
    chapter_id: str
    title: str
    volume_id: str
    has_main_content: bool


@dataclass
class Chapter:
    order: int
    chapter_id: str
    title: str
    volume_id: str = ""


@dataclass
class ChapterGroup:
    index: int
    name: str
    group_chapter_id: str
    start_volume_id: str
    end_volume_id: str
    start_catalog_index: Optional[int] = None
    end_catalog_index: Optional[int] = None


@dataclass
class Selection:
    mode: str
    chapters: list[Chapter]
    group: Optional[ChapterGroup]
    seed_chapter_id: str


_tls = threading.local()
_rate_lock = threading.Lock()
_last_request_at = 0.0


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def safe_filename(name: str, fallback: str = "识典古籍") -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip().strip(".")
    return s[:120] or fallback


def clean_name(raw: Any) -> str:
    try:
        name = old.chapter_name(raw)
    except Exception:
        name = ""
    return name.strip()


def fetch_loader(seed_url: str, timeout: float) -> tuple[dict, str]:
    clean_url = old.norm_url(seed_url)
    loader_url = clean_url + "?" + old.LOADER_QUERY
    r = old.make_session().get(loader_url, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or not isinstance(data.get("bookInfo"), dict):
        raise RuntimeError("loader JSON 中缺少 bookInfo")
    return data, loader_url


def flatten_catalog(book_info: dict) -> list[CatalogChapter]:
    """只遍历 bookInfo.catalog，避免把 chapterGroups 虚拟节点混入正文目录。"""
    catalog = book_info.get("catalog")
    if catalog is None:
        raise RuntimeError("bookInfo 中缺少 catalog")

    out: list[CatalogChapter] = []
    seen: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            cid = obj.get("chapterId")
            if cid is not None:
                cid = str(cid)
                if cid and cid not in seen:
                    seen.add(cid)
                    title = clean_name(obj.get("chapterName")) or f"章节 {cid}"
                    out.append(
                        CatalogChapter(
                            catalog_order=len(out) + 1,
                            chapter_id=cid,
                            title=title,
                            volume_id=str(obj.get("volumeId") or ""),
                            has_main_content=bool(obj.get("hasMainContent", True)),
                        )
                    )

            handled: set[str] = set()
            for key in ("chapters", "subChapters", "children", "items"):
                if key in obj:
                    handled.add(key)
                    walk(obj[key])

            for key, value in obj.items():
                if key in handled:
                    continue
                if isinstance(value, (dict, list)):
                    walk(value)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(catalog)
    if not out:
        raise RuntimeError("catalog 中没有发现 chapterId")
    return out


def parse_groups(book_info: dict, catalog: list[CatalogChapter]) -> list[ChapterGroup]:
    raw_groups = book_info.get("chapterGroups")
    if not isinstance(raw_groups, list):
        return []

    by_volume: dict[str, list[int]] = {}
    for i, ch in enumerate(catalog):
        if ch.volume_id:
            by_volume.setdefault(ch.volume_id, []).append(i)

    groups: list[ChapterGroup] = []
    for idx, raw in enumerate(raw_groups):
        if not isinstance(raw, dict):
            continue
        interval = raw.get("interval")
        if not isinstance(interval, list) or len(interval) < 2:
            continue

        start_vol = str(interval[0] or "")
        end_vol = str(interval[1] or "")
        group = ChapterGroup(
            index=idx,
            name=clean_name(raw.get("chapterName")) or f"分组 {idx + 1}",
            group_chapter_id=str(raw.get("chapterId") or ""),
            start_volume_id=start_vol,
            end_volume_id=end_vol,
        )

        starts = by_volume.get(start_vol, [])
        ends = by_volume.get(end_vol, [])
        if starts and ends:
            pairs = [(s, e) for s in starts for e in ends if s <= e]
            if pairs:
                group.start_catalog_index = min(s for s, _ in pairs)
                group.end_catalog_index = max(e for _, e in pairs)
        groups.append(group)

    return groups


def catalog_to_chapters(entries: list[CatalogChapter]) -> list[Chapter]:
    return [
        Chapter(i + 1, e.chapter_id, e.title, e.volume_id)
        for i, e in enumerate(entries)
    ]


def find_index(catalog: list[CatalogChapter], chapter_id: str) -> int:
    for i, ch in enumerate(catalog):
        if ch.chapter_id == chapter_id:
            return i
    raise ValueError(f"目录中找不到 chapterId: {chapter_id}")


def resolved_group_text(groups: list[ChapterGroup]) -> str:
    lines = []
    for g in groups:
        status = (
            f"catalog[{g.start_catalog_index}:{g.end_catalog_index}]"
            if g.start_catalog_index is not None and g.end_catalog_index is not None
            else "未解析区间"
        )
        lines.append(f"{g.index + 1}. {g.name} ({status})")
    return "\n".join(lines)


def select_chapters(
    catalog: list[CatalogChapter],
    groups: list[ChapterGroup],
    seed_chapter_id: str,
    from_id: str,
    to_id: str,
    group_index: Optional[int],
    group_name: str,
) -> Selection:
    if from_id or to_id:
        if not (from_id and to_id):
            raise ValueError("--from-chapter-id 与 --to-chapter-id 必须同时提供")
        a = find_index(catalog, from_id)
        b = find_index(catalog, to_id)
        if b < a:
            raise ValueError("to chapter 位于 from chapter 之前")
        return Selection("manual", catalog_to_chapters(catalog[a:b + 1]), None, seed_chapter_id)

    resolved = [
        g for g in groups
        if g.start_catalog_index is not None and g.end_catalog_index is not None
    ]

    if group_index is not None:
        if group_index < 1 or group_index > len(groups):
            raise ValueError(f"--group-index 范围应为 1..{len(groups)}")
        g = groups[group_index - 1]
        if g.start_catalog_index is None or g.end_catalog_index is None:
            raise RuntimeError(f"chapterGroup {group_index} 无法映射到 catalog: {g.name}")
        seg = catalog[g.start_catalog_index:g.end_catalog_index + 1]
        return Selection("chapter_group", catalog_to_chapters(seg), g, seed_chapter_id)

    if group_name:
        exact = [g for g in resolved if g.name == group_name]
        hits = exact or [g for g in resolved if group_name in g.name]
        if len(hits) != 1:
            raise ValueError(
                f"--group-name={group_name!r} 匹配到 {len(hits)} 个分组。\n"
                + resolved_group_text(groups)
            )
        g = hits[0]
        seg = catalog[g.start_catalog_index:g.end_catalog_index + 1]  # type: ignore[index]
        return Selection("chapter_group", catalog_to_chapters(seg), g, seed_chapter_id)

    if seed_chapter_id:
        seed_idx = find_index(catalog, seed_chapter_id)
        hits = [
            g for g in resolved
            if g.start_catalog_index <= seed_idx <= g.end_catalog_index  # type: ignore[operator]
        ]
        if len(hits) == 1:
            g = hits[0]
            seg = catalog[g.start_catalog_index:g.end_catalog_index + 1]  # type: ignore[index]
            return Selection("chapter_group", catalog_to_chapters(seg), g, seed_chapter_id)
        if len(hits) > 1:
            raise RuntimeError(
                f"输入章节同时落入 {len(hits)} 个 chapterGroup，无法安全自动选择"
            )

    if not groups:
        return Selection("whole_catalog", catalog_to_chapters(catalog), None, seed_chapter_id)

    if len(resolved) == 1:
        g = resolved[0]
        seg = catalog[g.start_catalog_index:g.end_catalog_index + 1]  # type: ignore[index]
        return Selection("chapter_group", catalog_to_chapters(seg), g, seed_chapter_id)

    raise RuntimeError(
        "这是一个包含多个 chapterGroup 的合集，但输入 URL 没有提供可定位的正文章节，"
        "无法判断你要哪一部。请粘贴该部任意章节 URL，或使用 --group-index / --group-name。\n"
        + resolved_group_text(groups)
    )


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, min_interval)

    def wait(self) -> None:
        global _last_request_at
        if self.min_interval <= 0:
            return
        with _rate_lock:
            now = time.monotonic()
            delay = self.min_interval - (now - _last_request_at)
            if delay > 0:
                time.sleep(delay)
            _last_request_at = time.monotonic()


def thread_session():
    if not hasattr(_tls, "session"):
        _tls.session = old.make_session()
    return _tls.session


def cache_path(cache_dir: Path, ch: Chapter) -> Path:
    return cache_dir / f"{ch.order:04d}_{ch.chapter_id}.json"


def load_cached(cache_dir: Path, ch: Chapter):
    p = cache_path(cache_dir, ch)
    if not p.exists():
        return None
    try:
        return old.Page(**json.loads(p.read_text("utf-8")))
    except Exception:
        return None


def save_cached(cache_dir: Path, ch: Chapter, page) -> None:
    p = cache_path(cache_dir, ch)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(page), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def save_debug(cache_dir: Path, ch: Chapter, body: str, suffix: str) -> None:
    d = cache_dir / "debug"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ch.order:04d}_{ch.chapter_id}_{suffix}.html").write_text(
        body, encoding="utf-8", errors="replace"
    )


def fetch_once(url: str, timeout: float, limiter: RateLimiter) -> str:
    limiter.wait()
    r = thread_session().get(url, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = "utf-8"
    return r.text


def download_one(
    bid: str,
    ch: Chapter,
    cache_dir: Path,
    timeout: float,
    attempts: int,
    limiter: RateLimiter,
):
    cached = load_cached(cache_dir, ch)
    if cached:
        return cached

    urls = [
        f"{old.BASE}/book/{bid}/chapter/{ch.chapter_id}",
        f"{old.BASE}/zh/book/{bid}/chapter/{ch.chapter_id}",
    ]
    last_error: Exception | None = None
    last_body = ""

    for attempt in range(1, max(1, attempts) + 1):
        url = urls[(attempt - 1) % len(urls)]
        try:
            body = fetch_once(url, timeout, limiter)
            last_body = body
            if len(body) < 100:
                raise RuntimeError(f"页面异常短: {len(body)} chars")
            page = old.parse_page(body, url, ch.order, ch.title)
            save_cached(cache_dir, ch, page)
            return page
        except Exception as e:
            last_error = e
            if attempt < attempts:
                time.sleep(min(0.7 * attempt, 4.0))

    if last_body:
        save_debug(cache_dir, ch, last_body, "parse_failed")
    raise RuntimeError(f"{ch.title} 下载/解析失败: {last_error}")


def download_all(
    bid: str,
    chapters: list[Chapter],
    cache_dir: Path,
    workers: int,
    timeout: float,
    attempts: int,
    min_interval: float,
):
    cache_dir.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(min_interval)
    results: dict[int, Any] = {}
    failures: list[dict] = []

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as ex:
        fmap = {
            ex.submit(download_one, bid, ch, cache_dir, timeout, attempts, limiter): ch
            for ch in chapters
        }
        total = len(chapters)
        done = 0
        for fut in as_completed(fmap):
            ch = fmap[fut]
            done += 1
            try:
                page = fut.result()
                results[ch.order] = page
                chars = sum(len(x) for x in page.paragraphs)
                log(f"✓ [{done}/{total}] #{ch.order} {page.title} ({chars:,} chars)")
            except Exception as e:
                failures.append({
                    "order": ch.order,
                    "chapter_id": ch.chapter_id,
                    "title": ch.title,
                    "error": repr(e),
                })
                log(f"× [{done}/{total}] #{ch.order} {ch.title}: {e}")

    pages = [results[i] for i in sorted(results)]
    failures.sort(key=lambda x: x["order"])
    return pages, failures


def duplicate_groups(pages: list[Any]) -> list[list[str]]:
    by_hash: dict[str, list[str]] = {}
    for p in pages:
        digest = getattr(p, "sha256", None) or getattr(p, "content_sha256", None)
        if not digest:
            continue
        by_hash.setdefault(str(digest), []).append(p.url)
    return [urls for urls in by_hash.values() if len(urls) > 1]


def validate_epub(path: Path, expected_chapters: int) -> None:
    if not path.exists() or not zipfile.is_zipfile(path):
        raise RuntimeError("EPUB 不存在或不是有效 ZIP 容器")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        required = {
            "mimetype",
            "META-INF/container.xml",
            "OEBPS/content.opf",
            "OEBPS/nav.xhtml",
            "OEBPS/toc.ncx",
        }
        missing = required - set(names)
        if missing:
            raise RuntimeError(f"EPUB 缺少必要文件: {sorted(missing)}")
        if names[0] != "mimetype":
            raise RuntimeError("EPUB mimetype 必须是 ZIP 第一项")
        if zf.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
            raise RuntimeError("EPUB mimetype 不应压缩")
        if zf.read("mimetype") != b"application/epub+zip":
            raise RuntimeError("EPUB mimetype 内容错误")
        chapters = [
            n for n in names
            if re.fullmatch(r"OEBPS/chapter_\d{4}\.xhtml", n)
        ]
        if len(chapters) != expected_chapters:
            raise RuntimeError(
                f"EPUB 章节数不一致: expected={expected_chapters} actual={len(chapters)}"
            )


def write_report(
    path: Path,
    bid: str,
    book_name: str,
    title: str,
    loader_url: str,
    catalog: list[CatalogChapter],
    groups: list[ChapterGroup],
    selection: Selection,
    pages: list[Any],
    failures: list[dict],
) -> dict:
    total_chars = sum(sum(len(x) for x in p.paragraphs) for p in pages)
    dup = duplicate_groups(pages)
    titles = [p.title for p in pages]
    group = selection.group

    report = {
        "book_id": bid,
        "book_name": book_name,
        "title": title,
        "loader_url": loader_url,
        "selection_mode": selection.mode,
        "seed_chapter_id": selection.seed_chapter_id,
        "catalog_count": len(catalog),
        "chapter_group_count": len(groups),
        "group_index": (group.index + 1) if group else None,
        "group_name": group.name if group else None,
        "group_interval": [group.start_volume_id, group.end_volume_id] if group else None,
        "selected_count": len(selection.chapters),
        "pages": len(pages),
        "total_chars": total_chars,
        "selected_first": asdict(selection.chapters[0]) if selection.chapters else None,
        "selected_last": asdict(selection.chapters[-1]) if selection.chapters else None,
        "failures": failures,
        "duplicates": dup,
        "has_volume_1": any("卷之一" in t for t in titles),
        "has_volume_257": any("卷之二百五十七" in t for t in titles),
        "groups": [asdict(g) for g in groups],
        "titles": titles,
        "urls": [p.url for p in pages],
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def parse_args():
    ap = argparse.ArgumentParser(
        description="识典古籍章节 URL -> 自动识别所属书/分组 -> EPUB"
    )
    ap.add_argument("url", help="识典书籍 URL 或任意正文章节 URL")
    ap.add_argument("-o", "--output", default="", help="EPUB 输出路径；省略则按识别出的书名命名")
    ap.add_argument("--title", default="", help="覆盖 EPUB 书名")
    ap.add_argument("--cache-dir", default=".shidian_cache")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--min-interval", type=float, default=0.45)
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--group-index", type=int, default=None, help="chapterGroups 中的分组序号，1-based")
    ap.add_argument("--group-name", default="", help="按识典 chapterGroup 名称选择")
    ap.add_argument("--from-chapter-id", default="", help="手工起点（高级兜底）")
    ap.add_argument("--to-chapter-id", default="", help="手工终点（高级兜底）")
    ap.add_argument("--dry-run", action="store_true", help="只识别目录范围，不下载正文")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    seed = old.norm_url(args.url)
    bid = old.book_id(seed)
    seed_cid = old.chapter_id(seed)

    data, loader_url = fetch_loader(seed, args.timeout)
    book_info = data["bookInfo"]
    book_name = str(book_info.get("bookName") or f"识典古籍_{bid}")
    catalog = flatten_catalog(book_info)
    groups = parse_groups(book_info, catalog)

    selection = select_chapters(
        catalog=catalog,
        groups=groups,
        seed_chapter_id=seed_cid,
        from_id=args.from_chapter_id,
        to_id=args.to_chapter_id,
        group_index=args.group_index,
        group_name=args.group_name,
    )

    inferred_title = selection.group.name if selection.group else book_name
    title = args.title or inferred_title
    output = Path(args.output) if args.output else Path(f"{safe_filename(title)}.epub")
    report_path = output.with_suffix(".report.json")

    log(f"book: {book_name} ({bid})")
    log(f"catalog: {len(catalog)} | chapterGroups: {len(groups)}")
    log(f"selection: {selection.mode} | {len(selection.chapters)} chapters")
    if selection.group:
        log(f"group: #{selection.group.index + 1} {selection.group.name}")
    if selection.chapters:
        log(f"首: {selection.chapters[0].chapter_id} | {selection.chapters[0].title}")
        log(f"尾: {selection.chapters[-1].chapter_id} | {selection.chapters[-1].title}")

    if args.dry_run:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        write_report(
            report_path, bid, book_name, title, loader_url,
            catalog, groups, selection, [], []
        )
        log(f"dry-run 完成: {report_path}")
        return 0

    cache_dir = Path(args.cache_dir) / bid
    pages, failures = download_all(
        bid=bid,
        chapters=selection.chapters,
        cache_dir=cache_dir,
        workers=args.workers,
        timeout=args.timeout,
        attempts=args.attempts,
        min_interval=args.min_interval,
    )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = write_report(
        report_path, bid, book_name, title, loader_url,
        catalog, groups, selection, pages, failures
    )

    if failures or len(pages) != len(selection.chapters):
        raise RuntimeError(
            f"下载不完整：selected={len(selection.chapters)} pages={len(pages)} "
            f"failures={len(failures)}；已写报告 {report_path}"
        )

    old.build_epub(output, title, bid, pages)
    validate_epub(output, len(pages))

    log(
        f"完成: selected={len(selection.chapters)} pages={len(pages)} "
        f"failures=0 chars={report['total_chars']:,} "
        f"epub={output.stat().st_size / 1024 / 1024:.2f} MiB"
    )
    log(f"EPUB: {output}")
    log(f"报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
