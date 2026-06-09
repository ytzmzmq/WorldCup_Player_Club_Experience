"""
2026 FIFA World Cup 球员数据库增量更新脚本

功能:
1. 爬取英文维基百科 "2026 FIFA World Cup squads" 页面，提取所有参赛球员信息
2. 与本地 world_cup_db.json 对比，找出新增球员（增量更新）
3. 将不在最新名单中的球员标记为 is_active: false
4. 输出新入选球员列表，供下游任务抓取履历

依赖: pip install requests beautifulsoup4
"""

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ── 配置 ─────────────────────────────────────────────────────────────────────
WIKI_URL = "https://en.wikipedia.org/wiki/2026_FIFA_World_Cup_squads"
WIKI_API_URL = "https://en.wikipedia.org/w/api.php"
DB_PATH = Path(__file__).parent / "world_cup_db.json"
NEW_PLAYERS_OUTPUT = Path(__file__).parent / "new_players.json"

# 请求头，避免被 Wikipedia 反爬拦截
HEADERS = {
    "User-Agent": (
        "WorldCupDBUpdater/1.0 "
        "(https://github.com/your-repo; your-email@example.com) "
        "python-requests/2.31.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── 数据抓取 ─────────────────────────────────────────────────────────────────

def fetch_page_html() -> str:
    """获取维基百科页面 HTML，优先用 API 获取解析后的内容"""
    # 方式1: 通过 Wikipedia Parse API（更稳定，返回解析后的 HTML）
    try:
        log.info("尝试通过 Wikipedia API 获取页面...")
        resp = requests.get(
            WIKI_API_URL,
            params={
                "action": "parse",
                "page": "2026_FIFA_World_Cup_squads",
                "format": "json",
                "prop": "text",
                "disablelimitreport": True,
                "disableeditsection": True,
            },
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "parse" in data:
            html = data["parse"]["text"]["*"]
            log.info("通过 API 成功获取页面 HTML")
            return html
    except Exception as e:
        log.warning(f"Wikipedia API 请求失败: {e}")

    # 方式2: 直接请求页面
    log.info("回退到直接请求页面 HTML...")
    resp = requests.get(WIKI_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _extract_country_from_heading(heading_tag) -> str:
    """从 h3/h2 标题中提取国家名"""
    # 去掉编辑链接 [edit] 和旗帜图片等
    text = heading_tag.get_text(strip=True)
    # 移除 "[edit]" 标记
    text = re.sub(r"\[edit(?:\s*\w+)*\]", "", text).strip()
    return text


def _find_player_links_in_table(table) -> list[dict]:
    """
    从 wikitable 中提取球员信息。

    Wikipedia 世界杯名单页面的表格结构通常如下:
    - 表头包含: No., Pos., Player, Date of birth (age), Caps, Goals, Club
    - 或者用更简洁的格式

    我们寻找 "Player" 列或直接寻找表格行中的人名链接。
    """
    players = []

    rows = table.find_all("tr")
    if not rows:
        return players

    # 先解析表头，确定 "Player" 列的索引
    header_row = rows[0]
    headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
    player_col_idx = None
    for i, h in enumerate(headers):
        if h in ("player", "player name", "name"):
            player_col_idx = i
            break

    for row in rows[1:]:  # 跳过表头
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        # 策略1: 用 "Player" 列定位
        if player_col_idx is not None and player_col_idx < len(cells):
            target_cell = cells[player_col_idx]
        else:
            # 策略2: 遍历所有列，找到包含多个内部链接的列（通常是球员名）
            target_cell = None
            for cell in cells:
                links = cell.find_all("a", href=True)
                # 排除旗帜图标等小链接
                real_links = [
                    a for a in links
                    if a.get("href", "").startswith("/wiki/")
                    and "File:" not in a.get("href", "")
                    and "Flag" not in a.get("title", "")
                ]
                if real_links:
                    target_cell = cell
                    break

        if target_cell is None:
            continue

        # 在目标单元格中找到球员链接
        links = target_cell.find_all("a", href=True)
        for link in links:
            href = link["href"]
            name = link.get_text(strip=True)

            # 过滤无效链接
            if not href.startswith("/wiki/"):
                continue
            if "File:" in href or "Flag" in link.get("title", ""):
                continue
            if not name or len(name) < 2:
                continue
            # 排除明显的非球员链接（如俱乐部链接中的脚注标记）
            if re.match(r"^\d+$", name):
                continue

            full_url = urljoin("https://en.wikipedia.org", href)
            players.append({
                "name": name,
                "wiki_url": full_url,
            })
            break  # 每个单元格只取第一个有效链接

    return players


def _find_player_links_in_list(element) -> list[dict]:
    """
    备选方案：如果球员不是以表格而是以列表形式展示，
    从 <ul>/<ol> 中提取球员链接。
    """
    players = []
    for li in element.find_all("li"):
        link = li.find("a", href=True)
        if link:
            href = link["href"]
            name = link.get_text(strip=True)
            if href.startswith("/wiki/") and "File:" not in href and name:
                full_url = urljoin("https://en.wikipedia.org", href)
                players.append({
                    "name": name,
                    "wiki_url": full_url,
                })
    return players


def parse_squads(html: str) -> dict[str, list[dict]]:
    """
    解析维基百科页面 HTML，提取所有国家队及其球员列表。

    返回: {country_name: [{name, wiki_url}, ...], ...}
    """
    soup = BeautifulSoup(html, "html.parser")
    squads = {}
    current_country = None

    # Wikipedia 的页面结构:
    # h2 = Group 标题 (如 "Group A")
    # h3 = 国家标题 (如 "Ecuador")
    # 国家标题下面紧跟 wikitable

    # 先收集所有标题，按顺序遍历
    for element in soup.find_all(["h2", "h3", "table"]):
        if element.name in ("h2", "h3"):
            heading_text = _extract_country_from_heading(element)

            # h2 标题是 Group 级别（如 "Group A"）或页面级别（如 "Statistics"），
            # 永远不代表具体国家，必须清空当前国家
            if element.name == "h2":
                current_country = None
                continue

            # 对于 h3 标题，跳过非国家标题
            skip_patterns = [
                r"^squads?$",
                r"^statistics$",
                r"^references?$",
                r"^external\s+links?$",
                r"^see\s+also$",
                r"^notes?$",
                r"^contents$",
                r"^\d+\s+fifa\s+world\s+cup",
                r"^average\s+age",
                r"^coach\s+representation",
                r"^player\s+representation",
                r"^age$",
                r"^caps?$",
                r"^goals?$",
            ]
            if any(re.match(p, heading_text, re.IGNORECASE) for p in skip_patterns):
                # 关键: 遇到非国家标题时清空当前国家，
                # 防止后续统计表格被错误归到最后一个国家名下
                current_country = None
                continue

            if heading_text and len(heading_text) > 1:
                current_country = heading_text
                if current_country not in squads:
                    squads[current_country] = []

        elif element.name == "table" and current_country:
            # 检查是否是包含球员的表格
            if "wikitable" in element.get("class", []) or element.find("th"):
                players = _find_player_links_in_table(element)
                if players:
                    # 追加而非覆盖（有些国家可能有多个表格）
                    existing_names = {p["name"] for p in squads[current_country]}
                    for p in players:
                        if p["name"] not in existing_names:
                            squads[current_country].append(p)
                            existing_names.add(p["name"])

    # 二次检查: 如果表格解析效果不好，尝试从 span#id 定位
    if not squads or all(len(v) == 0 for v in squads.values()):
        log.warning("表格解析未获取到球员，尝试备选的锚点定位方式...")
        squads = _parse_by_anchors(soup)

    #  sanity check: 每队球员不应超过 30 人（世界杯名单通常 23-26 人）
    MAX_SQUAD_SIZE = 30
    for country in list(squads.keys()):
        if len(squads[country]) > MAX_SQUAD_SIZE:
            log.warning(
                f"{country} 有 {len(squads[country])} 人，超过上限 {MAX_SQUAD_SIZE}，"
                f"截取前 {MAX_SQUAD_SIZE} 人"
            )
            squads[country] = squads[country][:MAX_SQUAD_SIZE]
        elif len(squads[country]) == 0:
            # 删除空队
            del squads[country]

    return squads


def _parse_by_anchors(soup: BeautifulSoup) -> dict[str, list[dict]]:
    """
    备选解析策略: 通过 span id 锚点定位国家，
    然后获取紧跟其后的表格内容。
    """
    squads = {}

    # 找到所有 span 标签中有 id 的（通常是国家名）
    for span in soup.find_all("span", id=True):
        span_id = span["id"]
        # 过滤：只要看起来像国家名的锚点
        if "_" not in span_id and len(span_id) > 2:
            country = span_id.replace("_", " ")
        else:
            continue

        # 向上找到包含该 span 的标题标签
        heading = span.find_parent(["h2", "h3", "h4"])
        if not heading:
            continue

        # 找到标题后面的下一个兄弟元素，寻找表格
        sibling = heading.find_next_sibling()
        while sibling and sibling.name not in ("h2", "h3"):
            if sibling.name == "table":
                players = _find_player_links_in_table(sibling)
                if players:
                    squads[country] = players
                    break
            sibling = sibling.find_next_sibling()

    return squads


# ── 本地数据库操作 ─────────────────────────────────────────────────────────────

def load_local_db(path: Path) -> dict:
    """加载本地 JSON 数据库，不存在则返回空结构"""
    if path.exists():
        log.info(f"加载本地数据库: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    log.info(f"本地数据库不存在: {path}，将创建新库")
    return {
        "meta": {
            "created_at": datetime.now().isoformat(),
            "last_updated": None,
            "version": "1.0",
        },
        "players": [],
    }


def save_db(db: dict, path: Path):
    """保存数据库到本地"""
    db["meta"]["last_updated"] = datetime.now().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    log.info(f"数据库已保存: {path}")


def build_player_key(name: str, country: str) -> str:
    """
    生成球员唯一标识键。
    用 (名字 + 国家) 组合，因为同名球员可能存在于不同国家。
    """
    # 标准化: 去空格、小写
    norm_name = re.sub(r"\s+", " ", name.strip().lower())
    norm_country = country.strip().lower()
    return f"{norm_name}__{norm_country}"


# ── 核心更新逻辑 ─────────────────────────────────────────────────────────────

def update_database(squads: dict[str, list[dict]], db: dict) -> list[dict]:
    """
    增量更新数据库:
    1. 新增球员 → 加入数据库, is_active=true
    2. 已存在的球员 → 保持 is_active=true
    3. 不在最新名单中的球员 → 标记 is_active=false

    返回: 新增球员列表 (供下游任务使用)
    """
    # 构建本地球员索引: key -> player_record
    existing_index = {}
    for player in db["players"]:
        key = build_player_key(player["name"], player["country"])
        existing_index[key] = player

    # 构建最新名单的 key 集合
    latest_keys = set()
    new_players = []

    for country, players in squads.items():
        for p in players:
            key = build_player_key(p["name"], country)
            latest_keys.add(key)

            if key not in existing_index:
                # ── 新球员 ──
                record = {
                    "name": p["name"],
                    "country": country,
                    "wiki_url": p["wiki_url"],
                    "is_active": True,
                    "added_at": datetime.now().isoformat(),
                    "profile": None,  # 下游任务填充
                }
                db["players"].append(record)
                new_players.append(record)
                log.info(f"  [+] 新增球员: {p['name']} ({country})")
            else:
                # ── 已有球员，确保活跃状态 ──
                existing_index[key]["is_active"] = True
                # 更新 wiki_url（以防链接变化）
                existing_index[key]["wiki_url"] = p["wiki_url"]

    # ── 处理被替换下场的球员 ──
    replaced_count = 0
    for player in db["players"]:
        key = build_player_key(player["name"], player["country"])
        if key not in latest_keys and player.get("is_active", True):
            player["is_active"] = False
            player["deactivated_at"] = datetime.now().isoformat()
            replaced_count += 1
            log.info(f"  [-] 球员被替换: {player['name']} ({player['country']})")

    log.info(f"更新完成: 新增 {len(new_players)} 人, "
             f"被替换 {replaced_count} 人, "
             f"当前活跃球员 {sum(1 for p in db['players'] if p.get('is_active', True))} 人")

    return new_players


# ── 输出 ──────────────────────────────────────────────────────────────────────

def output_new_players(new_players: list[dict], path: Path):
    """将新球员列表输出到 JSON 文件，供下游任务读取"""
    output_data = {
        "generated_at": datetime.now().isoformat(),
        "count": len(new_players),
        "players": [
            {
                "name": p["name"],
                "country": p["country"],
                "wiki_url": p["wiki_url"],
            }
            for p in new_players
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    log.info(f"新球员列表已输出: {path} ({len(new_players)} 人)")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("2026 FIFA World Cup 球员数据库更新")
    log.info("=" * 60)

    # 1) 抓取维基百科
    log.info(f"目标页面: {WIKI_URL}")
    try:
        html = fetch_page_html()
    except requests.RequestException as e:
        log.error(f"无法获取维基百科页面: {e}")
        sys.exit(1)

    # 2) 解析球员名单
    log.info("解析球员名单...")
    squads = parse_squads(html)

    total_countries = len(squads)
    total_players = sum(len(v) for v in squads.values())
    log.info(f"解析结果: {total_countries} 支国家队, {total_players} 名球员")

    if total_players == 0:
        log.error("未解析到任何球员，请检查页面结构是否变化")
        # 将原始 HTML 保存一份以便调试
        debug_path = Path(__file__).parent / "debug_page.html"
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(html)
        log.error(f"已保存原始 HTML 到 {debug_path}，请人工检查")
        sys.exit(1)

    # 打印各国名单概要
    log.info("各国球员数量:")
    for country in sorted(squads.keys()):
        log.info(f"  {country}: {len(squads[country])} 人")

    # 3) 加载本地数据库
    db = load_local_db(DB_PATH)

    # 4) 增量更新
    log.info("执行增量更新...")
    new_players = update_database(squads, db)

    # 5) 保存数据库
    save_db(db, DB_PATH)

    # 6) 输出新球员列表
    output_new_players(new_players, NEW_PLAYERS_OUTPUT)

    # 7) 终端输出概要
    log.info("=" * 60)
    log.info("更新完成!")
    log.info(f"  数据库路径: {DB_PATH}")
    log.info(f"  新球员文件: {NEW_PLAYERS_OUTPUT}")
    log.info(f"  新球员数量: {len(new_players)}")
    if new_players:
        log.info("新球员名单:")
        for p in new_players:
            log.info(f"    {p['name']} ({p['country']}) → {p['wiki_url']}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
