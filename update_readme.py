import os
import requests
import re
from datetime import datetime, timezone, timedelta

# =======================================================
# [설정 영역]
# =======================================================

SAVE_DIR_ROOT = "TIL" 
NOTION_PROPERTY_TITLE = "제목"
NOTION_PROPERTY_DATE = "날짜"
README_FILE = "README.md"
MARKER_START = "<!-- START_TIL -->"
MARKER_END = "<!-- END_TIL -->"
TIMEZONE_HOURS = 9 

DEFAULT_README_TEMPLATE = f"""# 📚 My TIL Collection

노션(Today I Learned) 자동화 수집 공간입니다.

## 📌 최근 학습 목록
{MARKER_START}
{MARKER_END}
"""

# =======================================================
# [인증 및 헤더]
# =======================================================
NOTION_TOKEN = os.environ.get('NOTION_TOKEN', '')
DATABASE_ID = os.environ.get('NOTION_DATABASE_ID', '')

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

def get_page_blocks(block_id):
    """
    특정 페이지 또는 블록의 자식 블록들을 재귀적으로 가져오며,
    column_list 등 중첩된 레이아웃 내부의 블록까지 탐색합니다.
    """
    url = f"https://api.com/v1/blocks/{block_id}/children" if False else f"https://api.notion.com/v1/blocks/{block_id}/children"
    response = requests.get(url, headers=headers)
    results = response.json().get('results', [])
    
    blocks = []
    for block in results:
        b_type = block.get('type')
        # 열(Column) 레이아웃이나 토글 등 내부 자식을 가진 블록 처리
        if block.get('has_children') or b_type in ['column_list', 'column', 'toggle']:
            block['children'] = get_page_blocks(block['id'])
        blocks.append(block)
    return blocks

def extract_text_from_rich_text(rich_text_list):
    """
    노션 텍스트 색상(color) 및 스타일(annotation)을 반영하여 마크다운/HTML로 변환합니다.
    """
    content = ""
    for text in rich_text_list:
        plain = text.get('plain_text', '')
        href = text.get('href')
        annotations = text.get('annotations', {})
        
        # 스타일 적용 (굵기, 기울임, 밑줄 등)
        if annotations.get('bold'):
            plain = f"**{plain}**"
        if annotations.get('italic'):
            plain = f"*{plain}*"
        if annotations.get('strikethrough'):
            plain = f"~~{plain}~~"
        if annotations.get('code'):
            plain = f"`{plain}`"
            
        # 노션 글자 색상 및 배경색 처리 (color가 default가 아닌 경우)
        color = annotations.get('color', 'default')
        if color != 'default':
            if '_background' in color:
                # 배경색 처리 (예: gray_background -> background-color)
                bg_color = color.replace('_background', '')
                plain = f"<mark style='background-color: {bg_color};'>{plain}</mark>"
            else:
                # 글자 색상 처리
                plain = f"<span style='color: {color};'>{plain}</span>"

        if href:
            content += f"[{plain}]({href})"
        else:
            content += plain
    return content

def block_to_markdown(block, depth=0):
    b_type = block['type']
    indent = "  " * depth  
    markdown_output = ""

    if b_type in ['paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item', 'numbered_list_item', 'to_do', 'toggle']:
        rich_text = block[b_type].get('rich_text', [])
        content = extract_text_from_rich_text(rich_text)
        
        if b_type == 'paragraph': 
            if content.strip():
                markdown_output += f"{indent}{content}\n\n"
        elif b_type == 'heading_1': 
            markdown_output += f"\n# {content}\n\n"
        elif b_type == 'heading_2': 
            markdown_output += f"\n## {content}\n\n"
        elif b_type == 'heading_3': 
            markdown_output += f"\n### {content}\n\n"
        elif b_type == 'bulleted_list_item': 
            markdown_output += f"{indent}- {content}\n"
        elif b_type == 'numbered_list_item': 
            markdown_output += f"{indent}1. {content}\n"
        elif b_type == 'to_do':
            checked = "[x]" if block['to_do']['checked'] else "[ ]"
            markdown_output += f"{indent}- {checked} {content}\n"
        elif b_type == 'toggle': 
            markdown_output += f"{indent}- 📂 {content}\n"
            
    elif b_type == 'quote':
        rich_text = block['quote'].get('rich_text', [])
        content = extract_text_from_rich_text(rich_text)
        markdown_output += f"{indent}> {content}\n\n"
        
    elif b_type == 'callout':
        rich_text = block['callout'].get('rich_text', [])
        content = extract_text_from_rich_text(rich_text)
        icon = block['callout'].get('icon', {})
        icon_emoji = icon.get('emoji', '💡') if icon.get('type') == 'emoji' else '💡'
        # GitHub Preview에서 눈에 띄는 콜아웃 박스 형태로 변환
        markdown_output += f"\n> {icon_emoji} **{content}**\n\n"
        
    elif b_type == 'code':
        language = block['code'].get('language', 'text')
        rich_text = block['code'].get('rich_text', [])
        content = extract_text_from_rich_text(rich_text)
        markdown_output += f"{indent}```{language}\n{content}\n{indent}```\n\n"
        
    elif b_type == 'image':
        image_info = block['image']
        url = image_info.get('file', {}).get('url') or image_info.get('external', {}).get('url') or ""
        markdown_output += f"{indent}![Image]({url})\n\n"
        
    elif b_type in ['video', 'embed']:
        # 비디오 및 임베드 링크 처리
        v_info = block.get(b_type, {})
        url = v_info.get('url', '')
        markdown_output += f"{indent}🎥 [Watch Video / Embed]({url})\n\n"
        
    elif b_type == 'bookmark':
        # 북마크 링크 처리
        b_info = block.get('bookmark', {})
        url = b_info.get('url', '')
        caption_text = extract_text_from_rich_text(b_info.get('caption', []))
        display_text = caption_text if caption_text else url
        markdown_output += f"{indent}🔗 [{display_text}]({url})\n\n"
        
    elif b_type == 'pdf':
        # PDF 파일 처리
        pdf_info = block.get('pdf', {})
        url = pdf_info.get('file', {}).get('url') or pdf_info.get('external', {}).get('url') or ""
        markdown_output += f"{indent}📄 [View PDF Document]({url})\n\n"
        
    elif b_type == 'divider': 
        markdown_output += f"\n---\n\n"

    # 중첩된 자식 블록(Column 레이아웃, 토글 내용 등) 재귀 처리
    if block.get('children'):
        for child in block['children']:
            markdown_output += block_to_markdown(child, depth + (1 if b_type in ['column', 'toggle'] else 0))

    return markdown_output

def sanitize_filename(title):
    clean_name = re.sub(r'[\\/*?:"<>|]', "", title)
    clean_name = clean_name.replace(" ", "_")
    return clean_name

def save_as_markdown(page, date_str):
    if len(date_str) > 10:
        date_str = date_str[:10]

    page_id = page['id']
    try:
        title = page['properties'][NOTION_PROPERTY_TITLE]['title'][0]['text']['content']
    except:
        title = "Untitled"
    
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    except:
        date_obj = datetime.now()
        date_str = date_obj.strftime("%Y-%m-%d")
        
    year = date_obj.strftime("%Y")
    month = date_obj.strftime("%m")
    
    directory = f"{SAVE_DIR_ROOT}/{year}/{month}"
    os.makedirs(directory, exist_ok=True)
    
    safe_title = sanitize_filename(title)
    filename = f"{directory}/{date_str}_{safe_title}.md"
    
    blocks = get_page_blocks(page_id)
    markdown_content = f"# {title}\n\n"
    markdown_content += f"> 날짜: {date_str}\n"
    markdown_content += f"> 원본 노션: [링크]({page['url']})\n\n"
    markdown_content += "---\n\n"
    
    for block in blocks:
        markdown_content += block_to_markdown(block)
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(markdown_content)
    
    return title, filename

def update_main_readme_by_scanning(reset_mode):
    if not os.path.exists(SAVE_DIR_ROOT):
        return

    files_data = []
    for root, dirs, files in os.walk(SAVE_DIR_ROOT):
        for file in files:
            if file.endswith(".md"):
                path = os.path.join(root, file)
                date_str = file[:10]
                try:
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                except:
                    continue
                
                title_part = file[11:-3].replace("_", " ")
                files_data.append({
                    "date": date_obj,
                    "date_str": date_str,
                    "title": title_part,
                    "path": path
                })

    files_data.sort(key=lambda x: x["date"], reverse=True)

    grouped = {}
    for item in files_data:
        month_key = item["date"].strftime("%Y %m")
        if month_key not in grouped:
            grouped[month_key] = []
        grouped[month_key].append(item)

    new_content = ""
    for i, (month, items) in enumerate(grouped.items()):
        if i == 0:
            new_content += f"### {month}\n"
            for item in items:
                safe_path = item["path"].replace(" ", "%20")
                new_content += f"- [{item['date_str']} : {item['title']}](./{safe_path})\n"
            new_content += "\n"
        else:
            new_content += f"<details>\n"
            new_content += f"<summary>{month} ({len(items)})</summary>\n\n"
            for item in items:
                safe_path = item["path"].replace(" ", "%20")
                new_content += f"- [{item['date_str']} : {item['title']}](./{safe_path})\n"
            new_content += "\n</details>\n\n"

    if reset_mode == 'true' or not os.path.exists(README_FILE):
        with open(README_FILE, "w", encoding="utf-8") as f:
            f.write(DEFAULT_README_TEMPLATE)

    with open(README_FILE, "r", encoding="utf-8") as f:
        readme_text = f.read()

    start_idx = readme_text.find(MARKER_START)
    end_idx = readme_text.find(MARKER_END)

    if start_idx == -1 or end_idx == -1:
        final_content = readme_text + f"\n\n{MARKER_START}\n{new_content}{MARKER_END}"
    else:
        final_content = (
            readme_text[:start_idx + len(MARKER_START)] + 
            "\n" + new_content + 
            readme_text[end_idx:]
        )

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.write(final_content)

def main():
    fetch_mode = os.environ.get('FETCH_MODE', 'DAILY')
    reset_mode = os.environ.get('RESET_MODE', 'false').lower() 
    
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    payload = {}

    if fetch_mode == "ALL":
        print(">> [INFO] 전체 데이터 동기화 시작")
    else:
        kst = timezone(timedelta(hours=TIMEZONE_HOURS))
        target_date = (datetime.now(kst) - timedelta(days=1)).strftime("%Y-%m-%d")
        print(f">> [INFO] 대상 날짜: {target_date}")
        payload["filter"] = {
            "property": NOTION_PROPERTY_DATE,
            "date": { "equals": target_date }
        }

    has_more = True
    next_cursor = None
    
    while has_more:
        if next_cursor:
            payload['start_cursor'] = next_cursor
            
        res = requests.post(url, headers=headers, json=payload)
        data = res.json()
        pages = data.get('results', [])
        
        for page in pages:
            try:
                props = page['properties']
                if not props[NOTION_PROPERTY_DATE]['date']:
                    continue
                page_date = props[NOTION_PROPERTY_DATE]['date']['start']
            except KeyError:
                continue

            title, filepath = save_as_markdown(page, page_date)
            print(f"DEBUG: 성공 - {page_date} : {title}")
        
        has_more = data.get('has_more', False)
        next_cursor = data.get('next_cursor')

    update_main_readme_by_scanning(reset_mode)

if __name__ == "__main__":
    main()
