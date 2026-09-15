#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""稳健区间下载器：复用原 EPUB 生成器，只修正目录范围与软限流重试。"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import requests
import shidian_chain_to_epub as core

_rate_lock = threading.Lock()
_last_request = 0.0
_min_interval = 0.45
_tls = threading.local()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def rate_wait() -> None:
    global _last_request
    if _min_interval <= 0:
        return
    with _rate_lock:
        delay = _min_interval - (time.monotonic() - _last_request)
        if delay > 0:
            time.sleep(delay)
        _last_request = time.monotonic()


def get_session() -> requests.Session:
    if not hasattr(_tls, "session"):
        _tls.session = core.make_session()
    return _tls.session


def select_range(chapters, first_id: str | None, last_id: str | None):
    start, end = 0, len(chapters) - 1
    if first_id:
        found = [i for i, ch in enumerate(chapters) if ch.chapter_id == first_id]
        if not found:
            raise RuntimeError(f"目录中找不到起始 chapter_id={first_id}")
        start = found[0]
    if last_id:
        found = [i for i, ch in enumerate(chapters) if ch.chapter_id == last_id]
        if not found:
            raise RuntimeError(f"目录中找不到结束 chapter_id={last_id}")
        end = found[0]
    if end < start:
        raise RuntimeError("目录区间反向")
    selected = chapters[start : end + 1]
    return [core.Chapter(i + 1, ch.chapter_id, ch.title) for i, ch in enumerate(selected)]


def save_debug(cache_dir: Path, ch, attempt: int, text: str) -> None:
    d = cache_dir / "debug"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ch.order:04d}_{ch.chapter_id}_a{attempt}.html").write_text(
        text[:400_000], encoding="utf-8", errors="replace"
    )


def download_one(bid: str, ch, cache_dir: Path, timeout: float, attempts: int):
    cached = core.load_cached(cache_dir, ch)
    if cached:
        return cached

    urls = [
        f"{core.BASE}/book/{bid}/chapter/{ch.chapter_id}",
        f"{core.BASE}/zh/book/{bid}/chapter/{ch.chapter_id}",
    ]
    last_error = None

    for attempt in range(1, attempts + 1):
        url = urls[(attempt - 1) % len(urls)]
        text = ""
        try:
            rate_wait()
            r = get_session().get(
                url,
                timeout=timeout,
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            )
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                r.encoding = "utf-8"
            text = r.text
            page = core.parse_page(text, url, ch.order, ch.title)
            core.save_cached(cache_dir, ch, page)
            return page
        except Exception as exc:
            last_error = exc
            if text and attempt >= max(1, attempts - 1):
                try:
                    save_debug(cache_dir, ch, attempt, text)
                except Exception:
                    pass
            if attempt < attempts:
                time.sleep(min(8.0, 0.8 * (2 ** (attempt - 1))))

    raise RuntimeError(
        f"{ch.title} ({ch.chapter_id}) after {attempts} attempts: {last_error!r}"
    )


def download_all(bid: str, chapters, cache_dir: Path, workers: int, timeout: float, attempts: int):
    cache_dir.mkdir(parents=True, exist_ok=True)
    results, failures = {}, []

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        futures = {
            pool.submit(download_one, bid, ch, cache_dir, timeout, attempts): ch
            for ch in chapters
        }
        done = 0
        for future in as_completed(futures):
            ch = futures[future]
            done += 1
            try:
                page = future.result()
                results[ch.order] = page
                chars = sum(len(x) for x in page.paragraphs)
                log(f"✓ [{done}/{len(chapters)}] #{ch.order} {page.title} ({chars:,} chars)")
            except Exception as exc:
                failures.append(
                    {
                        "order": ch.order,
                        "chapter_id": ch.chapter_id,
                        "title": ch.title,
                        "error": repr(exc),
                    }
                )
                log(f"× [{done}/{len(chapters)}] #{ch.order} {ch.title}: {exc}")

    pages = [results[i] for i in sorted(results)]
    failures.sort(key=lambda x: x["order"])
    return pages, failures


def write_report(path: Path, bid: str, title: str, catalog_total: int, selected, pages, failures):
    hashes = {}
    for p in pages:
        hashes.setdefault(p.sha256, []).append(p.title)
    titles = [p.title for p in pages]
    report = {
        "book_id": bid,
        "title": title,
        "catalog_total": catalog_total,
        "selected_count": len(selected),
        "selected_first": asdict(selected[0]) if selected else None,
        "selected_last": asdict(selected[-1]) if selected else None,
        "pages": len(pages),
        "total_chars": sum(sum(len(x) for x in p.paragraphs) for p in pages),
        "failures": failures,
        "duplicates": [x for x in hashes.values() if len(x) > 1],
        "has_volume_1": any("卷之一" in t for t in titles),
        "has_volume_257": any("二百五十七" in t for t in titles),
        "first_titles": titles[:10],
        "last_titles": titles[-10:],
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    p = argparse.ArgumentParser(description="识典 loader 章节区间 -> 稳健并发下载 -> EPUB")
    p.add_argument("url")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--cache-dir", default=".shidian_cache")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--attempts", type=int, default=6)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--min-interval", type=float, default=0.45)
    p.add_argument("--from-chapter-id")
    p.add_argument("--to-chapter-id")
    args = p.parse_args()

    global _min_interval
    _min_interval = max(0.0, args.min_interval)

    seed = core.norm_url(args.url)
    bid = core.book_id(seed)
    cache_dir = Path(args.cache_dir) / bid

    catalog, loader_url = core.discover_catalog(seed, args.timeout)
    log(f"loader: {loader_url}")
    log(f"loader 全部 chapter 节点: {len(catalog)}")
    selected = select_range(catalog, args.from_chapter_id, args.to_chapter_id)
    if not selected:
        raise RuntimeError("选中章节为空")
    log(f"本次选中: {len(selected)}")
    log(f"首: {selected[0].chapter_id} | {selected[0].title}")
    log(f"尾: {selected[-1].chapter_id} | {selected[-1].title}")

    pages, failures = download_all(
        bid, selected, cache_dir, args.workers, args.timeout, args.attempts
    )

    output = Path(args.output)
    core.build_epub(output, args.title, bid, pages)
    report = write_report(
        output.with_suffix(".report.json"),
        bid,
        args.title,
        len(catalog),
        selected,
        pages,
        failures,
    )
    log(
        f"完成: selected={report['selected_count']} pages={report['pages']} "
        f"failures={len(failures)} chars={report['total_chars']:,} "
        f"epub={output.stat().st_size / 1024 / 1024:.2f} MiB"
    )
    return 2 if failures or len(pages) != len(selected) else 0


if __name__ == "__main__":
    raise SystemExit(main())
