#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import sys
from typing import Any

import requests

import shidian_chain_to_epub as core


def count_chapters(obj: Any) -> tuple[int, list[str]]:
    ids: list[str] = []
    seen: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            cid = x.get("chapterId")
            if cid is not None:
                cid = str(cid)
                if cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return len(ids), ids


def brief(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        scalar = {}
        for k, v in obj.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                s = str(v)
                scalar[k] = s[:180]
        return {
            "type": "dict",
            "keys": list(obj.keys())[:80],
            "scalar": scalar,
            "chapter_count": count_chapters(obj)[0],
        }
    if isinstance(obj, list):
        return {
            "type": "list",
            "len": len(obj),
            "chapter_count": count_chapters(obj)[0],
        }
    return {"type": type(obj).__name__, "value": str(obj)[:180]}


def find_paths(root: Any, target_id: str):
    matches = []

    def walk(x: Any, path: list[Any], ancestors: list[Any]) -> None:
        if isinstance(x, dict):
            if str(x.get("chapterId", "")) == target_id:
                matches.append((path[:], ancestors[:] + [x]))
            for k, v in x.items():
                walk(v, path + [k], ancestors + [x])
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, path + [i], ancestors + [x])

    walk(root, [], [])
    return matches


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: probe_loader_structure.py <chapter-url> <chapter-id>")
        return 2

    url = core.norm_url(sys.argv[1]) + "?" + core.LOADER_QUERY
    target = sys.argv[2]
    r = core.make_session().get(url, timeout=30)
    r.raise_for_status()
    data = r.json()

    total, _ = count_chapters(data)
    print("loader_url:", url)
    print("total_unique_chapters:", total)

    matches = find_paths(data, target)
    print("matches:", len(matches))
    for mi, (path, ancestors) in enumerate(matches, 1):
        print("\n=== MATCH", mi, "===")
        print("path:", json.dumps(path, ensure_ascii=False))
        print("ancestor_count:", len(ancestors))
        for i, anc in enumerate(reversed(ancestors[-12:])):
            info = brief(anc)
            print(f"ancestor_minus_{i}:", json.dumps(info, ensure_ascii=False))

    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
