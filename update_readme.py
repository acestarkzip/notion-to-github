"""Sync Notion database pages to GitHub-friendly Markdown.

Required environment variables:
    NOTION_TOKEN
    NOTION_DATABASE_ID

Optional environment variables:
    FETCH_MODE=DAILY | ALL          # DAILY imports yesterday's pages (KST)
    RESET_MODE=true | false         # Recreate README.md template before updating
    DOWNLOAD_NOTION_MEDIA=true      # Download Notion-hosted media into TIL/.../assets/
    NOTION_VERSION=2026-03-11      # Current API version; this script resolves the data source
    NOTION_DATA_SOURCE_ID=...       # Required only if the database contains multiple data sources

Notes:
- Notion's interactive UI cannot be reproduced completely in GitHub Markdown.
- YouTube blocks become clickable thumbnails; GitHub Markdown does not render YouTube iframes.
- Native video/PDF/audio HTML tags are emitted as a best-effort preview. GitHub Pages is more
  flexible than the repository's Markdown Preview, which sanitizes some embedded HTML.
- Notion-hosted file URLs are signed and expire. Set DOWNLOAD_NOTION_MEDIA=true to keep local
  copies in the repository (watch GitHub's file-size limits, especially for videos).
"""

from __future__ import annotations

import html
import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

# =======================================================
# [설정 영역]
# =======================================================

SAVE_DIR_ROOT = Path(os.getenv("SAVE_DIR_ROOT", "TIL"))
NOTION_PROPERTY_TITLE = os.getenv("NOTION_PROPERTY_TITLE", "제목")
NOTION_PROPERTY_DATE = os.getenv("NOTION_PROPERTY_DATE", "날짜")
README_FILE = Path(os.getenv("README_FILE", "README.md"))
MARKER_START = "<!-- START_TIL -->"
MARKER_END = "<!-- END_TIL -->"
TIMEZONE_HOURS = 9

# Current Notion API version. The query endpoint is resolved automatically:
# /databases/{id}/query for legacy versions, /data_sources/{id}/query for 2025+.
NOTION_VERSION = os.getenv("NOTION_VERSION", "2026-03-11")
NOTION_DATA_SOURCE_ID = os.getenv("NOTION_DATA_SOURCE_ID", "").strip()
DOWNLOAD_NOTION_MEDIA = os.getenv("DOWNLOAD_NOTION_MEDIA", "false").lower() == "true"
MAX_MEDIA_MB = int(os.getenv("MAX_MEDIA_MB", "50"))
MAX_VIDEO_MB = int(os.getenv("MAX_VIDEO_MB", "90"))
MAX_BLOCK_DEPTH = 40
REQUEST_TIMEOUT = (10, 45)
MAX_RETRIES = 5

DEFAULT_README_TEMPLATE = f"""# 📚 My TIL Collection

노션(Today I Learned) 자동화 수집 공간입니다.

## 📌 최근 학습 목록
{MARKER_START}
{MARKER_END}
"""

# =======================================================
# [인증 및 HTTP]
# =======================================================

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": NOTION_VERSION,
}
SESSION = requests.Session()


def request_json(method: str, url: str, **kwargs) -> dict:
    """Call Notion API with timeout and basic rate-limit/server-error retries."""
    for attempt in range(MAX_RETRIES):
        try:
            response = SESSION.request(
                method,
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(min(2 ** attempt, 16))
            continue

        if response.status_code == 429 or response.status_code in (500, 502, 503, 504):
            if attempt == MAX_RETRIES - 1:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(2 ** attempt, 16)
            time.sleep(delay)
            continue

        if not response.ok:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(
                f"Notion API 요청 실패 ({response.status_code}): {detail}"
            )

        return response.json()

    raise RuntimeError("Notion API 요청 재시도 횟수를 초과했습니다.")


# =======================================================
# [Notion 블록 가져오기]
# =======================================================


def get_page_blocks(block_id: str, depth: int = 0) -> list[dict]:
    """Fetch every child page of a block, including paginated/nested children."""
    if depth > MAX_BLOCK_DEPTH:
        return []

    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    blocks: list[dict] = []
    cursor = None

    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor

        data = request_json("GET", url, params=params)
        for block in data.get("results", []):
            if block.get("has_children"):
                block["children"] = get_page_blocks(block["id"], depth + 1)
            else:
                block.setdefault("children", [])
            blocks.append(block)

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break

    return blocks


# =======================================================
# [공통 유틸리티]
# =======================================================


def markdown_url(url: str) -> str:
    """Escape spaces and closing parentheses in Markdown destinations."""
    if not url:
        return ""
    return url.replace(" ", "%20").replace(")", "%29")


def get_rich_text_plain(rich_text_list: list[dict] | None) -> str:
    return "".join(item.get("plain_text", "") for item in (rich_text_list or []))


def extract_text_from_rich_text(rich_text_list: list[dict] | None) -> str:
    """Convert Notion rich_text annotations and hyperlinks into Markdown."""
    parts = []
    for item in rich_text_list or []:
        plain = item.get("plain_text", "")
        annotations = item.get("annotations", {}) or {}

        # Escape literal Markdown characters before applying formatting.
        plain = plain.replace("\\", "\\\\")
        for char in ("*", "_", "`", "[", "]"):
            plain = plain.replace(char, "\\" + char)

        if annotations.get("code"):
            plain = f"`{plain.replace('`', '\\`')}`"
        else:
            if annotations.get("bold"):
                plain = f"**{plain}**"
            if annotations.get("italic"):
                plain = f"*{plain}*"
            if annotations.get("strikethrough"):
                plain = f"~~{plain}~~"
            if annotations.get("underline"):
                plain = f"<u>{plain}</u>"

            color = annotations.get("color", "default")
            if color != "default":
                # GitHub may strip inline CSS; <mark> is the most portable highlight.
                if color.endswith("_background"):
                    plain = f"<mark>{plain}</mark>"

        href = item.get("href")
        if href:
            parts.append(f"[{plain}]({markdown_url(href)})")
        else:
            parts.append(plain)

    return "".join(parts)


def get_block_text(block: dict, block_type: str | None = None) -> str:
    kind = block_type or block.get("type", "")
    payload = block.get(kind, {}) or {}
    if isinstance(payload.get("rich_text"), list):
        return extract_text_from_rich_text(payload["rich_text"])
    if isinstance(payload.get("title"), str):
        return payload["title"]
    if isinstance(payload.get("title"), list):
        return extract_text_from_rich_text(payload["title"])
    return ""


def get_media_url(payload: dict) -> str:
    """Get a URL from Notion's file/external media object shapes."""
    source = payload.get("file") or payload.get("external") or {}
    if isinstance(source, dict):
        return source.get("url", "") or ""
    return ""


def get_caption(payload: dict) -> str:
    return extract_text_from_rich_text(payload.get("caption", []))


def youtube_video_id(url: str) -> str | None:
    patterns = (
        r"(?:youtube\.com/watch\?[^#]*?v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def sanitize_filename(title: str) -> str:
    clean_name = re.sub(r'[\\/*?:"<>|]', "", title).strip()
    clean_name = re.sub(r"\s+", "_", clean_name)
    return clean_name[:100] or "Untitled"


def get_file_extension(url: str, fallback: str = ".bin") -> str:
    path = unquote(urlparse(url).path)
    suffix = Path(path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or ""):
        return suffix
    return fallback


def download_media_asset(
    url: str,
    block: dict,
    assets_dir: Path | None,
    markdown_dir: Path,
) -> str:
    """Optionally download media to a stable relative path; otherwise return source URL."""
    if not url:
        return ""
    if not DOWNLOAD_NOTION_MEDIA or assets_dir is None:
        return url

    block_type = block.get("type", "file")
    fallback_ext = {
        "image": ".png",
        "pdf": ".pdf",
        "video": ".mp4",
        "audio": ".mp3",
    }.get(block_type, ".bin")
    ext = get_file_extension(url, fallback_ext)
    block_id = re.sub(r"[^a-fA-F0-9]", "", block.get("id", ""))[:12] or str(abs(hash(url)))[:12]
    assets_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{block_type}_{block_id}{ext}"
    destination = assets_dir / filename

    if not destination.exists():
        limit_mb = MAX_VIDEO_MB if block_type == "video" else MAX_MEDIA_MB
        temp_path = destination.with_suffix(destination.suffix + ".part")
        try:
            # Do not send the Notion Authorization header to an external file host.
            with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > limit_mb * 1024 * 1024:
                    print(f"[WARN] 미디어 용량 제한 초과({limit_mb}MB), 원본 링크 사용: {url}")
                    return url

                total = 0
                with open(temp_path, "wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 128):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > limit_mb * 1024 * 1024:
                            raise ValueError(f"미디어가 {limit_mb}MB 제한을 초과했습니다.")
                        output.write(chunk)
            temp_path.replace(destination)
        except (requests.RequestException, OSError, ValueError) as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            print(f"[WARN] 미디어 다운로드 실패, 원본 링크 사용: {exc}")
            return url

    relative = os.path.relpath(destination, start=markdown_dir).replace(os.sep, "/")
    return quote(relative, safe="/._-")


def html_attr(value: str) -> str:
    return html.escape(value, quote=True)


def render_video(url: str, title: str = "Video") -> str:
    """Render YouTube as a linked thumbnail, or direct media as HTML video."""
    if not url:
        return ""

    video_id = youtube_video_id(url)
    if video_id:
        thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
        return (
            f'<p><a href="{html_attr(url)}">'
            f'<img src="{html_attr(thumbnail)}" alt="{html_attr(title)}" '
            f'width="640" /></a></p>\n'
            f"[▶ YouTube에서 동영상 보기]({markdown_url(url)})\n\n"
        )

    extension = get_file_extension(url, "").lower()
    if extension in {".mp4", ".webm", ".ogv", ".ogg", ".mov", ".m4v"}:
        safe_url = html_attr(url)
        return (
            f'<video controls preload="metadata" width="100%" src="{safe_url}">'
            f"Your browser does not support the video tag.</video>\n\n"
            f"[▶ {title} 열기]({markdown_url(url)})\n\n"
        )

    # For providers such as Vimeo, the URL is a webpage rather than a media file.
    return f"🎥 [{title} 보기]({markdown_url(url)})\n\n"


def infer_github_alert(callout_payload: dict) -> str:
    """Map Notion callout icon/background to GitHub's supported alert types."""
    icon = callout_payload.get("icon", {}) or {}
    emoji = icon.get("emoji", "") if icon.get("type") == "emoji" else ""
    color = callout_payload.get("color", "default")

    if any(mark in emoji for mark in ("⚠", "🚨", "🔥", "⛔")) or color.startswith(("red", "orange")):
        return "WARNING"
    if any(mark in emoji for mark in ("🛑", "☠")):
        return "CAUTION"
    if any(mark in emoji for mark in ("❗", "📌", "🚩")) or color.startswith("purple"):
        return "IMPORTANT"
    if any(mark in emoji for mark in ("💡", "✅", "🌱", "🎯")) or color.startswith(("green", "blue")):
        return "TIP"
    return "NOTE"


# =======================================================
# [Notion 블록 -> GitHub Markdown 변환]
# =======================================================


def render_table(block: dict) -> str:
    payload = block.get("table", {}) or {}
    rows = block.get("children", []) or []
    if not rows:
        return ""

    parsed_rows = []
    for row_block in rows:
        row = row_block.get("table_row", {}) or {}
        cells = [extract_text_from_rich_text(cell) for cell in row.get("cells", [])]
        parsed_rows.append(cells)

    width = max((len(row) for row in parsed_rows), default=0)
    if width == 0:
        return ""

    def normalize(cell: str) -> str:
        return cell.replace("|", "\\|").replace("\n", "<br>")

    normalized = [row + [""] * (width - len(row)) for row in parsed_rows]
    output = []
    first_row_is_header = bool(payload.get("has_column_header"))

    if first_row_is_header:
        header = normalized[0]
        output.append("| " + " | ".join(normalize(cell) for cell in header) + " |")
        output.append("| " + " | ".join("---" for _ in range(width)) + " |")
        data_rows = normalized[1:]
    else:
        output.append("| " + " | ".join(f"Column {i + 1}" for i in range(width)) + " |")
        output.append("| " + " | ".join("---" for _ in range(width)) + " |")
        data_rows = normalized

    for row_index, row in enumerate(data_rows):
        if payload.get("has_row_header") and row:
            row[0] = f"**{row[0]}**"
        output.append("| " + " | ".join(normalize(cell) for cell in row) + " |")

    return "\n".join(output) + "\n\n"


def block_to_markdown(
    block: dict,
    depth: int = 0,
    assets_dir: Path | None = None,
    markdown_dir: Path | None = None,
    column_number: int | None = None,
) -> str:
    """Convert a Notion block and all nested children into GitHub-friendly Markdown."""
    block_type = block.get("type", "unsupported")
    payload = block.get(block_type, {}) or {}
    indent = "  " * depth
    output = ""
    markdown_dir = markdown_dir or Path(".")

    if block_type in ("paragraph", "heading_1", "heading_2", "heading_3", "heading_4"):
        content = get_block_text(block)
        if block_type == "paragraph":
            if content.strip():
                output += f"{indent}{content}\n\n"
        else:
            level = int(block_type.rsplit("_", 1)[1])
            output += f"\n{'#' * level} {content}\n\n"

        # Toggleable Notion headings can have children; preserve them as a details section.
        if block.get("children"):
            title = content or "상세 내용"
            nested = "".join(
                block_to_markdown(child, depth, assets_dir, markdown_dir)
                for child in block["children"]
            )
            output += f"<details>\n<summary>{title}</summary>\n\n{nested}\n</details>\n\n"
        return output

    if block_type in ("bulleted_list_item", "numbered_list_item", "to_do"):
        content = get_block_text(block)
        if block_type == "bulleted_list_item":
            output += f"{indent}- {content}\n"
        elif block_type == "numbered_list_item":
            output += f"{indent}1. {content}\n"
        else:
            checked = "x" if payload.get("checked") else " "
            output += f"{indent}- [{checked}] {content}\n"

        for child in block.get("children", []):
            output += block_to_markdown(child, depth + 1, assets_dir, markdown_dir)
        return output + ("\n" if block.get("children") else "")

    if block_type == "toggle":
        title = get_block_text(block) or "자세히 보기"
        nested = "".join(
            block_to_markdown(child, depth, assets_dir, markdown_dir)
            for child in block.get("children", [])
        )
        return f"<details>\n<summary>{title}</summary>\n\n{nested}\n</details>\n\n"

    if block_type == "quote":
        content = get_block_text(block)
        quoted = "\n".join(f"> {line}" for line in content.splitlines() or [""])
        output += f"{quoted}\n\n"

    elif block_type == "callout":
        content = get_block_text(block)
        icon = payload.get("icon", {}) or {}
        emoji = icon.get("emoji", "💡") if icon.get("type") == "emoji" else "💡"
        alert_type = infer_github_alert(payload)
        output += f"> [!{alert_type}]\n"
        for line in content.splitlines() or [""]:
            output += f"> {emoji} {line}\n"
        output += "\n"

    elif block_type == "code":
        language = payload.get("language", "text") or "text"
        content = get_rich_text_plain(payload.get("rich_text", []))
        longest_ticks = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
        fence = "`" * max(3, longest_ticks + 1)
        output += f"{indent}{fence}{language}\n{content}\n{indent}{fence}\n\n"

    elif block_type == "equation":
        expression = payload.get("expression", "")
        output += f"\n$$\n{expression}\n$$\n\n" if expression else ""

    elif block_type == "image":
        url = get_media_url(payload)
        url = download_media_asset(url, block, assets_dir, markdown_dir)
        caption = get_caption(payload)
        if url:
            output += f"![{caption or 'Notion image'}]({markdown_url(url)})\n\n"

    elif block_type == "video":
        url = get_media_url(payload)
        caption = get_caption(payload) or "Notion video"
        # Keep YouTube URLs intact so they render as linked thumbnails, even when
        # DOWNLOAD_NOTION_MEDIA is enabled for locally uploaded Notion files.
        if not youtube_video_id(url):
            url = download_media_asset(url, block, assets_dir, markdown_dir)
        output += render_video(url, caption)

    elif block_type == "audio":
        url = get_media_url(payload)
        url = download_media_asset(url, block, assets_dir, markdown_dir)
        caption = get_caption(payload) or "Audio"
        if url:
            output += (
                f'<audio controls preload="metadata" src="{html_attr(url)}">'
                "Your browser does not support the audio tag.</audio>\n\n"
                f"[🔊 {caption} 재생/다운로드]({markdown_url(url)})\n\n"
            )

    elif block_type == "file":
        url = get_media_url(payload)
        url = download_media_asset(url, block, assets_dir, markdown_dir)
        caption = get_caption(payload) or "파일 열기"
        if url:
            output += f"📎 [{caption}]({markdown_url(url)})\n\n"

    elif block_type == "pdf":
        url = get_media_url(payload)
        url = download_media_asset(url, block, assets_dir, markdown_dir)
        caption = get_caption(payload) or "PDF 문서"
        if url:
            # <object> works best on GitHub Pages; GitHub's Markdown sanitizer may remove it.
            output += (
                f'<object data="{html_attr(url)}" type="application/pdf" '
                f'width="100%" height="600px">'
                f'<a href="{html_attr(url)}">{html.escape(caption)} 열기</a>'
                "</object>\n\n"
                f"[📄 {caption} 열기]({markdown_url(url)})\n\n"
            )

    elif block_type in ("embed", "bookmark", "link_preview"):
        url = payload.get("url", "")
        caption = get_caption(payload)
        title = caption or url or "링크 열기"
        if block_type == "embed" and url and youtube_video_id(url):
            output += render_video(url, title)
        elif url:
            # Markdown cannot recreate Notion's rich OGP card, so retain title + URL.
            output += f"> 🔗 **{title}**\n>\n> {markdown_url(url)}\n\n"
            output += f"[링크 열기]({markdown_url(url)})\n\n"

    elif block_type == "divider":
        output += "\n---\n\n"

    elif block_type == "table":
        output += render_table(block)

    elif block_type == "table_row":
        cells = payload.get("cells", [])
        output += " | ".join(extract_text_from_rich_text(cell) for cell in cells) + "\n"

    elif block_type == "table_of_contents":
        output += "[📑 목차: 아래 제목을 기준으로 GitHub에서 자동 목차를 확인하세요.]\n\n"

    elif block_type == "column_list":
        columns = block.get("children", [])
        for index, column in enumerate(columns, start=1):
            output += f"\n> **Column {index}**\n\n"
            output += block_to_markdown(column, depth, assets_dir, markdown_dir, index)
        output += "\n"

    elif block_type == "column":
        for child in block.get("children", []):
            output += block_to_markdown(child, depth, assets_dir, markdown_dir)

    elif block_type in ("child_page", "child_database"):
        title = payload.get("title", "하위 페이지" if block_type == "child_page" else "하위 데이터베이스")
        block_id = block.get("id", "").replace("-", "")
        if block_id:
            notion_url = f"https://www.notion.so/{quote(str(title).replace(' ', '-'))}-{block_id}"
            output += f"📂 [{title}]({notion_url})\n\n"
        else:
            output += f"📂 **{title}**\n\n"

    elif block_type == "link_to_page":
        page_ref = payload.get("page_id") or payload.get("database_id") or ""
        page_ref = str(page_ref).replace("-", "")
        if page_ref:
            output += f"[Notion 페이지 열기](https://www.notion.so/{page_ref})\n\n"

    elif block_type == "breadcrumb":
        output += "🏠 *Notion 페이지 경로*\n\n"

    elif block_type == "synced_block":
        # Original synced blocks usually expose children; synced copies may only expose source ID.
        if block.get("children"):
            output += "<div>\n\n"
            output += "".join(
                block_to_markdown(child, depth, assets_dir, markdown_dir)
                for child in block["children"]
            )
            output += "\n</div>\n\n"
        else:
            output += "*🔁 동기화된 블록: 원본 Notion 페이지에서 내용을 확인하세요.*\n\n"

    elif block_type in ("template", "tab", "meeting_notes"):
        title = get_block_text(block) or payload.get("title") or {
            "template": "템플릿",
            "tab": "탭",
            "meeting_notes": "회의록",
        }.get(block_type, block_type)
        nested = "".join(
            block_to_markdown(child, depth, assets_dir, markdown_dir)
            for child in block.get("children", [])
        )
        output += f"<details>\n<summary>{title}</summary>\n\n{nested}\n</details>\n\n"

    elif block_type == "unsupported":
        output += "*⚠️ 이 Notion 블록 유형은 현재 API 버전에서 읽을 수 없습니다.*\n\n"

    else:
        # Preserve visible text for block types not explicitly handled yet.
        text_value = get_block_text(block)
        if text_value:
            output += f"{indent}{text_value}\n\n"
        else:
            output += f"*⚠️ 변환하지 못한 Notion 블록: `{block_type}`*\n\n"

    # Generic nested-child fallback for blocks whose branch did not consume children.
    if block.get("children") and block_type not in {
        "paragraph", "heading_1", "heading_2", "heading_3", "heading_4",
        "bulleted_list_item", "numbered_list_item", "to_do", "toggle",
        "table", "column_list", "column", "synced_block", "template", "tab",
        "meeting_notes",
    }:
        for child in block["children"]:
            output += block_to_markdown(child, depth, assets_dir, markdown_dir)

    return output


# =======================================================
# [페이지 저장]
# =======================================================


def get_page_title(page: dict) -> str:
    properties = page.get("properties", {})
    title_property = properties.get(NOTION_PROPERTY_TITLE, {}) or {}
    title_items = title_property.get("title", []) or []
    if title_items:
        return get_rich_text_plain(title_items) or "Untitled"

    # Fallback for a database where its title property has a different name/type.
    for value in properties.values():
        if value.get("type") == "title" or value.get("title"):
            return get_rich_text_plain(value.get("title", [])) or "Untitled"
    return "Untitled"


def save_as_markdown(page: dict, date_str: str) -> tuple[str, str]:
    date_str = str(date_str)[:10]
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        date_obj = datetime.now(timezone(timedelta(hours=TIMEZONE_HOURS)))
        date_str = date_obj.strftime("%Y-%m-%d")

    title = get_page_title(page)
    year = date_obj.strftime("%Y")
    month = date_obj.strftime("%m")
    directory = SAVE_DIR_ROOT / year / month
    directory.mkdir(parents=True, exist_ok=True)

    safe_title = sanitize_filename(title)
    filename = directory / f"{date_str}_{safe_title}.md"
    assets_dir = directory / f"{date_str}_{safe_title}_assets"

    blocks = get_page_blocks(page["id"])
    markdown_content = f"# {title}\n\n"
    markdown_content += f"> 날짜: {date_str}\n"
    markdown_content += f"> 원본 노션: [링크]({page.get('url', '')})\n\n"
    markdown_content += "---\n\n"

    for block in blocks:
        markdown_content += block_to_markdown(
            block,
            depth=0,
            assets_dir=assets_dir if DOWNLOAD_NOTION_MEDIA else None,
            markdown_dir=directory,
        )

    if not DOWNLOAD_NOTION_MEDIA and contains_media(blocks):
        markdown_content += (
            "\n> [!NOTE]\n"
            "> Notion에 업로드된 이미지·PDF·파일의 일부 링크는 일정 시간이 지나면 만료될 수 있습니다. "
            "> 영구 보관이 필요하면 `DOWNLOAD_NOTION_MEDIA=true`로 실행하세요.\n"
        )

    filename.write_text(markdown_content, encoding="utf-8")
    return title, str(filename)


# =======================================================
# [메인 README 자동 업데이트]
# =======================================================


def update_main_readme_by_scanning(reset_mode: str) -> None:
    if not SAVE_DIR_ROOT.exists():
        return

    files_data = []
    for path in SAVE_DIR_ROOT.rglob("*.md"):
        if path.name == README_FILE.name:
            continue
        date_str = path.name[:10]
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        title_part = path.stem[11:].replace("_", " ")
        relative_path = "./" + quote(path.as_posix(), safe="/._-")
        files_data.append({
            "date": date_obj,
            "date_str": date_str,
            "title": title_part,
            "path": relative_path,
        })

    files_data.sort(key=lambda item: item["date"], reverse=True)
    grouped: dict[str, list[dict]] = {}
    for item in files_data:
        month_key = item["date"].strftime("%Y %m")
        grouped.setdefault(month_key, []).append(item)

    new_content = ""
    for index, (month, items) in enumerate(grouped.items()):
        if index == 0:
            new_content += f"### {month}\n"
            for item in items:
                new_content += f"- [{item['date_str']} : {item['title']}]({item['path']})\n"
            new_content += "\n"
        else:
            new_content += f"<details>\n<summary>{month} ({len(items)})</summary>\n\n"
            for item in items:
                new_content += f"- [{item['date_str']} : {item['title']}]({item['path']})\n"
            new_content += "\n</details>\n\n"

    if reset_mode == "true" or not README_FILE.exists():
        README_FILE.write_text(DEFAULT_README_TEMPLATE, encoding="utf-8")

    readme_text = README_FILE.read_text(encoding="utf-8")
    start_idx = readme_text.find(MARKER_START)
    end_idx = readme_text.find(MARKER_END)

    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        final_content = (
            readme_text.rstrip()
            + f"\n\n{MARKER_START}\n{new_content}{MARKER_END}\n"
        )
    else:
        final_content = (
            readme_text[: start_idx + len(MARKER_START)]
            + "\n"
            + new_content
            + readme_text[end_idx:]
        )

    README_FILE.write_text(final_content, encoding="utf-8")


# =======================================================
# [Notion 데이터베이스 조회 및 실행]
# =======================================================


def get_query_endpoint() -> str:
    """Resolve the query endpoint for the configured Notion API version.

    Notion API versions from 2025-09-03 split a database container from its
    data source(s). For older API versions, the database query endpoint remains
    in use. When a database has multiple data sources, configure
    NOTION_DATA_SOURCE_ID explicitly to select the TIL data source.
    """
    version_date = datetime.strptime(NOTION_VERSION, "%Y-%m-%d").date()
    if version_date < datetime(2025, 9, 3).date():
        return f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"

    if NOTION_DATA_SOURCE_ID:
        data_source_id = NOTION_DATA_SOURCE_ID
    else:
        database = request_json(
            "GET", f"https://api.notion.com/v1/databases/{DATABASE_ID}"
        )
        data_sources = database.get("data_sources", [])
        if len(data_sources) == 1:
            data_source_id = data_sources[0]["id"]
        elif not data_sources:
            raise RuntimeError(
                "이 Notion 데이터베이스에서 data_sources를 찾지 못했습니다. "
                "NOTION_VERSION 또는 DATABASE_ID를 확인하세요."
            )
        else:
            choices = ", ".join(
                f"{item.get('name', 'Unnamed')}={item.get('id')}" for item in data_sources
            )
            raise RuntimeError(
                "이 데이터베이스에는 여러 Data Source가 있습니다. "
                "TIL 테이블의 ID를 확인해 GitHub Actions 변수 NOTION_DATA_SOURCE_ID에 "
                f"설정하세요. 선택지: {choices}"
            )

    return f"https://api.notion.com/v1/data_sources/{data_source_id}/query"


def contains_media(blocks: list[dict]) -> bool:
    media_types = {"image", "video", "audio", "file", "pdf"}
    for block in blocks:
        if block.get("type") in media_types:
            return True
        if contains_media(block.get("children", []) or []):
            return True
    return False


def main() -> None:
    if not NOTION_TOKEN:
        raise RuntimeError("환경변수 NOTION_TOKEN이 설정되지 않았습니다.")
    if not DATABASE_ID:
        raise RuntimeError("환경변수 NOTION_DATABASE_ID가 설정되지 않았습니다.")

    fetch_mode = os.environ.get("FETCH_MODE", "DAILY").upper()
    reset_mode = os.environ.get("RESET_MODE", "false").lower()
    url = get_query_endpoint()
    payload: dict = {}

    if fetch_mode == "ALL":
        print(">> [INFO] 전체 데이터 동기화 시작")
    else:
        kst = timezone(timedelta(hours=TIMEZONE_HOURS))
        target_date = (datetime.now(kst) - timedelta(days=1)).strftime("%Y-%m-%d")
        print(f">> [INFO] 대상 날짜: {target_date}")
        payload["filter"] = {
            "property": NOTION_PROPERTY_DATE,
            "date": {"equals": target_date},
        }

    has_more = True
    next_cursor = None
    while has_more:
        if next_cursor:
            payload["start_cursor"] = next_cursor
        else:
            payload.pop("start_cursor", None)

        data = request_json("POST", url, json=payload)
        pages = data.get("results", [])

        for page in pages:
            props = page.get("properties", {})
            date_property = props.get(NOTION_PROPERTY_DATE, {}) or {}
            date_value = date_property.get("date") or {}
            page_date = date_value.get("start")
            if not page_date:
                continue

            title, filepath = save_as_markdown(page, page_date)
            print(f"[SUCCESS] {page_date[:10]} : {title} -> {filepath}")

        has_more = data.get("has_more", False)
        next_cursor = data.get("next_cursor")

    update_main_readme_by_scanning(reset_mode)
    print("[DONE] README.md 업데이트 완료")


if __name__ == "__main__":
    main()
