import os
import requests
import re
from datetime import datetime, timezone, timedelta

# =======================================================
# [ 설정 ]
# =======================================================

SAVE_DIR_ROOT = "TIL" 
NOTION_PROPERTY_TITLE = "제목"  # 사용 중인 노션 제목 속성명에 맞게 변경해 주세요
NOTION_PROPERTY_DATE = "날짜"    # 사용 중인 노션 날짜 속성명에 맞게 변경해 주세요
README_FILE = "README.md"
MARKER_START = "<!-- START_SECTION:til -->"
MARKER_END = "<!-- END_SECTION:til -->"
TIMEZONE_HOURS = 9 

# 기본 README 템플릿
DEFAULT_README_TEMPLATE = f"""# 📚 My TIL Collection

매일 학습한 TIL(Today I Learned) 기록입니다.

## 최근 기록
{MARKER_START}
{MARKER_END}
"""

# =======================================================
# [ 환경변수 및 헤더 ]
# =======================================================
NOTION_TOKEN = os.environ['NOTION_TOKEN']
DATABASE_ID = os.environ['NOTION_DATABASE_ID']

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

def get_page_blocks(block_id):
    """
    특정 페이지 또는 블록의 자식 블록들을 재귀적으로 가져옵니다.
    """
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    response = requests.get(url, headers=headers)
    results = response.json().get('results', [])
    
    blocks = []
    for block in results:
        # 자식 블록이 존재할 경우 재귀적으로 탐색
        if block.get('has_children'):
            block['children'] = get_page_blocks(block['id'])
        blocks.append(block)
    return blocks

def extract_text_from_rich_text(rich_text_list):
    content = ""
    for text in rich_text_list:
        plain = text['plain_text']
        href = text.get('href')
        if href:
            content += f"[{plain}]({href})"
        else:
            content += plain
    return content

def block_to_markdown(block, depth=0):
    """
    depth 인자를 받아 하위 계층일수록 들여쓰기 공백을 추가하여
    GitHub Markdown 프리뷰에서 깨지지 않는 리스트를 생성합니다.
    """
    b_type = block['type']
    indent = "  " * depth  # 깊이에 따른 2칸 들여쓰기
    markdown_output = ""

    if b_type in ['paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item', 'numbered_list_item', 'to_do', 'toggle', 'quote', 'callout']:
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
        elif b_type == 'quote': 
            markdown_output += f"{indent}> {content}\n\n"
        elif b_type == 'callout': 
            markdown_output += f"{indent}> 💡 {content}\n\n"
        elif b_type == 'toggle': 
            markdown_output += f"{indent}- {content}\n"
            
    elif b_type == 'code':
        language = block['code'].get('language', 'text')
        rich_text = block['code'].get('rich_text', [])
        content = extract_text_from_rich_text(rich_text)
        markdown_output += f"{indent}```{language}\n{content}\n{indent}```\n\n"
    elif b_type == 'image':
        url = block['image'].get('file', {}).get('url') or block['image'].get('external', {}).get('url') or ""
        markdown_output += f"{indent}![Image]({url})\n\n"
    elif b_type == 'divider': 
        markdown_output += f"\n---\n\n"

    # 자식 블록이 있으면 depth + 1을 전달하여 처리
    if block.get('children'):
        for child in block['children']:
            markdown_output += block_to_markdown(child, depth + 1)

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
    
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
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
        markdown_content += block_to_markdown(block, depth=0)
    
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
        print(f">> [INFO] README.md 생성. (RESET_MODE: {reset_mode})")
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
        print(">> [모드: ALL] 전체 데이터를 가져옵니다.")
    else:
        kst = timezone(timedelta(hours=TIMEZONE_HOURS))
        target_date = (datetime.now(kst) - timedelta(days=1)).strftime("%Y-%m-%d")
        print(f">> [모드: DAILY] {target_date} 데이터를 가져옵니다.")
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
            print(f"DEBUG: 변환 완료 - {page_date} : {title}")
        
        has_more = data.get('has_more', False)
        next_cursor = data.get('next_cursor')

    update_main_readme_by_scanning(reset_mode)

if __name__ == "__main__":
    main()
