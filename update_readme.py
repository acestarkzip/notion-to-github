"""Notion TIL -> GitHub Markdown exporter.

Required environment variables:
    NOTION_TOKEN          Notion integration token
    NOTION_DATABASE_ID    Database ID containing TIL pages

Optional environment variables:
    FETCH_MODE            DAILY (default) or ALL
    RESET_MODE            true / false (default false)

The script intentionally keeps the original Notion API version (2022-06-28)
so that the existing /v1/databases/{id}/query workflow remains compatible.
"""

from __future__ import annotations

import mimetypes
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import requests


# =======================================================
# [사용자 설정 영역]
# =======================================================

SAVE_DIR_ROOT = Path("TIL")
NOTION_PROPERTY_TITLE = "제목"
NOTION_PROPERTY_DATE = "날짜"
README_FILE = Path("README.md")
TIMEZONE_HOURS = 9

# README에서 글 목록이 자동으로 교체될 영역
MARKER_START = "<!-- TIL_LIST_START -->"
MARKER_END = "<!-- TIL_LIST_END -->"

# 각 TIL 문서에 목차를 자동으로 생성합니다.
GENERATE_TABLE_OF_CONTENTS = True
TOC_TITLE = "## 📑 목차"

# Notion 파일을 GitHub 저장소에 내려받을 때 사용할 설정
DOWNLOAD_NOTION_FILES = True
ASSET_FOLDER_SUFFIX = "_assets"
MAX_DOWNLOAD_SIZE_MB = 100  # 100MB를 초과하는 파일은 다운로드하지 않고 원본 링크 사용
REQUEST_TIMEOUT_SECONDS = 30
MAX_API_RETRIES = 4
MAX_BLOCK_DEPTH = 30
API_MIN_INTERVAL_SECONDS = 0.35  # Notion API 요청 속도 제한을 고려

DEFAULT_README_TEMPLATE = f"""# 📝 My TIL Collection

노션에서 작성된 TIL(Today I Learned)이 자동으로 업로드되는 저장소입니다.

## 📚 글 목록
{MARKER_START}
{MARKER_END}
"""


# =======================================================
# [Notion API 설정]
# =======================================================

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    # 기존 자동화와의 호환성을 위해 유지합니다.
    "Notion-Version": "2022-06-28",
}

SESSION = requests.Session()
_LAST_API_REQUEST_AT = 0.0


# =======================================================
# [공통 유틸리티]
# =======================================================

def log(message: str) -> None:
    print(message, flush=True)


def notion_request(method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
    """Notion API 요청. 속도 제한, 재시도, HTTP 오류를 처리합니다."""
    global _LAST_API_REQUEST_AT

    if not NOTION_TOKEN:
        raise RuntimeError("환경 변수 NOTION_TOKEN이 설정되지 않았습니다.")

    for attempt in range(MAX_API_RETRIES):
        elapsed = time.monotonic() - _LAST_API_REQUEST_AT
        if elapsed < API_MIN_INTERVAL_SECONDS:
            time.sleep(API_MIN_INTERVAL_SECONDS - elapsed)

        try:
            response = SESSION.request(
                method,
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
            _LAST_API_REQUEST_AT = time.monotonic()

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_API_RETRIES - 1:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                wait_seconds = float(retry_after) if retry_after else min(2 ** attempt, 15)
                log(f">> [WARN] Notion API 응답 {response.status_code}. {wait_seconds:.1f}초 후 재시도합니다.")
                time.sleep(wait_seconds)
                continue

            response.raise_for_status()
            return response.json()

        except requests.RequestException:
            if attempt == MAX_API_RETRIES - 1:
                raise
            time.sleep(min(2 ** attempt, 15))

    raise RuntimeError("Notion API 요청에 실패했습니다.")


def get_paginated_results(
    url: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Notion API의 has_more / next_cursor를 끝까지 따라가 결과를 반환합니다."""
    results: List[Dict[str, Any]] = []
    request_payload = dict(payload or {})

    while True:
        data = notion_request(method, url, json=request_payload) if method.upper() != "GET" else notion_request(
            "GET", url, params=request_payload or None
        )
        results.extend(data.get("results", []))

        if not data.get("has_more") or not data.get("next_cursor"):
            break

        request_payload["start_cursor"] = data["next_cursor"]

    return results


def plain_text(rich_text_list: Optional[List[Dict[str, Any]]]) -> str:
    """Notion rich_text 배열에서 서식 없는 텍스트를 추출합니다."""
    parts: List[str] = []
    for item in rich_text_list or []:
        if item.get("plain_text") is not None:
            parts.append(str(item["plain_text"]))
        elif item.get("text", {}).get("content") is not None:
            parts.append(str(item["text"]["content"]))
        elif item.get("equation", {}).get("expression") is not None:
            parts.append(str(item["equation"]["expression"]))
    return "".join(parts)


def rich_text_to_markdown(rich_text_list: Optional[List[Dict[str, Any]]]) -> str:
    """굵게/기울임/취소선/밑줄/인라인 코드/하이퍼링크를 Markdown으로 변환합니다."""
    output: List[str] = []

    for item in rich_text_list or []:
        text_value = str(item.get("plain_text", item.get("text", {}).get("content", "")))
        if not text_value:
            continue

        annotations = item.get("annotations", {}) or {}
        if annotations.get("code"):
            value = text_value.replace("`", "\\`")
            longest = max((len(match.group(0)) for match in re.finditer(r"`+", value)), default=0)
            fence = "`" * (longest + 1)
            value = f"{fence}{value}{fence}"
        else:
            value = text_value
            if annotations.get("bold"):
                value = f"**{value}**"
            if annotations.get("italic"):
                value = f"*{value}*"
            if annotations.get("strikethrough"):
                value = f"~~{value}~~"
            if annotations.get("underline"):
                value = f"<u>{value}</u>"

        href = item.get("href") or item.get("text", {}).get("link", {}).get("url")
        if href:
            value = f"[{value}]({href})"

        # Notion의 글자색/배경색은 GitHub Markdown에서 동일하게 재현되지 않아 텍스트는 유지합니다.
        output.append(value)

    return "".join(output)


def get_block_rich_text(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    block_data = block.get(block.get("type", ""), {}) or {}
    return block_data.get("rich_text", []) or []


def sanitize_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    clean = re.sub(r"\s+", "_", clean)
    clean = clean.strip("._")
    return clean[:150] or "untitled"


def github_slug(text: str) -> str:
    """GitHub heading anchor에 가까운 slug를 생성합니다(한글 포함)."""
    value = unicodedata.normalize("NFKC", text).lower().strip()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    value = value.replace("_", "")  # GitHub 섹션 앵커 규칙에 맞춰 문장부호 제거
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "section"


def get_title_from_page(page: Dict[str, Any]) -> str:
    properties = page.get("properties", {})
    title_property = properties.get(NOTION_PROPERTY_TITLE, {})
    title_items = title_property.get("title", [])
    title = plain_text(title_items).strip()

    if not title:
        for prop in properties.values():
            if prop.get("type") == "title" or "title" in prop:
                title = plain_text(prop.get("title", [])).strip()
                if title:
                    break

    return title or "제목없음"


def get_page_date(page: Dict[str, Any]) -> Optional[str]:
    date_property = page.get("properties", {}).get(NOTION_PROPERTY_DATE, {})
    date_value = date_property.get("date") or {}
    start = date_value.get("start")
    return str(start)[:10] if start else None


# =======================================================
# [Notion 블록 조회: 페이지네이션 + 중첩 블록]
# =======================================================

def get_page_blocks(block_id: str) -> List[Dict[str, Any]]:
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    return get_paginated_results(url, method="GET", payload={"page_size": 100})


def load_block_tree(
    blocks: List[Dict[str, Any]],
    depth: int = 0,
    visited: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """has_children인 블록의 하위 블록을 재귀적으로 조회합니다."""
    if visited is None:
        visited = set()
    if depth > MAX_BLOCK_DEPTH:
        log(f">> [WARN] 하위 블록 깊이가 {MAX_BLOCK_DEPTH}를 초과하여 일부 조회를 생략했습니다.")
        return blocks

    for block in blocks:
        block_id = block.get("id")
        if block.get("has_children") and block_id and block_id not in visited:
            visited.add(block_id)
            try:
                children = get_page_blocks(block_id)
                block["_children"] = load_block_tree(children, depth + 1, visited)
            except requests.RequestException as exc:
                log(f">> [WARN] 하위 블록 조회 실패 ({block_id}): {exc}")
                block["_children"] = []
        else:
            block["_children"] = []

    return blocks


# =======================================================
# [파일/이미지 다운로드]
# =======================================================

def extension_from_url_or_type(url: str, content_type: str = "") -> str:
    path_ext = Path(unquote(urlparse(url).path)).suffix
    if path_ext and len(path_ext) <= 10:
        return path_ext
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    return guessed or ".bin"


def download_notion_file(
    url: str,
    block_id: str,
    asset_dir: Path,
    preferred_name: str = "",
) -> Optional[str]:
    """Notion이 발급한 임시 파일 URL을 저장하고 Markdown 상대 경로를 반환합니다."""
    if not DOWNLOAD_NOTION_FILES or not url:
        return None
    if not url.startswith("https://"):
        return None

    asset_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9]", "", block_id)[:10] or "asset"

    try:
        with SESSION.get(url, stream=True, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_DOWNLOAD_SIZE_MB * 1024 * 1024:
                log(f">> [WARN] 파일이 {MAX_DOWNLOAD_SIZE_MB}MB를 초과하여 다운로드하지 않습니다: {preferred_name or url}")
                return None

            extension = extension_from_url_or_type(url, content_type)
            base_name = sanitize_filename(preferred_name) if preferred_name else "file"
            if Path(base_name).suffix:
                extension = Path(base_name).suffix
                base_name = Path(base_name).stem
            filename = f"{safe_id}_{base_name}{extension}"
            destination = asset_dir / filename

            total = 0
            try:
                with destination.open("wb") as file_handle:
                    for chunk in response.iter_content(chunk_size=1024 * 64):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_SIZE_MB * 1024 * 1024:
                            log(f">> [WARN] 다운로드 중 파일 크기 제한을 초과했습니다: {preferred_name or url}")
                            raise ValueError("파일 크기 제한 초과")
                        file_handle.write(chunk)
            except Exception:
                destination.unlink(missing_ok=True)
                raise

            return f"./{asset_dir.name}/{quote(filename)}"

    except (requests.RequestException, OSError, ValueError) as exc:
        log(f">> [WARN] 파일 다운로드 실패. 원본 링크를 사용합니다: {exc}")
        return None


def get_media_source(media_data: Dict[str, Any]) -> Tuple[str, str]:
    """미디어 블록에서 URL과 유형(file/external)을 반환합니다."""
    for source_type in ("file", "external"):
        source = media_data.get(source_type) or {}
        if source.get("url"):
            return source["url"], source_type
    return "", ""


def youtube_video_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().replace("www.", "")
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
        elif host in {"youtube.com", "m.youtube.com", "youtube-nocookie.com"}:
            if parsed.path == "/watch":
                from urllib.parse import parse_qs
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            else:
                parts = parsed.path.strip("/").split("/")
                video_id = parts[1] if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live"} else ""
        else:
            return None
        return video_id if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id or "") else None
    except Exception:
        return None


def youtube_markdown(url: str, title: str = "YouTube 영상") -> str:
    video_id = youtube_video_id(url)
    if not video_id:
        return f"[▶ {title}]({url})\n\n"
    thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    return f"[![{title}]({thumbnail})]({url})\n\n"


# =======================================================
# [목차 생성]
# =======================================================

def collect_headings(
    blocks: Iterable[Dict[str, Any]],
    used_slugs: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """본문의 heading_1~3을 찾아 GitHub 링크용 slug를 고유하게 만듭니다."""
    if used_slugs is None:
        used_slugs = {}

    headings: List[Dict[str, Any]] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type in {"heading_1", "heading_2", "heading_3"}:
            content = plain_text(get_block_rich_text(block)).strip()
            if content:
                base = github_slug(content)
                duplicate_count = used_slugs.get(base, 0)
                used_slugs[base] = duplicate_count + 1
                slug = base if duplicate_count == 0 else f"{base}-{duplicate_count}"
                headings.append({
                    "id": block.get("id"),
                    "level": int(block_type[-1]),
                    "title": content,
                    "slug": slug,
                })

        headings.extend(collect_headings(block.get("_children", []), used_slugs))

    return headings


def build_table_of_contents(headings: List[Dict[str, Any]]) -> str:
    if not GENERATE_TABLE_OF_CONTENTS or not headings:
        return ""

    min_level = min(item["level"] for item in headings)
    lines = [TOC_TITLE, ""]
    for item in headings:
        indent = "  " * max(0, item["level"] - min_level)
        lines.append(f"{indent}- [{item['title']}](#{item['slug']})")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# =======================================================
# [Notion 블록 -> Markdown 변환]
# =======================================================

def fenced_code(language: str, content: str) -> str:
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", content)), default=2)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language or 'text'}\n{content}\n{fence}\n\n"


def get_caption(media_data: Dict[str, Any]) -> str:
    return plain_text(media_data.get("caption", [])).strip()


def render_file_block(
    block: Dict[str, Any],
    block_type: str,
    asset_dir: Path,
) -> str:
    data = block.get(block_type, {}) or {}
    url, source_type = get_media_source(data)
    caption = get_caption(data)
    title = caption or data.get("name") or f"{block_type.upper()} 파일"

    local_path = None
    if source_type == "file":
        local_path = download_notion_file(url, block.get("id", ""), asset_dir, title)

    target = local_path or url
    if not target:
        return f"> [파일을 가져오지 못했습니다: {title}]\n\n"

    if block_type == "image":
        return f"![{caption or 'Image'}]({target})\n\n"

    label = "PDF 문서 열기" if block_type == "pdf" else title
    if block_type == "video" and url and youtube_video_id(url):
        return youtube_markdown(url, title)
    return f"[📎 {label}]({target})\n\n"


def render_table(block: Dict[str, Any]) -> str:
    rows = block.get("_children", [])
    if not rows:
        return ""

    table_options = block.get("table", {}) or {}
    has_column_header = bool(table_options.get("has_column_header"))
    parsed_rows: List[List[str]] = []

    for row in rows:
        if row.get("type") != "table_row":
            continue
        cells = row.get("table_row", {}).get("cells", [])
        parsed_rows.append([
            rich_text_to_markdown(cell).replace("|", "\\|").replace("\n", "<br>")
            for cell in cells
        ])

    if not parsed_rows:
        return ""

    column_count = max(len(row) for row in parsed_rows)
    parsed_rows = [row + [""] * (column_count - len(row)) for row in parsed_rows]
    output = ["| " + " | ".join(parsed_rows[0]) + " |"]

    if has_column_header:
        output.append("| " + " | ".join(["---"] * column_count) + " |")
        data_rows = parsed_rows[1:]
    else:
        # GitHub Markdown 표는 헤더 행이 필요하므로 첫 행을 헤더로 사용합니다.
        output.append("| " + " | ".join(["---"] * column_count) + " |")
        data_rows = parsed_rows[1:]

    for row in data_rows:
        output.append("| " + " | ".join(row) + " |")

    return "\n".join(output) + "\n\n"


def render_blocks(
    blocks: Iterable[Dict[str, Any]],
    asset_dir: Path,
    indent: int = 0,
) -> str:
    output: List[str] = []
    indent_text = " " * indent

    for block in blocks:
        block_type = block.get("type", "")
        data = block.get(block_type, {}) or {}
        rich = data.get("rich_text", []) or []
        content = rich_text_to_markdown(rich)
        children = block.get("_children", []) or []

        if block_type == "paragraph":
            if content.strip():
                output.append(f"{indent_text}{content}\n\n")
            elif children:
                output.append(render_blocks(children, asset_dir, indent))

        elif block_type in {"heading_1", "heading_2", "heading_3"}:
            level = int(block_type[-1])
            # GitHub가 Markdown 제목에서 자동 생성하는 anchor를 사용합니다.
            output.append(f"{indent_text}{'#' * level} {content}\n\n")
            if children:
                output.append(render_blocks(children, asset_dir, indent))

        elif block_type == "bulleted_list_item":
            output.append(f"{indent_text}- {content}\n")
            if children:
                output.append(render_blocks(children, asset_dir, indent + 2))

        elif block_type == "numbered_list_item":
            output.append(f"{indent_text}1. {content}\n")
            if children:
                output.append(render_blocks(children, asset_dir, indent + 3))

        elif block_type == "to_do":
            checked = "x" if data.get("checked") else " "
            output.append(f"{indent_text}- [{checked}] {content}\n")
            if children:
                output.append(render_blocks(children, asset_dir, indent + 2))

        elif block_type == "toggle":
            output.append(f"{indent_text}<details>\n{indent_text}<summary>{content or '자세히 보기'}</summary>\n\n")
            if children:
                output.append(render_blocks(children, asset_dir, indent))
            output.append(f"{indent_text}</details>\n\n")

        elif block_type == "quote":
            quote_lines = (content or " ").splitlines() or [" "]
            output.append("\n".join(f"{indent_text}> {line}" for line in quote_lines) + "\n\n")

        elif block_type == "callout":
            icon_data = data.get("icon", {}) or {}
            icon = icon_data.get("emoji") or "💡"
            quote_lines = (content or " ").splitlines() or [" "]
            output.append("\n".join(f"{indent_text}> {icon} {line}" for line in quote_lines) + "\n\n")
            if children:
                output.append(render_blocks(children, asset_dir, indent))

        elif block_type == "code":
            language = data.get("language", "text")
            code_content = plain_text(data.get("rich_text", []))
            output.append(fenced_code(language, code_content))

        elif block_type == "image":
            output.append(render_file_block(block, "image", asset_dir))

        elif block_type in {"file", "pdf", "audio", "video"}:
            output.append(render_file_block(block, block_type, asset_dir))

        elif block_type == "embed":
            url = data.get("url", "")
            title = get_caption(data) or "외부 콘텐츠"
            if url and youtube_video_id(url):
                output.append(youtube_markdown(url, title))
            elif url:
                output.append(f"[🔗 {title}]({url})\n\n")

        elif block_type == "bookmark":
            url = data.get("url", "")
            caption = get_caption(data)
            title = caption or url or "북마크"
            if url and youtube_video_id(url):
                output.append(youtube_markdown(url, title))
            elif url:
                output.append(f"[🔗 {title}]({url})\n\n")

        elif block_type == "link_preview":
            url = data.get("url", "")
            if url:
                if youtube_video_id(url):
                    output.append(youtube_markdown(url))
                else:
                    output.append(f"[🔗 {url}]({url})\n\n")

        elif block_type == "table":
            output.append(render_table(block))

        elif block_type in {"column_list", "column", "synced_block"}:
            if children:
                output.append(render_blocks(children, asset_dir, indent))
                if block_type in {"column", "column_list"}:
                    output.append("\n")

        elif block_type == "equation":
            expression = data.get("expression", "")
            if expression:
                output.append(f"$$\n{expression}\n$$\n\n")

        elif block_type == "child_page":
            child_title = data.get("title", "하위 페이지")
            child_url = f"https://www.notion.so/{block.get('id', '').replace('-', '')}"
            output.append(f"[📄 {child_title}]({child_url})\n\n")
            if children:
                output.append(render_blocks(children, asset_dir, indent))

        elif block_type == "child_database":
            db_title = data.get("title", "하위 데이터베이스")
            output.append(f"> 📚 하위 데이터베이스: **{db_title}**\n\n")

        elif block_type == "link_to_page":
            page_ref = data.get("page_id") or data.get("database_id") or ""
            page_url = f"https://www.notion.so/{page_ref.replace('-', '')}" if page_ref else ""
            if page_url:
                output.append(f"[Notion 페이지 열기]({page_url})\n\n")

        elif block_type == "divider":
            output.append("---\n\n")

        elif block_type in {"table_of_contents", "breadcrumb", "table_row"}:
            # Notion 목차 블록은 위에서 heading을 수집해 GitHub용 목차로 다시 생성합니다.
            continue

        elif block_type == "template":
            if content:
                output.append(f"{indent_text}{content}\n\n")
            if children:
                output.append(render_blocks(children, asset_dir, indent))

        else:
            # 알 수 없는 블록이라도 완전히 조용히 버리지 않고 유형을 기록합니다.
            output.append(f"<!-- Notion block type not yet converted: {block_type or 'unknown'} -->\n\n")
            if children:
                output.append(render_blocks(children, asset_dir, indent))

    return "".join(output)


# =======================================================
# [Notion 페이지 저장]
# =======================================================

def save_as_markdown(page: Dict[str, Any], date_str: str) -> Tuple[str, Path]:
    date_str = str(date_str)[:10]
    title = get_title_from_page(page)
    page_id = page["id"]

    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    directory = SAVE_DIR_ROOT / date_obj.strftime("%Y") / date_obj.strftime("%m")
    directory.mkdir(parents=True, exist_ok=True)

    safe_title = sanitize_filename(title)
    filename = directory / f"{date_str}_{safe_title}.md"
    asset_dir = directory / f"{date_str}_{safe_title}{ASSET_FOLDER_SUFFIX}"

    log(f">> [INFO] Notion 블록 조회: {title}")
    blocks = load_block_tree(get_page_blocks(page_id))

    # Markdown 문서에 먼저 출력되는 페이지 제목과 목차 제목도
    # GitHub의 중복 anchor 번호 계산에 포함합니다.
    used_slugs: Dict[str, int] = {}
    for initial_heading in (title, re.sub(r"^#+\s*", "", TOC_TITLE)):
        if initial_heading:
            initial_slug = github_slug(initial_heading)
            used_slugs[initial_slug] = used_slugs.get(initial_slug, 0) + 1
    headings = collect_headings(blocks, used_slugs)

    markdown_content = f"# {title}\n\n"
    markdown_content += f"> 날짜: {date_str}\n"
    markdown_content += f"> 원본 노션: [링크]({page.get('url', '')})\n\n"
    markdown_content += "---\n\n"
    markdown_content += build_table_of_contents(headings)
    markdown_content += render_blocks(blocks, asset_dir)

    # 반복 배포 시 오래된 파일이 남지 않도록 빈 자산 폴더는 만들지 않습니다.
    filename.write_text(markdown_content.rstrip() + "\n", encoding="utf-8")

    return title, filename


# =======================================================
# [메인 README 목록 생성]
# =======================================================

def extract_markdown_title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem[11:].replace("_", " ") if len(path.stem) > 11 else path.stem


def update_main_readme_by_scanning(reset_mode: str) -> None:
    if not SAVE_DIR_ROOT.exists():
        log(">> [INFO] TIL 디렉터리가 없어 README 목록을 갱신하지 않습니다.")
        return

    files_data: List[Dict[str, Any]] = []
    for path in SAVE_DIR_ROOT.rglob("*.md"):
        date_str = path.name[:10]
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        files_data.append({
            "date": date_obj,
            "date_str": date_str,
            "title": extract_markdown_title(path),
            "path": path,
        })

    files_data.sort(key=lambda item: item["date"], reverse=True)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in files_data:
        month_key = item["date"].strftime("%Y년 %m월")
        grouped.setdefault(month_key, []).append(item)

    new_content_parts: List[str] = []
    for index, (month, items) in enumerate(grouped.items()):
        lines = [f"### {month}"]
        for item in items:
            relative_path = item["path"].as_posix()
            safe_path = quote(relative_path, safe="/._-")
            lines.append(f"- [{item['date_str']} : {item['title']}](./{safe_path})")

        month_content = "\n".join(lines) + "\n"
        if index == 0:
            new_content_parts.append(month_content)
        else:
            new_content_parts.append(
                f"<details>\n<summary>{month} ({len(items)}개)</summary>\n\n"
                f"{month_content}\n</details>\n"
            )

    new_content = "\n".join(new_content_parts).strip()

    if reset_mode == "true" or not README_FILE.exists():
        log(f">> [INFO] README.md 템플릿을 초기화합니다. (RESET_MODE: {reset_mode})")
        README_FILE.write_text(DEFAULT_README_TEMPLATE, encoding="utf-8")

    readme_text = README_FILE.read_text(encoding="utf-8")
    start_idx = readme_text.find(MARKER_START)
    end_idx = readme_text.find(MARKER_END)

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        content_start = start_idx + len(MARKER_START)
        final_content = (
            readme_text[:content_start]
            + "\n"
            + new_content
            + "\n"
            + readme_text[end_idx:]
        )
    else:
        # 기존 README에 마커가 없거나 손상된 경우, 파일 끝에 새 마커 영역을 추가합니다.
        separator = "\n" if readme_text.endswith("\n") else "\n\n"
        final_content = (
            readme_text
            + separator
            + MARKER_START
            + "\n"
            + new_content
            + "\n"
            + MARKER_END
            + "\n"
        )

    README_FILE.write_text(final_content, encoding="utf-8")
    log(f">> [INFO] README.md 목록 갱신 완료: {len(files_data)}개 문서")


# =======================================================
# [데이터베이스 조회 및 실행]
# =======================================================

def query_database(fetch_mode: str) -> List[Dict[str, Any]]:
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    payload: Dict[str, Any] = {"page_size": 100}

    if fetch_mode.upper() == "ALL":
        log(">> [모드: 전체] 모든 데이터를 조회합니다.")
    else:
        kst = timezone(timedelta(hours=TIMEZONE_HOURS))
        target_date = (datetime.now(kst) - timedelta(days=1)).strftime("%Y-%m-%d")
        log(f">> [모드: 일간] {target_date} 데이터를 조회합니다.")
        payload["filter"] = {
            "property": NOTION_PROPERTY_DATE,
            "date": {"equals": target_date},
        }

    return get_paginated_results(url, method="POST", payload=payload)


def main() -> None:
    global NOTION_TOKEN, DATABASE_ID, HEADERS

    NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
    DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
    HEADERS = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    if not NOTION_TOKEN:
        raise SystemExit("[ERROR] GitHub Secrets에 NOTION_TOKEN이 설정되어 있는지 확인하세요.")
    if not DATABASE_ID:
        raise SystemExit("[ERROR] GitHub Secrets에 NOTION_DATABASE_ID가 설정되어 있는지 확인하세요.")

    fetch_mode = os.environ.get("FETCH_MODE", "DAILY").upper()
    reset_mode = os.environ.get("RESET_MODE", "false").lower()

    if fetch_mode not in {"DAILY", "ALL"}:
        log(f">> [WARN] 알 수 없는 FETCH_MODE={fetch_mode}. DAILY 모드로 실행합니다.")
        fetch_mode = "DAILY"

    pages = query_database(fetch_mode)
    log(f">> [INFO] 조회된 Notion 페이지: {len(pages)}개")

    for page in pages:
        page_date = get_page_date(page)
        if not page_date:
            log(f">> [SKIP] 날짜 속성이 비어 있는 페이지: {get_title_from_page(page)}")
            continue

        try:
            title, filepath = save_as_markdown(page, page_date)
            log(f">> [DONE] 저장 완료 - {page_date} : {title} -> {filepath.as_posix()}")
        except (requests.RequestException, ValueError, KeyError, OSError) as exc:
            log(f">> [ERROR] 페이지 처리 실패 - {get_title_from_page(page)} ({page.get('id')}): {exc}")

    update_main_readme_by_scanning(reset_mode)
    log(">> [DONE] Notion → GitHub Markdown 변환 작업이 완료되었습니다.")


if __name__ == "__main__":
    main()
