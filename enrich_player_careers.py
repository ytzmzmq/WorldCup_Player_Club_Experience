"""
2026 FIFA World Cup 球员履历补全脚本

数据源策略（混合模式）:
- Transfermarkt: 搜索球员 → 获取 TM ID + 当前俱乐部
- Wikipedia: 从球员个人百科页面提取完整职业履历（青训 + 成年队所有俱乐部）
- 两源合并去重，确保覆盖率

功能:
1. 读取 world_cup_db.json 中尚未补全履历的球员
2. 逐人抓取 Transfermarkt + Wikipedia，提取所有效力过的俱乐部
3. 清洗俱乐部名称（去年份、出场数等噪声）
4. 将 clubs 数组写回 world_cup_db.json
5. 断点续跑 + 重试 + 异常保护

依赖: pip install cloudscraper beautifulsoup4 requests
"""

import json
import logging
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import cloudscraper
import requests as req_lib
from bs4 import BeautifulSoup

# ── 配置 ─────────────────────────────────────────────────────────────────────
TM_BASE = "https://www.transfermarkt.com"
TM_SEARCH = f"{TM_BASE}/schnellsuche/ergebnis/schnellsuche"

RATE_LIMIT_TM = (5, 9)     # Transfermarkt 请求间隔 (秒)
RATE_LIMIT_WIKI = (1, 3)   # Wikipedia 请求间隔 (秒)
MAX_RETRIES = 3
BACKOFF_BASE = 15

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "world_cup_db.json"
PROGRESS_PATH = SCRIPT_DIR / "enrich_progress.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WIKI_UA = (
    "WorldCupEnricher/1.0 "
    "(https://github.com/your-repo; your-email@example.com) "
    "python-requests/2.31.0"
)


# ══════════════════════════════════════════════════════════════════════════════
# 俱乐部名清洗
# ══════════════════════════════════════════════════════════════════════════════

_NOISE_PATTERNS = [
    r"\b(19|20)\d{2}\b",                        # 年份
    r"\b\d{4}\s*[–/-]\s*\d{2,4}\b",             # 赛季区间
    r"\bU[-\s]?\d{1,2}\b",                      # 年龄梯队
    r"\b(?:Youth|Academy|Reserves?|II|III|B\s*Team|Amateure)\b",
    r"\b(?:Loan|Free Transfer|End of loan|Retired)\b",
    r"\b\d+\s*(?:apps?|appearances?|goals?|caps?)\b",
    r"\b(?:Total|Overall|Career total)\b",
    r"^\s*[-–—•·]\s*",
    r"\b\d{1,2}\s*$",                           # 尾部孤立数字
]

# 需要完全忽略的非俱乐部文本
_IGNORE_TERMS = {
    "total", "career", "youth", "senior", "senior career", "youth career",
    "national team", "international", "apps", "goals", "caps", "ref",
    "club", "team", "years", "season", "league", "division",
    "→", "–", "-", "—",
}


def clean_club_name(raw: str) -> str:
    """清洗俱乐部名称"""
    name = raw.strip()
    # 去除超链接标记 [1], [2] 等
    name = re.sub(r"\[\d+\]", "", name)

    for pattern in _NOISE_PATTERNS:
        name = re.sub(pattern, "", name, flags=re.IGNORECASE)

    name = re.sub(r"\([^)]*\)", "", name)   # 去括号
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(" -–—,.;:|/")

    return name


def is_valid_club(name: str) -> bool:
    """判断清洗后的名字是否是有效的俱乐部名"""
    if not name or len(name) < 2:
        return False
    lower = name.lower().strip()
    if lower in _IGNORE_TERMS:
        return False
    if re.match(r"^\d+$", lower):
        return False
    if len(lower) > 60:
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# 进度追踪
# ══════════════════════════════════════════════════════════════════════════════

def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"processed": {}, "failed": []}


def save_progress(progress: dict):
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# 网络请求工具
# ══════════════════════════════════════════════════════════════════════════════

def create_tm_scraper():
    """创建 Transfermarkt 专用 cloudscraper"""
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
        "Referer": TM_BASE,
    })
    return s


def fetch_retry(session, url, retries=MAX_RETRIES, **kwargs):
    """通用重试请求"""
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=30, **kwargs)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                wait = 60 + random.uniform(10, 30)
                log.warning(f"      限速 (429)，等待 {wait:.0f}s...")
                time.sleep(wait)
                continue
            log.warning(f"      HTTP {resp.status_code}: {url}")
            if attempt < retries - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
        except Exception as e:
            log.warning(f"      请求异常: {e}")
            if attempt < retries - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Transfermarkt 数据源
# ══════════════════════════════════════════════════════════════════════════════

def tm_search_player(tm, name: str) -> str | None:
    """
    Transfermarkt 搜索球员，返回 player_id。
    优先精确匹配，否则返回第一个结果。
    """
    time.sleep(random.uniform(1, 3))
    resp = fetch_retry(tm, TM_SEARCH, params={"query": name})
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    first_id = None

    # 新版卡片: <a class="no-result__cards" href="/spieler/profil?id={id}">
    for card in soup.find_all("a", class_="no-result__cards"):
        href = card.get("href", "")
        m = re.search(r"[?&]id=(\d+)", href)
        if not m:
            continue
        pid = m.group(1)

        # 提取名字用于匹配
        parts = []
        for cls in ("no-result__player-name", "no-result__player-lastname"):
            span = card.find("span", class_=cls)
            if span:
                parts.append(span.get_text(strip=True))
        card_name = " ".join(parts).strip()

        if _jaccard(card_name.lower(), name.lower()) > 0.4:
            return pid
        if first_id is None:
            first_id = pid

    return first_id


def tm_get_current_club(tm, player_id: str) -> str | None:
    """从 Transfermarkt profile 页面提取当前俱乐部"""
    url = f"{TM_BASE}/spieler/profil?id={player_id}"
    time.sleep(random.uniform(*RATE_LIMIT_TM))
    resp = fetch_retry(tm, url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    club_span = soup.find("span", class_="data-header__club")
    if club_span:
        return club_span.get_text(strip=True)
    # 备选: /verein/ 链接
    for a in soup.find_all("a", href=re.compile(r"/verein/")):
        text = a.get_text(strip=True)
        if text and len(text) > 2:
            return text
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Wikipedia 数据源（完整职业履历）
# ══════════════════════════════════════════════════════════════════════════════

def wiki_get_career_clubs(wiki_url: str) -> list[str]:
    """
    从球员 Wikipedia 页面提取所有效力俱乐部。
    综合利用: infobox + 正文 career 段落 + 统计表。
    """
    time.sleep(random.uniform(*RATE_LIMIT_WIKI))

    try:
        resp = req_lib.get(
            wiki_url,
            headers={"User-Agent": WIKI_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=20,
        )
        if resp.status_code != 200:
            log.warning(f"      Wikipedia 返回 {resp.status_code}")
            return []
    except Exception as e:
        log.warning(f"      Wikipedia 请求失败: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    clubs_raw: list[str] = []

    # ── 策略 1: Infobox ──
    clubs_raw.extend(_wiki_extract_infobox(soup))

    # ── 策略 2: Career section headings ──
    clubs_raw.extend(_wiki_extract_career_sections(soup))

    # ── 策略 3: Career statistics table ──
    clubs_raw.extend(_wiki_extract_stats_table(soup))

    # 清洗 + 去重
    seen: set[str] = set()
    clubs: list[str] = []
    for raw in clubs_raw:
        cleaned = clean_club_name(raw)
        if is_valid_club(cleaned) and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            clubs.append(cleaned)

    return clubs


def _wiki_extract_infobox(soup: BeautifulSoup) -> list[str]:
    """从 Wikipedia infobox 提取俱乐部（Youth career + Senior career）"""
    clubs = []
    infobox = soup.find("table", class_="infobox")
    if not infobox:
        # 备选: vcard / football biography 格式
        infobox = soup.find("table", class_=re.compile(r"vcard|football"))

    if not infobox:
        return clubs

    rows = infobox.find_all("tr")
    in_career_section = False

    for row in rows:
        header = row.find(["th", "td"])
        if not header:
            continue

        header_text = header.get_text(strip=True).lower()

        # 检测 career section 开始
        if any(kw in header_text for kw in [
            "youth career", "senior career", "club career",
            "career", "playing career", "clubs",
        ]):
            in_career_section = True

        # 检测 career section 结束（遇到新的 section header）
        if in_career_section and any(kw in header_text for kw in [
            "national team", "international", "managerial",
            "coaching", "medals", "personal", "full name",
        ]):
            in_career_section = False
            continue

        if in_career_section:
            # 提取该行中的所有俱乐部链接
            for a in row.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                # 排除国旗、联赛标志等
                if "File:" in href or "Flag" in a.get("title", ""):
                    continue
                if text and len(text) > 1:
                    clubs.append(text)

    # 如果 infobox 方法效果不好，尝试更宽松的提取
    if len(clubs) < 3:
        # 提取 infobox 中所有看起来像俱乐部的链接
        for a in infobox.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if "File:" in href or "Flag" in a.get("title", ""):
                continue
            if text and len(text) > 2 and not text[0].isdigit():
                clubs.append(text)

    return clubs


def _wiki_extract_career_sections(soup: BeautifulSoup) -> list[str]:
    """
    从 Wikipedia 正文的 career 章节提取俱乐部。
    寻找 ==Club career== / ==Career== 等标题下的内容。
    """
    clubs = []

    for heading in soup.find_all(re.compile(r"^h[2-4]$")):
        heading_text = heading.get_text(strip=True).lower()
        if not any(kw in heading_text for kw in [
            "club career", "career", "youth career", "early career",
            "professional career", "playing career",
        ]):
            continue

        # 遍历此标题下的段落，直到遇到下一个同级标题
        sibling = heading.find_next_sibling()
        while sibling:
            if sibling.name in ("h2", "h3", "h4") and sibling.name <= heading.name:
                break  # 新 section 开始

            if sibling.name in ("p", "div", "ul", "ol"):
                # 提取段落中的俱乐部链接
                for a in sibling.find_all("a", href=True):
                    href = a["href"]
                    text = a.get_text(strip=True)
                    if "/wiki/" in href and "File:" not in href:
                        if text and len(text) > 2:
                            clubs.append(text)

            sibling = sibling.find_next_sibling()

    return clubs


def _wiki_extract_stats_table(soup: BeautifulSoup) -> list[str]:
    """从职业统计数据表中提取俱乐部名"""
    clubs = []

    for table in soup.find_all("table", class_="wikitable"):
        # 检查表头是否包含 career stats 关键词
        header_row = table.find("tr")
        if not header_row:
            continue
        header_text = header_row.get_text(strip=True).lower()
        if not any(kw in header_text for kw in ["club", "season", "league"]):
            continue

        # 遍历表格行，提取第一列的俱乐部名（通常是 Club 列）
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            first_cell = cells[0]
            for a in first_cell.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if "/wiki/" in href and "File:" not in href and text:
                    clubs.append(text)
            # 如果第一列没有链接，取纯文本
            if not first_cell.find("a"):
                text = first_cell.get_text(strip=True)
                if text and len(text) > 2:
                    clubs.append(text)

    return clubs


# ══════════════════════════════════════════════════════════════════════════════
# 名字相似度
# ══════════════════════════════════════════════════════════════════════════════

def _jaccard(a: str, b: str) -> float:
    words_a = set(re.findall(r"\w+", a.lower()))
    words_b = set(re.findall(r"\w+", b.lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


# ══════════════════════════════════════════════════════════════════════════════
# 数据库操作
# ══════════════════════════════════════════════════════════════════════════════

def load_db() -> dict:
    if not DB_PATH.exists():
        log.error(f"数据库文件不存在: {DB_PATH}")
        log.error("请先运行 update_world_cup_db.py")
        sys.exit(1)
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(db: dict):
    db["meta"]["last_updated"] = datetime.now().isoformat()
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def player_key(p: dict) -> str:
    name = re.sub(r"\s+", " ", p["name"].strip().lower())
    country = p["country"].strip().lower()
    return f"{name}__{country}"


# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════

def main():
    limit = None
    if len(sys.argv) > 1 and sys.argv[1] == "--limit":
        try:
            limit = int(sys.argv[2])
        except (IndexError, ValueError):
            log.error("用法: python enrich_player_careers.py --limit 5")
            sys.exit(1)

    log.info("=" * 60)
    log.info("球员履历补全 (Transfermarkt + Wikipedia)")
    log.info("=" * 60)

    db = load_db()
    progress = load_progress()

    pending = [
        p for p in db["players"]
        if p.get("is_active", True)
        and (p.get("clubs") is None or p.get("clubs") == [])
        and player_key(p) not in progress["processed"]
    ]

    total_active = sum(1 for p in db["players"] if p.get("is_active", True))
    already_done = len(progress["processed"])
    log.info(f"活跃球员总数: {total_active}")
    log.info(f"已完成: {already_done}")
    log.info(f"待处理: {len(pending)}")

    if not pending:
        log.info("所有球员履历已补全，无需操作")
        return

    if limit:
        pending = pending[:limit]
        log.info(f"测试模式: 本次只处理前 {limit} 名球员")

    tm = create_tm_scraper()
    processed_count = 0
    success_count = 0
    fail_count = 0
    save_interval = 10

    for i, player in enumerate(pending):
        pk = player_key(player)
        log.info(f"\n[{i + 1}/{len(pending)}] {player['name']} ({player['country']})")

        try:
            all_clubs: list[str] = []

            # ── 数据源 1: Transfermarkt (当前俱乐部) ──
            log.info(f"    [TM] 搜索...")
            tm_id = tm_search_player(tm, player["name"])
            tm_current_club = None

            if tm_id:
                log.info(f"    [TM] ID={tm_id}")
                tm_current_club = tm_get_current_club(tm, tm_id)
                if tm_current_club:
                    cleaned = clean_club_name(tm_current_club)
                    if is_valid_club(cleaned):
                        all_clubs.append(cleaned)
                        log.info(f"    [TM] 当前俱乐部: {cleaned}")
            else:
                log.warning(f"    [TM] 未找到")

            # ── 数据源 2: Wikipedia (完整职业履历) ──
            wiki_url = player.get("wiki_url")
            if wiki_url:
                log.info(f"    [Wiki] 提取履历...")
                wiki_clubs = wiki_get_career_clubs(wiki_url)
                if wiki_clubs:
                    # 合并（Wikipedia 俱乐部放前面，TM 当前俱乐部补在后面）
                    existing_lower = {c.lower() for c in all_clubs}
                    for c in wiki_clubs:
                        if c.lower() not in existing_lower:
                            all_clubs.append(c)
                            existing_lower.add(c.lower())
                    log.info(f"    [Wiki] 找到 {len(wiki_clubs)} 个俱乐部")
                else:
                    log.warning(f"    [Wiki] 未找到俱乐部记录")
            else:
                log.warning(f"    [Wiki] 无维基百科 URL")

            # ── 写入数据库 ──
            if all_clubs:
                player["clubs"] = all_clubs
                log.info(f"    ✓ 共 {len(all_clubs)} 个俱乐部: {', '.join(all_clubs)}")
                progress["processed"][pk] = {
                    "tm_id": tm_id,
                    "club_count": len(all_clubs),
                    "done_at": datetime.now().isoformat(),
                }
                success_count += 1
            else:
                log.warning(f"    ✗ 未找到任何俱乐部")
                player["clubs"] = []
                progress["processed"][pk] = {
                    "tm_id": tm_id,
                    "club_count": 0,
                    "note": "no clubs found",
                    "done_at": datetime.now().isoformat(),
                }

            processed_count += 1

        except Exception as e:
            log.error(f"    处理异常: {e}")
            fail_count += 1
            progress["failed"].append({
                "key": pk,
                "name": player["name"],
                "country": player["country"],
                "error": str(e),
                "at": datetime.now().isoformat(),
            })

        # Transfermarkt 请求间隔
        wait = random.uniform(*RATE_LIMIT_TM)
        log.info(f"    等待 {wait:.1f}s...")
        time.sleep(wait)

        if processed_count % save_interval == 0:
            save_db(db)
            save_progress(progress)
            log.info(f"    ── 中间保存 ({processed_count}/{len(pending)}) ──")

    # 最终保存
    save_db(db)
    save_progress(progress)

    log.info("\n" + "=" * 60)
    log.info("补全完成!")
    log.info(f"  本次处理: {processed_count}")
    log.info(f"  成功: {success_count}")
    log.info(f"  失败: {fail_count}")
    log.info(f"  累计完成: {already_done + processed_count}")
    log.info(f"  数据库已保存: {DB_PATH}")

    if progress["failed"]:
        log.info(f"  历史失败记录: {len(progress['failed'])} 条")
        for f in progress["failed"][-5:]:
            log.info(f"    - {f['name']} ({f['country']}): {f['error']}")

    log.info("=" * 60)


if __name__ == "__main__":
    main()
