#!/usr/bin/env python3
"""Build SEO-ready static article pages with native and resilient TOC navigation, BlogPosting JSON-LD, sitemap, and RSS from Notion index."""
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


# Static article pages do not load js/post.js: the article HTML is already built
# into /posts/<notion-page-id>/index.html. Keep TOC navigation self-contained so
# it works on the published static page, including when a stale/missing target
# ID has to be recovered from the visible TOC label.
# TOC links use native same-page HTML anchors (href="#notion-heading-...").
# Every target ID is added by attach_heading_anchor_ids() and validated before publish.
# Do not preventDefault() on these links: the browser's native fragment navigation
# is more reliable across BODY/HTML scrolling roots and remains functional without JS.



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
        # Do not use generated TOC entries as search snippets; prefer actual article text.
        if re.match(r"^\s*(?:[-*+] |\d+\. )?\[[^]]+\]\(#notion-heading-[0-9a-f]{32}\)", line, re.I):
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


def normalize_iso_datetime(value, fallback_date=""):
    """Return a Schema.org-friendly ISO datetime/date without inventing timestamps."""
    raw = str(value or "").strip()
    if raw:
        # A Notion date property is often date-only; keep it date-only rather than
        # inventing a midnight UTC timestamp. last_edited_time remains a datetime.
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            try:
                return date_cls.fromisoformat(raw).isoformat()
            except ValueError:
                return ""
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except ValueError:
            try:
                return date_cls.fromisoformat(raw[:10]).isoformat()
            except ValueError:
                pass
    if fallback_date:
        try:
            return date_cls.fromisoformat(str(fallback_date)[:10]).isoformat()
        except ValueError:
            pass
    return ""


def notion_tag_names(post):
    """Read tag names from the current Notion-authoritative blog-index schema."""
    names = []
    for tag in post.get("tags", []) or []:
        if isinstance(tag, dict):
            name = str(tag.get("name") or "").strip()
        else:
            name = str(tag or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def attach_heading_anchor_ids(fragment):
    """Move legacy Notion anchors onto the next H1-H6 before Bleach sanitizes HTML."""
    anchor = r'<a\b[^>]*\bname=["\'](?P<name>notion-heading-[0-9a-f]{32})["\'][^>]*>\s*</a>'
    pattern = re.compile(
        r'(?:<p\b[^>]*>\s*)?' + anchor + r'(?:\s*</p>)?\s*(?P<heading><h[1-6]\b[^>]*>)',
        re.I,
    )
    converted = 0

    def replace(match):
        nonlocal converted
        heading_tag = match.group("heading")
        anchor_id = match.group("name")
        if re.search(r'\bid=["\']', heading_tag, re.I):
            heading_tag = re.sub(
                r'\bid=(["\'])[^"\']*\1',
                lambda _id_match: f'id="{esc(anchor_id)}"',
                heading_tag,
                count=1,
                flags=re.I,
            )
        else:
            heading_tag = heading_tag[:-1] + f' id="{esc(anchor_id)}">'

        # Avoid having the sticky navigation cover the target heading.
        if re.search(r'\bstyle=["\']', heading_tag, re.I):
            heading_tag = re.sub(
                r'\bstyle=(["\'])(.*?)\1',
                lambda style_match: (
                    f'style={style_match.group(1)}'
                    f'{style_match.group(2).rstrip("; ")};scroll-margin-top:128px'
                    f'{style_match.group(1)}'
                ),
                heading_tag,
                count=1,
                flags=re.I,
            )
        else:
            heading_tag = heading_tag[:-1] + ' style="scroll-margin-top:128px">'
        converted += 1
        return heading_tag

    output = pattern.sub(replace, fragment)
    # Remove remaining empty legacy anchors; valid anchors have moved to headings.
    output = re.sub(r'(?:<p\b[^>]*>\s*)?' + anchor + r'(?:\s*</p>)?', "", output, flags=re.I)

    toc_ids = set(re.findall(r'href=["\']#(notion-heading-[0-9a-f]{32})["\']', output, flags=re.I))
    heading_ids = set(re.findall(r'<h[1-6]\b[^>]*\bid=["\'](notion-heading-[0-9a-f]{32})["\']', output, flags=re.I))
    missing = sorted(toc_ids - heading_ids)
    if missing:
        raise RuntimeError(
            "TOC contains heading links without matching heading IDs after anchor conversion: "
            + ", ".join(missing[:10])
        )
    return output, converted, len(toc_ids)

def first_image_from_html(fragment):
    from html.parser import HTMLParser

    class ImageExtractor(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.image = None

        def handle_starttag(self, tag, attrs):
            if tag.lower() != "img" or self.image:
                return
            attrs = dict(attrs)
            src = str(attrs.get("src") or "").strip()
            if src.startswith(("https://", "http://")):
                self.image = {"url": src, "alt": str(attrs.get("alt") or "").strip()}

    parser = ImageExtractor()
    parser.feed(str(fragment or ""))
    parser.close()
    return parser.image


def build_blogposting_jsonld(post, title, description, canonical, article_image, body):
    """Create per-article structured data using only Notion/source-derived fields."""
    date_published = normalize_iso_datetime(post.get("date"))
    date_modified = normalize_iso_datetime(post.get("last_edited_time"), date_published)
    tags = notion_tag_names(post)
    image_obj = first_image_from_html(body)
    image_url = image_obj["url"] if image_obj else article_image
    schema = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "@id": canonical + "#blogposting",
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
        "headline": title,
        "description": description,
        "image": [image_url],
        "author": {
            "@type": "Organization",
            "@id": SITE_URL + "/#organization",
            "name": BRAND,
            "url": SITE_URL + "/",
        },
        "publisher": {
            "@type": "Organization",
            "@id": SITE_URL + "/#organization",
            "name": BRAND,
            "logo": {"@type": "ImageObject", "url": SITE_URL + "/images/sss-robotics-logo.png"},
        },
        "inLanguage": "ko-KR",
        "isAccessibleForFree": True,
    }
    if date_published:
        schema["datePublished"] = date_published
    if date_modified:
        schema["dateModified"] = date_modified
    if tags:
        schema["keywords"] = tags
        schema["articleSection"] = tags[0]
    encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("</", "<\\/")
    return '<script type="application/ld+json">' + encoded + "</script>"


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
    rss_content_by_id = {}
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
        "h1": ["id", "style"], "h2": ["id", "style"], "h3": ["id", "style"],
        "h4": ["id", "style"], "h5": ["id", "style"], "h6": ["id", "style"],
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
        "scroll-margin-top",
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
        # The first H1 is the document title rendered in the page header, not body content.
        body = re.sub(r"<h1\b[^>]*>.*?</h1>\s*", "", body, count=1, flags=re.I | re.S)
        body, anchor_count, toc_link_count = attach_heading_anchor_ids(body)
        body = bleach.clean(
            body,
            tags=allowed_tags,
            attributes=allowed_attributes,
            protocols={"http", "https", "mailto", "data"},
            css_sanitizer=css_sanitizer,
        )
        body = rewrite_local_links(body, md_file, root)
        # Re-check after sanitization/link rewriting, not only before sanitization.
        final_toc_ids = set(re.findall(r'href=["\']#(notion-heading-[0-9a-f]{32})["\']', body, flags=re.I))
        final_heading_ids = set(re.findall(r'<h[1-6]\b[^>]*\bid=["\'](notion-heading-[0-9a-f]{32})["\']', body, flags=re.I))
        final_missing = sorted(final_toc_ids - final_heading_ids)
        if final_missing:
            raise RuntimeError(
                f"TOC targets were removed during HTML sanitization for {page_id}: "
                + ", ".join(final_missing[:10])
            )

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
        default_article_image = f"{SITE_URL}/images/sss-robotics-logo.png"
        image_info = first_image_from_html(body)
        article_image = image_info["url"] if image_info else default_article_image
        article_image_alt = (image_info.get("alt") if image_info else "") or f"{title} - {BRAND}"
        tags = notion_tag_names(post)
        published_time = normalize_iso_datetime(date)
        modified_time = normalize_iso_datetime(post.get("last_edited_time"), published_time)
        article_meta_lines = [
            f'  <link rel="canonical" href="{canonical}">',
            f'  <meta name="author" content="{esc(BRAND)}">',
            f'  <meta name="keywords" content="{esc(", ".join(tags))}">',
            '  <meta property="og:type" content="article">',
            f'  <meta property="og:site_name" content="{esc(BRAND)}">',
            f'  <meta property="og:title" content="{esc(article_title)}">',
            f'  <meta property="og:description" content="{esc(desc)}">',
            f'  <meta property="og:url" content="{canonical}">',
            f'  <meta property="og:image" content="{esc(article_image)}">',
            f'  <meta property="og:image:alt" content="{esc(article_image_alt)}">',
            '  <meta property="og:locale" content="ko_KR">',
            '  <meta name="twitter:card" content="summary_large_image">',
            f'  <meta name="twitter:title" content="{esc(article_title)}">',
            f'  <meta name="twitter:description" content="{esc(desc)}">',
            f'  <meta name="twitter:image" content="{esc(article_image)}">',
        ]
        if published_time:
            article_meta_lines.append(f'  <meta property="article:published_time" content="{esc(published_time)}">')
        if modified_time:
            article_meta_lines.append(f'  <meta property="article:modified_time" content="{esc(modified_time)}">')
        if tags:
            article_meta_lines.append(f'  <meta property="article:section" content="{esc(tags[0])}">')
            article_meta_lines.extend(f'  <meta property="article:tag" content="{esc(tag)}">' for tag in tags)
        article_meta = "\n".join(article_meta_lines)
        page = page.replace(description_tag, description_tag + "\n" + article_meta, 1)
        jsonld_script = build_blogposting_jsonld(post, title, desc, canonical, default_article_image, body)
        if "</head>" not in page.lower():
            raise RuntimeError(f"post.html has no closing head tag: {page_id}")
        page = re.sub(r"</head>", jsonld_script + "\n</head>", page, count=1, flags=re.I)
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
        # Static SEO pages contain the rendered article HTML. The dynamic post.js
        # loader is intentionally removed. TOC links remain ordinary native anchors;
        # heading IDs and scroll-margin are generated above and validated below.
        page = page.replace('<script src="/js/post.js" defer></script>', "")
        if "</body>" not in page.lower():
            raise RuntimeError(f"post.html template has no closing body tag: {page_id}")

        out = root / "posts" / page_id / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        # Fail the build rather than publishing an article whose TOC has dangling links.
        if toc_link_count != anchor_count:
            # A page without a TOC legitimately has zero link count and zero anchors.
            if toc_link_count or anchor_count:
                raise RuntimeError(
                    f"TOC anchor count mismatch for {page_id}: links={toc_link_count}, anchors={anchor_count}"
                )
        if '"@type":"BlogPosting"' not in jsonld_script:
            raise RuntimeError(f"BlogPosting JSON-LD missing for {page_id}")
        out.write_text(page, encoding="utf-8")
        rss_content_by_id[page_id] = {"body": body, "description": desc, "title": title, "tags": tags}
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
            lastmods[f"{SITE_URL}/posts/{page_id}/"] = normalize_iso_datetime(
                post.get("last_edited_time"), str(post.get("date", ""))
            )[:10]
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

    # RSS 2.0 feed: include a concise description and the complete article HTML.
    # The GitHub Actions workflow runs this builder after every Notion sync, so new
    # and updated posts receive full RSS content without any manual Pages edits.
    CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
    ATOM_NS = "http://www.w3.org/2005/Atom"
    ET.register_namespace("content", CONTENT_NS)
    ET.register_namespace("atom", ATOM_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{BRAND} 기술 블로그"
    ET.SubElement(channel, "link").text = f"{SITE_URL}/blog.html"
    ET.SubElement(channel, "description").text = DEFAULT_DESCRIPTION
    ET.SubElement(channel, "language").text = "ko-KR"
    ET.SubElement(channel, f"{{{ATOM_NS}}}link", {
        "href": f"{SITE_URL}/rss.xml", "rel": "self", "type": "application/rss+xml"
    })
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc), usegmt=True)
    dated_posts = []
    for post in index["posts"]:
        try:
            post_date = date_cls.fromisoformat(str(post.get("date", ""))[:10])
            page_id = safe_slug(post.get("notion_page_id"))
        except (ValueError, TypeError):
            continue
        content = rss_content_by_id.get(page_id)
        if not content:
            print(f"[WARN] RSS skips post without generated static page: {page_id}")
            continue
        dated_posts.append((post_date, post, f"{SITE_URL}/posts/{page_id}/", content))
    dated_posts.sort(key=lambda item: (item[0], item[2]), reverse=True)
    for post_date, post, link, content in dated_posts:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = content["title"]
        ET.SubElement(item, "link").text = link
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = link
        published = datetime.combine(post_date, datetime.min.time(), tzinfo=timezone.utc)
        ET.SubElement(item, "pubDate").text = format_datetime(published, usegmt=True)
        ET.SubElement(item, "description").text = content["description"]
        # RSS consumers that support the content module receive the full formatted article.
        ET.SubElement(item, f"{{{CONTENT_NS}}}encoded").text = content["body"]
        for tag in content["tags"]:
            ET.SubElement(item, "category").text = tag
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
    rss_check = ET.parse(rss_path).getroot()  # Ensure RSS is well-formed XML too.
    rss_items = rss_check.findall("./channel/item")
    content_items = rss_check.findall(f"./channel/item/{{{CONTENT_NS}}}encoded")
    if len(rss_items) != len(dated_posts) or len(content_items) != len(rss_items):
        raise RuntimeError(
            f"RSS count/content mismatch: items={len(rss_items)}, full_content={len(content_items)}, expected={len(dated_posts)}"
        )
    for item in rss_items:
        if not (item.findtext(f"{{{CONTENT_NS}}}encoded") or "").strip():
            raise RuntimeError("RSS item is missing full content:encoded")
    print(
        f"[SEO] generated {generated} static posts; sitemap URLs={len(sitemap_locs)}; "
        f"RSS items={len(rss_items)} (full content); BlogPosting JSON-LD={generated}"
    )


if __name__ == "__main__":
    main()
