#!/usr/bin/env python3
"""Build crawlable static article pages and sitemap from the Notion-authoritative index."""
from __future__ import annotations
import argparse, html, json, re
from pathlib import Path
from urllib.parse import quote, unquote
import markdown
import bleach

SITE_URL = "https://www.sssrobotics.com"
BRAND = "S급 로봇 연구소"
DEFAULT_DESCRIPTION = "S급 로봇 연구소의 로봇, 인공지능, 시뮬레이션 기술 학습 기록입니다."

def esc(value): return html.escape(str(value or ""), quote=True)
def safe_slug(page_id):
    value = re.sub(r"[^0-9a-fA-F-]", "", str(page_id or ""))
    if len(value.replace('-', '')) != 32: raise ValueError(f"Invalid Notion page ID: {page_id!r}")
    return value.replace('-', '').lower()

def description_from(markdown_text, memo):
    # Remove front matter/comments/markdown syntax and use the first meaningful paragraph.
    text = re.sub(r"<!--.*?-->", " ", markdown_text, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    lines=[]
    for line in text.splitlines():
        line=line.strip()
        if not line or line.startswith('#') or line.startswith('>') or line.startswith('|') or line.startswith('---'):
            continue
        line=re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", line)
        line=re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", line)
        line=re.sub(r"[`*_~]", "", line)
        line=re.sub(r"<[^>]+>", " ", line)
        line=re.sub(r"\s+", " ", line).strip()
        if line and not line.startswith(('http://','https://')):
            lines.append(line)
        if sum(map(len, lines)) >= 100: break
    result = " ".join(lines) or memo or DEFAULT_DESCRIPTION
    return result[:155].rsplit(' ', 1)[0] if len(result) > 155 else result

def rewrite_local_links(fragment, md_path):
    # Markdown links/images are relative to their original TIL markdown file.
    from html.parser import HTMLParser
    class Rewriter(HTMLParser):
        def __init__(self): super().__init__(convert_charrefs=False); self.out=[]
        def handle_starttag(self, tag, attrs):
            new=[]
            for k,v in attrs:
                if v and k in ('src','href') and not re.match(r'^(?:[a-z]+:|//|#|/)', v, re.I):
                    resolved=(md_path.parent / unquote(v)).as_posix()
                    # Keep links within the published repository; avoid path traversal.
                    parts=Path(resolved).parts
                    if '..' not in parts:
                        v='/' + quote(resolved, safe='/')
                new.append((k,v))
            self.out.append('<'+tag+''.join(f' {k}="{esc(v)}"' if v is not None else f' {k}' for k,v in new)+'>')
        def handle_startendtag(self, tag, attrs): self.handle_starttag(tag, attrs); self.out[-1]=self.out[-1][:-1]+' />'
        def handle_endtag(self, tag): self.out.append(f'</{tag}>')
        def handle_data(self, data): self.out.append(data)
        def handle_entityref(self, name): self.out.append(f'&{name};')
        def handle_charref(self, name): self.out.append(f'&#{name};')
        def handle_comment(self, data): pass
    parser=Rewriter(); parser.feed(fragment); return ''.join(parser.out)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--site-dir', default='pages-site'); args=ap.parse_args()
    root=Path(args.site_dir).resolve(); index_path=root/'TIL'/'blog-index.json'
    template_path=root/'post.html'
    if not index_path.is_file() or not template_path.is_file(): raise SystemExit('Missing TIL/blog-index.json or post.html')
    index=json.loads(index_path.read_text(encoding='utf-8'))
    if index.get('source_of_truth') != 'notion_database' or not isinstance(index.get('posts'),list): raise SystemExit('Refusing to build from a non-authoritative blog index')
    template=template_path.read_text(encoding='utf-8')
    urls={SITE_URL+'/', SITE_URL+'/blog.html'}
    generated=0
    for post in index['posts']:
        md_rel=Path(str(post.get('path','')))
        if md_rel.is_absolute() or '..' in md_rel.parts or not str(md_rel).startswith('TIL/') or md_rel.suffix.lower()!='.md':
            print(f"[WARN] skip unsafe markdown path: {md_rel}"); continue
        md_file=root/md_rel
        if not md_file.is_file(): print(f"[WARN] missing markdown: {md_rel}"); continue
        page_id=safe_slug(post.get('notion_page_id'))
        title=str(post.get('title') or md_file.stem)
        date=str(post.get('date') or '')
        raw=md_file.read_text(encoding='utf-8')
        desc=description_from(raw, post.get('memo',''))
        body=markdown.markdown(raw, extensions=['extra','sane_lists','nl2br'])
        body=re.sub(r'<h1\b[^>]*>.*?</h1>\s*', '', body, count=1, flags=re.I|re.S)
        body=bleach.clean(body, tags=bleach.sanitizer.ALLOWED_TAGS.union({'p','h1','h2','h3','h4','h5','h6','img','pre','code','hr','br','table','thead','tbody','tr','th','td','ul','ol','li','blockquote','div','span','details','summary','sup','sub'}), attributes={**bleach.sanitizer.ALLOWED_ATTRIBUTES, 'a':['href','title','target','rel'], 'img':['src','alt','title','width','height'], 'code':['class'], 'pre':['class'], 'th':['align'], 'td':['align']}, protocols={'http','https','mailto','data'})
        body=rewrite_local_links(body, md_file)
        canonical=f'{SITE_URL}/posts/{page_id}/'
        page=template
        page=page.replace('<html lang="ko">','<html lang="ko">',1)
        page=page.replace('<meta name="description" content="S급 로봇 연구소 — 로봇과 인간이 공존할 미래를 준비합니다.">', f'<meta name="description" content="{esc(desc)}">\n  <link rel="canonical" href="{canonical}">\n  <meta property="og:type" content="article">\n  <meta property="og:site_name" content="{BRAND}">\n  <meta property="og:title" content="{esc(title)} | {BRAND}">\n  <meta property="og:description" content="{esc(desc)}">\n  <meta property="og:url" content="{canonical}">\n  <meta property="article:published_time" content="{esc(date)}">')
        page=page.replace('<title>S급 로봇 연구소 - 로봇과 인간이 공존할 미래를 준비합니다.</title>', f'<title>{esc(title)} | {BRAND}</title>')
        # Nested URL-safe asset paths: use root-relative CSS/JS/logo and nav links.
        page=page.replace('href="./', 'href="/').replace('src="./', 'src="/')
        page=page.replace('href="/index.html#', 'href="/#').replace('href="/blog.html"', 'href="/blog.html"')
        page=page.replace('href="/post.html?file=', 'href="/post.html?file=')
        page=page.replace('게시글을 불러오는 중입니다...', esc(title))
        page=page.replace('<p class="article-path" id="article-path"></p>', f'<p class="article-path" id="article-path">{esc(date)} · {esc(post.get("memo", ""))}</p>')
        loading='<div class="loading-state"><span class="loading-ring" aria-hidden="true"></span><span>Markdown을 불러오는 중입니다...</span></div>'
        page=page.replace(loading, body)
        # Static page must not execute legacy query-driven loader.
        page=page.replace('<script src="/js/post.js" defer></script>', '')
        out=root/'posts'/page_id/'index.html'; out.parent.mkdir(parents=True,exist_ok=True); out.write_text(page,encoding='utf-8')
        urls.add(canonical); generated+=1
    # Include other stable top-level pages only when present.
    for name in ('about.html','projects.html','contact.html'):
        if (root/name).is_file(): urls.add(f'{SITE_URL}/{name}')
    from datetime import datetime, timezone
    xml=['<?xml version="1.0" encoding="UTF-8"?>','<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url in sorted(urls): xml += ['  <url>',f'    <loc>{esc(url)}</loc>','  </url>']
    xml.append('</urlset>')
    (root/'sitemap.xml').write_text('\n'.join(xml)+'\n',encoding='utf-8')
    print(f'[SEO] generated {generated} static posts; sitemap URLs={len(urls)}')
if __name__=='__main__': main()
