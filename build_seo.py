#!/usr/bin/env python3
"""Build crawlable static article pages and sitemap from the Notion-authoritative index."""
from __future__ import annotations

import argparse
import html
import json
import os
import posixpath
from datetime import date as date_cls, datetime, timezone
from email.utils import format_datetime
import xml.etree.ElementTree as ET
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import markdown
import bleach
from bleach.css_sanitizer import CSSSanitizer

SITE_URL = "https://www.sssrobotics.com"
BRAND = "S급 로봇 연구소"
DEFAULT_DESCRIPTION = "S급 로봇 연구소의 로봇, 인공지능, 시뮬레이션 기술 학습 기록입니다."
GENERATED_MARKER = "<!-- SSS_ROBOTICS_GENERATED_SEO_PAGE -->"
# Notion exporter stores page media under TIL/..._assets. Using raw GitHub for
# image bytes avoids GitHub Pages/Jekyll filtering directories beginning with '_'.
RAW_ASSET_BASE = os.environ.get(
    "GITHUB_RAW_BASE_URL",
    "https://raw.githubusercontent.com/acestarkzip/acestarkzip.github.io/main",
).strip().rstrip("/")


def esc(value):
    return html.escape(str(value or ""), quote=True)


def safe_slug(page_id):
    value = re.sub(r"[^0-9a-fA-F-]", "", str(page_id or ""))
    if len(value.replace("-", "")) != 32:
        raise ValueError(f"Invalid Notion page ID: {page_id!r}")
    return value.replace("-", "").lower()


def description_from(markdown_text, memo):
    text = re.sub(r"<!--.*?-->", " ", markdown_text, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ">", "|", "---")):
            continue
        line = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"[`*_~]", "", line)
        line = re.sub(r"<[^>]+>", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and not line.startswith(("http://", "https://")):
            lines.append(line)
        if sum(map(len, lines)) >= 100:
            break
    result = " ".join(lines) or memo or DEFAULT_DESCRIPTION
    return result[:155].rsplit(" ", 1)[0] if len(result) > 155 else result


def rewrite_local_links(fragment, md_path, site_root):
    """Resolve relative links from the Markdown file's *repository path*.

    Important: md_path is an absolute runner filesystem path. It must first be
    made relative to site_root; publishing that absolute path into an HTML src
    attribute creates broken URLs such as /home/runner/work/... .
    """
    from html.parser import HTMLParser

    root = Path(site_root).resolve()
    source = Path(md_path).resolve()
    try:
        md_relative = source.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Markdown file is outside the Pages repository: {source}") from exc

    class Rewriter(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.out = []

        def handle_starttag(self, tag, attrs):
            new_attrs = []
            for key, value in attrs:
                if value and key in ("src", "href") and not re.match(
                    r"^(?:[a-z][a-z0-9+.-]*:|//|#|/)", value, re.I
                ):
                    parsed = urlsplit(value)
                    relative_target = unquote(parsed.path)
                    resolved = posixpath.normpath(
                        posixpath.join(posixpath.dirname(md_relative), relative_target)
                    )
                    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
                        # Do not turn paths escaping the published repository into URLs.
                        new_attrs.append((key, value))
                        continue

                    encoded = quote(resolved, safe="/-._~")
                    suffix = (f"?{parsed.query}" if parsed.query else "") + (
                        f"#{parsed.fragment}" if parsed.fragment else ""
                    )
                    if tag.lower() == "img" and key.lower() == "src" and resolved.startswith("TIL/"):
                        # The exporter stores images in folders named *_assets.
                        # Those paths can be omitted by Jekyll, so serve the image
                        # directly from the repository's raw-content endpoint.
                        value = f"{RAW_ASSET_BASE}/{encoded}{suffix}"
                    else:
                        value = f"/{encoded}{suffix}"
                new_attrs.append((key, value))

            self.out.append(
                "<" + tag + "".join(
                    f' {key}="{esc(value)}"' if value is not None else f" {key}"
                    for key, value in new_attrs
                ) + ">"
            )

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            self.out[-1] = self.out[-1][:-1] + " />"

        def handle_endtag(self, tag):
            self.out.append(f"</{tag}>")

        def handle_data(self, data):
            self.out.append(data)

        def handle_entityref(self, name):
            self.out.append(f"&{name};")

        def handle_charref(self, name):
            self.out.append(f"&#{name};")

        def handle_comment(self, data):
            pass

    parser = Rewriter()
    parser.feed(fragment)
    parser.close()
    return "".join(parser.out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-dir", default="pages-site")
    args = ap.parse_args()

    root = Path(args.site_dir).resolve()
    index_path = root / "TIL" / "blog-index.json"
    template_path = root / "post.html"
    if not index_path.is_file() or not template_path.is_file():
        raise SystemExit("Missing TIL/blog-index.json or post.html")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("source_of_truth") != "notion_database" or not isinstance(index.get("posts"), list):
        raise SystemExit("Refusing to build from a non-authoritative blog index")

    template = template_path.read_text(encoding="utf-8")
    urls = {SITE_URL + "/", SITE_URL + "/blog.html"}
    generated = 0
    raw_ids = [safe_slug(post.get("notion_page_id")) for post in index["posts"]]
    if len(raw_ids) != len(set(raw_ids)):
        raise RuntimeError("Duplicate Notion page IDs detected in blog-index.json")

    allowed_tags = set(bleach.sanitizer.ALLOWED_TAGS).union({
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "img", "pre", "code",
        "hr", "br", "table", "thead", "tbody", "tr", "th", "td", "ul", "ol",
        "li", "blockquote", "div", "span", "details", "summary", "sup", "sub", "u",
    })
    allowed_attributes = {
        **bleach.sanitizer.ALLOWED_ATTRIBUTES,
        "a": ["href", "title", "target", "rel", "class"],
        "img": ["src", "alt", "title", "width", "height", "class", "style"],
        "p": ["class", "style"],
        "blockquote": ["class", "style"],
        "ul": ["class", "style"],
        "ol": ["class", "style"],
        "li": ["class", "style"],
        "div": ["class", "style"],
        "span": ["class", "style"],
        "code": ["class"],
        "pre": ["class"],
        "th": ["align"],
        "td": ["align"],
    }
    css_sanitizer = CSSSanitizer(allowed_css_properties={
        "box-sizing", "width", "max-width", "height", "margin", "display",
        "border-radius", "list-style-position", "padding", "line-height",
        "border-left", "color", "font-size", "text-align", "vertical-align",
    })

    for post in index["posts"]:
        md_rel = Path(str(post.get("path", "")))
        if md_rel.is_absolute() or ".." in md_rel.parts or not str(md_rel).startswith("TIL/") or md_rel.suffix.lower() != ".md":
            print(f"[WARN] skip unsafe markdown path: {md_rel}")
            continue
        md_file = root / md_rel
        if not md_file.is_file():
            print(f"[WARN] missing markdown: {md_rel}")
            continue

        page_id = safe_slug(post.get("notion_page_id"))
        title = str(post.get("title") or md_file.stem)
        date = str(post.get("date") or "")
        raw = md_file.read_text(encoding="utf-8")
        desc = description_from(raw, post.get("memo", ""))
        body = markdown.markdown(raw, extensions=["extra", "sane_lists", "nl2br"])
        body = re.sub(r"<h1\b[^>]*>.*?</h1>\s*", "", body, count=1, flags=re.I | re.S)
        body = bleach.clean(
            body,
            tags=allowed_tags,
            attributes=allowed_attributes,
            protocols={"http", "https", "mailto", "data"},
            css_sanitizer=css_sanitizer,
        )
        body = rewrite_local_links(body, md_file, root)

        canonical = f"{SITE_URL}/posts/{page_id}/"
        page = template
        page = page.replace("<!DOCTYPE html>", "<!DOCTYPE html>\n" + GENERATED_MARKER, 1)

        # Set a unique description for this article.
        description_tag = f'<meta name="description" content="{esc(desc)}">'
        page, replaced = re.subn(
            r'<meta name="description" content="[^"]*">',
            lambda _match: description_tag,
            page,
            count=1,
            flags=re.I,
        )
        if replaced != 1:
            raise RuntimeError("post.html must contain exactly one meta description tag")

        # Remove the generic fallback metadata in post.html, then add article-specific values.
        page = "\n".join(
            line for line in page.splitlines()
            if not re.match(r'^[ \t]*<meta[ \t]+(?:property="og:|name="twitter:)', line, re.I)
            and not re.match(r'^[ \t]*<link[ \t]+rel="canonical"', line, re.I)
        )
        article_title = f"{title} | {BRAND}"
        article_image = f"{SITE_URL}/images/sss-robotics-logo.png"
        article_meta = "\n".join([
            f'  <link rel="canonical" href="{canonical}">',
            '  <meta property="og:type" content="article">',
            f'  <meta property="og:site_name" content="{esc(BRAND)}">',
            f'  <meta property="og:title" content="{esc(article_title)}">',
            f'  <meta property="og:description" content="{esc(desc)}">',
            f'  <meta property="og:url" content="{canonical}">',
            f'  <meta property="og:image" content="{article_image}">',
            '  <meta property="og:image:alt" content="S급 로봇 연구소 공식 로고">',
            '  <meta property="og:locale" content="ko_KR">',
            '  <meta name="twitter:card" content="summary">',
            f'  <meta name="twitter:title" content="{esc(article_title)}">',
            f'  <meta name="twitter:description" content="{esc(desc)}">',
            f'  <meta name="twitter:image" content="{article_image}">',
            f'  <meta property="article:published_time" content="{esc(date)}">',
        ])
        page = page.replace(description_tag, description_tag + "\n" + article_meta, 1)
        page = page.replace(
            "<title>S급 로봇 연구소 - 로봇과 인간이 공존할 미래를 준비합니다.</title>",
            f"<title>{esc(title)} | {BRAND}</title>",
        )
        page = page.replace('href="./', 'href="/').replace('src="./', 'src="/')
        # Keep generated article pages on the same current favicon as the site root.
        favicon_block = (
            '  <!-- SSS Robotics current brand favicon (cache-busted) -->\n'
            '  <link rel="icon" href="/favicon.ico?v=20261118" sizes="any">\n'
            '  <link rel="icon" type="image/png" href="/favicon.png?v=20261118">\n'
            '  <link rel="shortcut icon" href="/favicon.ico?v=20261118">\n'
            '  <link rel="apple-touch-icon" href="/favicon.png?v=20261118">'
        )
        page = re.sub(r'\s*<link\s+rel="(?:shortcut icon|icon)"[^>]*>', '', page, flags=re.I)
        page = page.replace('<meta name="theme-color" content="#080b09">',
                            '<meta name="theme-color" content="#080b09">\n' + favicon_block, 1)
        page = page.replace('href="/index.html#', 'href="/#')
        page = page.replace("게시글을 불러오는 중입니다...", esc(title))
        page = page.replace(
            '<p class="article-path" id="article-path"></p>',
            f'<p class="article-path" id="article-path">{esc(date)} · {esc(post.get("memo", ""))}</p>',
        )
        loading = '<div class="loading-state"><span class="loading-ring" aria-hidden="true"></span><span>Markdown을 불러오는 중입니다...</span></div>'
        page = page.replace(loading, body)
        page = page.replace('<script src="/js/post.js" defer></script>', "")

        out = root / "posts" / page_id / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
        urls.add(canonical)
        generated += 1

    for name in ("about.html", "projects.html", "contact.html"):
        if (root / name).is_file():
            urls.add(f"{SITE_URL}/{name}")

    # Remove only stale pages previously generated by this script. Never touch
    # manually maintained files in /posts or pages without our marker.
    active_ids = {safe_slug(post.get("notion_page_id")) for post in index["posts"]
                  if post.get("notion_page_id")}
    posts_root = root / "posts"
    if posts_root.exists():
        for old_page in posts_root.glob("*/index.html"):
            try:
                old_text = old_page.read_text(encoding="utf-8")
            except OSError:
                continue
            old_id = old_page.parent.name.lower()
            if GENERATED_MARKER in old_text and old_id not in active_ids:
                old_page.unlink()
                try:
                    old_page.parent.rmdir()
                except OSError:
                    pass
                print(f"[SEO] removed stale generated page: {old_id}")

    # Build an authoritative lastmod map from Notion's post date. Avoid fake
    # modification timestamps: use the real date field only when it is valid.
    lastmods = {}
    for post in index["posts"]:
        try:
            page_id = safe_slug(post.get("notion_page_id"))
            lastmods[f"{SITE_URL}/posts/{page_id}/"] = date_cls.fromisoformat(str(post.get("date", ""))).isoformat()
        except (ValueError, TypeError):
            continue

    # Sitemap: unique canonical URLs + lastmod where a real Notion date exists.
    sitemap_root = ET.Element("urlset", {"xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9"})
    for url in sorted(urls):
        entry = ET.SubElement(sitemap_root, "url")
        ET.SubElement(entry, "loc").text = url
        if url in lastmods:
            ET.SubElement(entry, "lastmod").text = lastmods[url]
    sitemap_path = root / "sitemap.xml"
    ET.indent(sitemap_root, space="  ")
    ET.ElementTree(sitemap_root).write(sitemap_path, encoding="utf-8", xml_declaration=True)

    # RSS 2.0 feed: latest posts first, with absolute canonical links.
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{BRAND} 기술 블로그"
    ET.SubElement(channel, "link").text = f"{SITE_URL}/blog.html"
    ET.SubElement(channel, "description").text = DEFAULT_DESCRIPTION
    ET.SubElement(channel, "language").text = "ko-KR"
    ET.SubElement(channel, "atom:link", {
        "xmlns:atom": "http://www.w3.org/2005/Atom",
        "href": f"{SITE_URL}/rss.xml", "rel": "self", "type": "application/rss+xml"
    })
    dated_posts = []
    for post in index["posts"]:
        try:
            post_date = date_cls.fromisoformat(str(post.get("date", "")))
        except (ValueError, TypeError):
            continue
        page_id = safe_slug(post.get("notion_page_id"))
        dated_posts.append((post_date, post, f"{SITE_URL}/posts/{page_id}/"))
    dated_posts.sort(key=lambda item: (item[0], item[2]), reverse=True)
    for post_date, post, link in dated_posts:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = str(post.get("title") or "제목 없는 게시글")
        ET.SubElement(item, "link").text = link
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = link
        published = datetime.combine(post_date, datetime.min.time(), tzinfo=timezone.utc)
        ET.SubElement(item, "pubDate").text = format_datetime(published, usegmt=True)
        ET.SubElement(item, "description").text = str(post.get("memo") or DEFAULT_DESCRIPTION)
    rss_path = root / "rss.xml"
    ET.indent(rss, space="  ")
    ET.ElementTree(rss).write(rss_path, encoding="utf-8", xml_declaration=True)

    # Validate generated XML and enforce uniqueness/count consistency before commit.
    sitemap_check = ET.parse(sitemap_path).getroot()
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap_locs = [node.text for node in sitemap_check.findall("sm:url/sm:loc", ns)]
    if len(sitemap_locs) != len(set(sitemap_locs)):
        raise RuntimeError("Duplicate URL detected in sitemap.xml")
    expected_post_urls = {f"{SITE_URL}/posts/{safe_slug(p.get('notion_page_id'))}/" for p in index["posts"]}
    missing_urls = expected_post_urls.difference(sitemap_locs)
    if missing_urls:
        raise RuntimeError("Sitemap is missing active post URLs: " + ", ".join(sorted(missing_urls)))
    if len(dated_posts) != len(index["posts"]):
        print(f"[WARN] RSS contains {len(dated_posts)} of {len(index['posts'])} posts with valid dates")
    ET.parse(rss_path)  # Ensure RSS is well-formed XML too.
    print(f"[SEO] generated {generated} static posts; sitemap URLs={len(sitemap_locs)}; RSS items={len(dated_posts)}")


if __name__ == "__main__":
    main()
