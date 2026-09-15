#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, hashlib, html, json, re, time, uuid, zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.shidianguji.com"
BOOK_RE = re.compile(r"/book/([^/?#]+)")
CH_RE = re.compile(r"/book/([^/?#]+)/chapter/([^/?#]+)")

@dataclass
class Page:
    index: int
    url: str
    chapter_id: str
    title: str
    paragraphs: list[str]
    next_url: Optional[str]
    sha256: str

def log(s):
    print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

def norm_url(u):
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}{p.path}" if p.scheme else u

def book_id(u):
    m = BOOK_RE.search(u)
    if not m: raise ValueError(f"无法识别 book_id: {u}")
    return m.group(1)

def chapter_id(u):
    m = CH_RE.search(u)
    return m.group(2) if m else ""

def session():
    s = requests.Session()
    r = Retry(total=4, connect=4, read=4, status=4, backoff_factor=.8,
              status_forcelist=(408,429,500,502,503,504),
              allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=r, pool_connections=2, pool_maxsize=2))
    s.headers.update({
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding":"gzip, deflate",
        "Accept-Language":"zh-CN,zh;q=0.9,en;q=0.5",
        "Referer": BASE + "/",
    })
    return s

def fetch(s,u,timeout):
    r=s.get(u,timeout=timeout); r.raise_for_status()
    if not r.encoding or r.encoding.lower() in ("iso-8859-1","ascii"): r.encoding="utf-8"
    if len(r.text)<100: raise RuntimeError(f"页面异常短: {u}")
    return r.text

def clean(x):
    return re.sub(r"[ \t\xa0]+"," ",x.replace("\u200b","").replace("\ufeff","")).strip()

def find_next(soup,cur):
    for a in soup.find_all("a",href=True):
        vals=[clean(a.get_text(" ",strip=True)),clean(a.get("aria-label","") or ""),clean(a.get("title","") or "")]
        if any(v.lower()=="next" for v in vals): return norm_url(urljoin(cur,a["href"]))
    return None

def title_of(soup):
    h=soup.find("h2")
    if h and clean(h.get_text(" ",strip=True)): return clean(h.get_text(" ",strip=True))
    if soup.title and soup.title.string: return clean(soup.title.string).split("-")[0].strip()
    return "未命名章节"

def paras_of(soup,title):
    h=soup.find("h2")
    if not h: return []
    out=[]; seen=set()
    for n in h.next_elements:
        if isinstance(n,Tag) and n.name=="a" and clean(n.get_text(" ",strip=True)).lower()=="next": break
        if not isinstance(n,NavigableString): continue
        p=n.parent if isinstance(n.parent,Tag) else None
        if p and p.name in {"script","style","noscript","svg","nav","header","footer","button"}: continue
        a=p.find_parent("a") if p else None
        if a and clean(a.get_text(" ",strip=True)).lower()=="next": break
        x=clean(str(n))
        if not x or x in {title,"All Books","Next","Log in for a better reading experience"}: continue
        if x not in seen: out.append(x); seen.add(x)
    return out

def parse(text,url,index):
    soup=BeautifulSoup(text,"html.parser"); t=title_of(soup); ps=paras_of(soup,t)
    if not ps: raise RuntimeError(f"未提取到正文: {url}")
    return Page(index,norm_url(url),chapter_id(url),t,ps,find_next(soup,url),hashlib.sha256("\n".join(ps).encode()).hexdigest())

def save(cache,p):
    (cache/f"{p.index:04d}_{p.chapter_id or 'page'}.json").write_text(json.dumps(asdict(p),ensure_ascii=False,indent=2),encoding="utf-8")

def load(cache):
    out=[]
    for f in sorted(cache.glob("[0-9][0-9][0-9][0-9]_*.json")):
        try: out.append(Page(**json.loads(f.read_text("utf-8"))))
        except Exception: pass
    return sorted(out,key=lambda x:x.index)

def crawl(seed,cache,delay,timeout,max_pages):
    s=session(); seed=norm_url(seed); bid=book_id(seed); cache.mkdir(parents=True,exist_ok=True)
    pages=load(cache); seen={p.url for p in pages}; hashes={p.sha256:p.url for p in pages}; warns=[]
    cur=pages[-1].next_url if pages else seed
    if pages: log(f"断点续传: 已有 {len(pages)} 页")
    while cur:
        cur=norm_url(cur)
        if cur in seen: warns.append(f"Next循环: {cur}"); break
        if book_id(cur)!=bid: warns.append(f"跳出当前书: {cur}"); break
        if len(pages)>=max_pages: warns.append(f"达到max_pages={max_pages}"); break
        i=len(pages)+1; log(f"GET [{i}] {cur}")
        p=parse(fetch(s,cur,timeout),cur,i)
        if p.sha256 in hashes: warns.append(f"重复正文: {p.title} 与 {hashes[p.sha256]} 相同")
        hashes[p.sha256]=p.url; pages.append(p); seen.add(p.url); save(cache,p)
        log(f"  ✓ {p.title} | {sum(map(len,p.paragraphs)):,} 字符 | Next={'有' if p.next_url else '无'}")
        cur=p.next_url
        if cur and delay: time.sleep(delay)
    return bid,pages,warns

def xhtml(title,body):
    return f'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN"><head><meta charset="utf-8"/><title>{html.escape(title)}</title><link rel="stylesheet" href="style.css" type="text/css"/></head><body>{body}</body></html>'''

def make_epub(out,title,bid,pages):
    uid=f"urn:uuid:{uuid.uuid4()}"; files={}; man=[]; spine=[]; nav=[]; ncx=[]
    files["OEBPS/title.xhtml"]=xhtml(title,f"<h1>{html.escape(title)}</h1><p class='meta'>识典古籍文字重排版</p>").encode()
    man.append('<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>'); spine.append('<itemref idref="title"/>')
    for i,p in enumerate(pages,1):
        fn=f"chapter_{i:04d}.xhtml"; body="<h1>"+html.escape(p.title)+"</h1>"+"".join(f"<p>{html.escape(x)}</p>" for x in p.paragraphs)
        files["OEBPS/"+fn]=xhtml(p.title,body).encode(); man.append(f'<item id="c{i}" href="{fn}" media-type="application/xhtml+xml"/>'); spine.append(f'<itemref idref="c{i}"/>'); nav.append(f'<li><a href="{fn}">{html.escape(p.title)}</a></li>'); ncx.append(f'<navPoint id="n{i}" playOrder="{i+1}"><navLabel><text>{html.escape(p.title)}</text></navLabel><content src="{fn}"/></navPoint>')
    files["OEBPS/nav.xhtml"]=xhtml("目录",'<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><h1>目录</h1><ol>'+''.join(nav)+'</ol></nav>').encode()
    files["OEBPS/style.css"]=b'body{font-family:serif;line-height:1.75;margin:5%;text-align:justify}h1{text-align:center;font-size:1.35em}p{text-indent:2em;margin:.32em 0}.meta{text-align:center;text-indent:0}'
    files["OEBPS/content.opf"]=(f'''<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">{uid}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>zh-CN</dc:language><dc:source>{BASE}/book/{bid}</dc:source><meta property="dcterms:modified">{time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="style.css" media-type="text/css"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>{''.join(man)}</manifest><spine toc="ncx">{''.join(spine)}</spine></package>''').encode()
    files["OEBPS/toc.ncx"]=(f'''<?xml version="1.0" encoding="UTF-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="{uid}"/><meta name="dtb:depth" content="1"/></head><docTitle><text>{html.escape(title)}</text></docTitle><navMap><navPoint id="nt" playOrder="1"><navLabel><text>{html.escape(title)}</text></navLabel><content src="title.xhtml"/></navPoint>{''.join(ncx)}</navMap></ncx>''').encode()
    files["META-INF/container.xml"]=b'<?xml version="1.0" encoding="UTF-8"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    with zipfile.ZipFile(out,"w") as z:
        z.writestr("mimetype",b"application/epub+zip",compress_type=zipfile.ZIP_STORED)
        for n,d in files.items(): z.writestr(n,d,compress_type=zipfile.ZIP_DEFLATED)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("url"); ap.add_argument("-o","--output",default="明太祖实录.epub"); ap.add_argument("--title",default="明太祖实录"); ap.add_argument("--cache-dir",default=".shidian_chain_cache"); ap.add_argument("--delay",type=float,default=.15); ap.add_argument("--timeout",type=float,default=30); ap.add_argument("--max-pages",type=int,default=5000); a=ap.parse_args()
    bid=book_id(a.url); cache=Path(a.cache_dir)/bid; bid,pages,warns=crawl(a.url,cache,a.delay,a.timeout,a.max_pages)
    if not pages: raise RuntimeError("没有抓到页面")
    out=Path(a.output); make_epub(out,a.title,bid,pages)
    report={"book_id":bid,"title":a.title,"pages":len(pages),"has_volume_1":any("卷之一" in p.title for p in pages),"has_volume_257":any("卷之二百五十七" in p.title for p in pages),"warnings":warns,"titles":[p.title for p in pages],"urls":[p.url for p in pages]}
    out.with_suffix(".report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    log(f"完成: {out} | {len(pages)} 页 | {out.stat().st_size/1024/1024:.2f} MiB")

if __name__=="__main__": main()
