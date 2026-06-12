"""
2026 FIFA World Cup 球员履历补全脚本 v2 (Transfermarkt API 版)

数据源策略:
- 主源: Transfermarkt REST API (tmapi.transfermarkt.technology)
  - 搜索: HTML 页面获取 player_id
  - 转会历史: JSON API, 带完整时间线
  - 俱乐部名称: JSON API 或 Profile HTML
- 辅源: Wikipedia (仅在 TM 找不到时使用)

功能:
1. 读取 world_cup_db.json 中尚未补全或需要刷新的球员
2. 通过 TM API 获取结构化转会历史
3. 构建有序的职业履历时间线
4. 写回 world_cup_db.json (career + current_club + clubs)
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

import cloudscraper
import requests as req_lib
from bs4 import BeautifulSoup

# Windows UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 配置 ─────────────────────────────────────────────────────────────────────

TM_BASE = "https://www.transfermarkt.com"
TM_SEARCH = f"{TM_BASE}/schnellsuche/ergebnis/schnellsuche"
TM_API = "https://tmapi.transfermarkt.technology"

RATE_LIMIT_TM = (5, 8)      # Transfermarkt 主站请求间隔 (秒)
RATE_LIMIT_API = (1.5, 3)   # TM API 请求间隔 (秒)
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
    """通用重试请求，处理 SSL 错误和限速"""
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
            log.warning(f"      请求异常: {type(e).__name__}: {str(e)[:80]}")
            if attempt < retries - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
    return None


def _jaccard(a: str, b: str) -> float:
    words_a = set(re.findall(r"\w+", a.lower()))
    words_b = set(re.findall(r"\w+", b.lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


# ══════════════════════════════════════════════════════════════════════════════
# Transfermarkt 数据源
# ══════════════════════════════════════════════════════════════════════════════

def tm_search_player(tm, name: str) -> dict | None:
    """
    Transfermarkt 搜索球员，返回 {player_id, slug, name}。
    优先精确匹配，否则返回第一个结果。
    """
    time.sleep(random.uniform(1, 3))
    resp = fetch_retry(tm, TM_SEARCH, params={"query": name})
    if not resp or resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    candidates = []

    # 新版格式: <a href="/slug/profil/spieler/{id}">
    for a in soup.find_all("a", href=re.compile(r"/profil/spieler/\d+")):
        href = a["href"]
        text = a.get_text(strip=True)
        m = re.search(r"/(.+)/profil/spieler/(\d+)", href)
        if m and text and len(text) > 1:
            slug = m.group(1)
            pid = m.group(2)
            candidates.append({"slug": slug, "player_id": pid, "name": text})

    # 旧版格式: <a class="no-result__cards" href="/spieler/profil?id={id}">
    if not candidates:
        for card in soup.find_all("a", class_="no-result__cards"):
            href = card.get("href", "")
            m = re.search(r"[?&]id=(\d+)", href)
            if not m:
                continue
            pid = m.group(1)
            parts = []
            for cls in ("no-result__player-name", "no-result__player-lastname"):
                span = card.find("span", class_=cls)
                if span:
                    parts.append(span.get_text(strip=True))
            card_name = " ".join(parts).strip()
            if card_name:
                candidates.append({"slug": "", "player_id": pid, "name": card_name})

    if not candidates:
        return None

    # 选择最佳匹配
    best = max(candidates, key=lambda c: _jaccard(c["name"], name))
    if _jaccard(best["name"], name) < 0.3:
        return None  # 相似度太低，可能不是同一个人
    return best


def tm_get_profile(tm, slug: str, player_id: str) -> dict:
    """获取 Profile 页面，提取当前俱乐部和俱乐部 ID 映射"""
    if slug:
        url = f"{TM_BASE}/{slug}/profil/spieler/{player_id}"
    else:
        url = f"{TM_BASE}/spieler/profil?id={player_id}"

    time.sleep(random.uniform(*RATE_LIMIT_TM))
    resp = fetch_retry(tm, url)

    result = {
        "current_club": None,
        "club_id_map": {},  # clubId -> name
    }

    if not resp or resp.status_code != 200:
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # 当前俱乐部
    club_span = soup.find("span", class_="data-header__club")
    if club_span:
        result["current_club"] = club_span.get_text(strip=True)

    # 从 /verein/ 链接建立 clubId -> name 映射
    for a in soup.find_all("a", href=re.compile(r"/verein/(\d+)")):
        text = a.get_text(strip=True)
        m = re.search(r"/verein/(\d+)", a["href"])
        if m and text and len(text) > 2:
            result["club_id_map"][m.group(1)] = text

    return result


def tm_get_transfer_history(tm, player_id: str) -> dict | None:
    """通过 TM API 获取结构化转会历史"""
    url = f"{TM_API}/transfer/history/player/{player_id}"

    old_accept = tm.headers.get("Accept")
    tm.headers["Accept"] = "application/json"

    time.sleep(random.uniform(*RATE_LIMIT_API))
    try:
        resp = fetch_retry(tm, url)
    finally:
        if old_accept:
            tm.headers["Accept"] = old_accept

    if not resp or resp.status_code != 200:
        return None

    try:
        data = json.loads(resp.text)
    except json.JSONDecodeError:
        return None

    if not data.get("success"):
        return None

    return data.get("data", {})


def tm_resolve_club_name(tm, club_id: str) -> str | None:
    """通过 TM API 解析俱乐部名称"""
    url = f"{TM_API}/club/{club_id}"

    old_accept = tm.headers.get("Accept")
    tm.headers["Accept"] = "application/json"

    time.sleep(random.uniform(*RATE_LIMIT_API))
    try:
        resp = fetch_retry(tm, url, retries=2)
    finally:
        if old_accept:
            tm.headers["Accept"] = old_accept

    if not resp or resp.status_code != 200:
        return None

    try:
        data = json.loads(resp.text)
    except json.JSONDecodeError:
        return None

    if data.get("success"):
        return data["data"].get("name")
    return None


def build_career_from_api(api_data: dict, club_id_map: dict, tm) -> tuple[list[dict], str | None]:
    """
    从 TM API 转会历史构建有序职业履历。

    返回:
        career: [{club, from_year, to_year, is_youth}, ...] 按时间正序
        current_club: 当前俱乐部名 (str | None)
    """
    transfers = api_data.get("history", {}).get("terminated", [])
    all_club_ids = api_data.get("clubIds", [])

    # 过滤掉缺失数据的转会记录
    transfers = [
        t for t in transfers
        if t.get("details") and t["details"].get("date")
        and t.get("transferDestination") and t["transferDestination"].get("clubId")
    ]

    # 按日期正序排列
    transfers_sorted = sorted(transfers, key=lambda x: x["details"]["date"])

    # 解析所有俱乐部名称
    resolved_names = dict(club_id_map)

    # 找出还需要解析的 clubId
    missing_ids = [cid for cid in all_club_ids if cid not in resolved_names]
    for cid in missing_ids:
        name = tm_resolve_club_name(tm, cid)
        if name:
            resolved_names[cid] = name

    if not transfers_sorted:
        return [], None

    # ── 清理占位名称 ──
    # Transfermarkt 对免签/冬窗转会偶尔显示 "New arrival"、"Winter signing" 等
    _PLACEHOLDER_NAMES = {
        "new arrival", "winter signing", "summer signing",
        "new player", "loan signing",
    }

    def clean_club_name(name: str, club_id: str) -> str:
        """如果俱乐部名是占位符，尝试用 API 重新解析；同时清理尾部日期标注"""
        if name.lower().strip() in _PLACEHOLDER_NAMES:
            # 重新用 API 解析
            api_name = tm_resolve_club_name(tm, club_id)
            if api_name and api_name.lower().strip() not in _PLACEHOLDER_NAMES:
                return api_name
        # 清理 TM 偶尔在俱乐部名后附加的合同到期标注, 如 "Club Name (- 2024)"
        name = re.sub(r"\s*\(-\s*\d{4}\)\s*$", "", name)
        return name

    # ── 青年队/预备队检测 ──
    _YOUTH_PATTERNS = re.compile(
        r"\bU[-\s]?\d{1,2}\b|"       # U17, U19, U21, U-23
        r"\bYouth\b|"
        r"\bAcademy\b|"
        r"\bReserves?\b|"
        r"\bII\b|"                    # Team II
        r"\bB\s*$|"                   # 末尾 B (Barcelona B)
        r"\bB\s+Team\b",
        re.IGNORECASE,
    )

    def is_youth_club(name: str) -> bool:
        return bool(_YOUTH_PATTERNS.search(name))

    # ── 构建转会时间线 ──
    career = []
    seen = set()

    for t in transfers_sorted:
        dst_id = t["transferDestination"]["clubId"]
        dst_name = resolved_names.get(dst_id, f"Club#{dst_id}")
        dst_name = clean_club_name(dst_name, dst_id)

        if dst_name not in seen:
            seen.add(dst_name)
            year = t["details"]["date"][:4]
            career.append({
                "club": dst_name,
                "from_year": year,
                "is_youth": is_youth_club(dst_name),
            })

    # 补充 to_year
    for i, entry in enumerate(career):
        if i + 1 < len(career):
            entry["to_year"] = career[i + 1]["from_year"]
        else:
            entry["to_year"] = None  # 当前俱乐部

    # 最后一次转会的 destination 是当前俱乐部
    last_transfer = transfers_sorted[-1]
    last_club_id = last_transfer["transferDestination"]["clubId"]
    current_club = resolved_names.get(last_club_id)
    if current_club and current_club.lower().strip() in _PLACEHOLDER_NAMES:
        current_club = clean_club_name(current_club, last_club_id)

    return career, current_club


# ══════════════════════════════════════════════════════════════════════════════
# Wikipedia 备选 (仅在 TM 找不到时使用)
# ══════════════════════════════════════════════════════════════════════════════

def wiki_get_career_clubs(wiki_url: str) -> list[str]:
    """从 Wikipedia 提取俱乐部列表（无时间线，仅作为备选）"""
    time.sleep(random.uniform(1, 3))
    try:
        resp = req_lib.get(
            wiki_url,
            headers={"User-Agent": WIKI_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=20,
        )
        if resp.status_code != 200:
            return []
    except Exception:
        return []

    # 需要过滤掉的非俱乐部文本
    _WIKI_NOISE = {
        "total", "career", "youth", "senior", "senior career", "youth career",
        "national team", "international", "apps", "goals", "caps", "ref",
        "club", "team", "years", "season", "league", "division",
        "goalkeeper", "defender", "midfielder", "forward", "striker",
        "centre-back", "center-back", "left-back", "right-back",
        "attacking midfielder", "defensive midfielder", "winger",
        "left wing", "right wing", "centre-forward", "center-forward",
        "football", "soccer", "citation needed",
    }

    # 国家队名称 (常见世界杯参赛国)
    _COUNTRY_NAMES = {
        "algeria", "argentina", "australia", "austria", "belgium",
        "bosnia and herzegovina", "brazil", "canada", "cape verde",
        "colombia", "croatia", "curaçao", "czech republic", "dr congo",
        "ecuador", "egypt", "england", "france", "germany", "ghana",
        "haiti", "iran", "iraq", "ivory coast", "japan", "jordan",
        "mexico", "morocco", "netherlands", "new zealand", "norway",
        "panama", "paraguay", "portugal", "qatar", "saudi arabia",
        "scotland", "senegal", "south africa", "south korea", "spain",
        "sweden", "switzerland", "tunisia", "turkey", "united states",
        "uruguay", "uzbekistan",
    }

    soup = BeautifulSoup(resp.text, "html.parser")
    clubs = []

    # Infobox
    infobox = soup.find("table", class_="infobox") or soup.find("table", class_=re.compile(r"vcard|football"))
    if infobox:
        for a in infobox.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if "File:" in href or "Flag" in a.get("title", ""):
                continue
            if text and len(text) > 2:
                clubs.append(text)

    # 去重 + 过滤噪声
    seen = set()
    result = []
    for c in clubs:
        cl = c.strip()
        lower = cl.lower()
        # 过滤: 噪声词、国家名、引用标记、过短/过长
        if lower in seen:
            continue
        if lower in _WIKI_NOISE or lower in _COUNTRY_NAMES:
            continue
        if re.match(r"^\[?\d+\]?$", cl) or len(cl) > 60:
            continue
        if re.match(r"^[a-z]\]$", cl):  # 引用标记如 [1], [a]
            continue
        # 过滤国家队青年队: "Czech Republic U21", "France U19" 等
        is_national_youth = False
        for country in _COUNTRY_NAMES:
            if lower.startswith(country + " ") and re.search(r"\bu[-\s]?\d{1,2}\b", lower):
                is_national_youth = True
                break
        if is_national_youth:
            continue
        seen.add(lower)
        result.append(cl)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 数据库操作
# ══════════════════════════════════════════════════════════════════════════════

def load_db() -> dict:
    if not DB_PATH.exists():
        log.error(f"数据库文件不存在: {DB_PATH}")
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
    refresh_mode = "--refresh" in sys.argv

    # 解析 --limit N 参数
    if "--limit" in sys.argv:
        try:
            idx = sys.argv.index("--limit")
            limit = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            log.error("用法: python enrich_player_careers_v2.py [--refresh] [--limit N]")
            sys.exit(1)

    log.info("=" * 60)
    log.info("球员履历补全 v2 (Transfermarkt API)")
    log.info("=" * 60)

    db = load_db()
    progress = load_progress()

    # 筛选待处理球员
    refresh_mode = "--refresh" in sys.argv

    if refresh_mode:
        pending = [p for p in db["players"] if p.get("is_active", True)]
        log.info("强制刷新模式: 处理所有活跃球员")
    else:
        pending = [
            p for p in db["players"]
            if p.get("is_active", True)
            and (p.get("career") is None or p.get("career") == [])
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

    # 全局俱乐部 ID 缓存 (跨球员复用，减少 API 调用)
    global_club_cache = {}

    for i, player in enumerate(pending):
        pk = player_key(player)
        log.info(f"\n[{i + 1}/{len(pending)}] {player['name']} ({player['country']})")

        try:
            career = []
            current_club = None
            source = "none"

            # ── 数据源 1: Transfermarkt API ──
            log.info(f"    [TM] 搜索...")
            tm_result = tm_search_player(tm, player["name"])

            if tm_result:
                tm_id = tm_result["player_id"]
                slug = tm_result["slug"]
                log.info(f"    [TM] 找到: {tm_result['name']} (ID={tm_id})")

                # Profile 页面: 获取当前俱乐部和 clubId 映射
                log.info(f"    [TM] 获取 Profile...")
                profile = tm_get_profile(tm, slug, tm_id)
                if profile["current_club"]:
                    log.info(f"    [TM] 当前俱乐部: {profile['current_club']}")

                # 合并全局缓存
                profile["club_id_map"].update(global_club_cache)
                global_club_cache.update(profile["club_id_map"])

                # API: 获取转会历史
                log.info(f"    [TM API] 获取转会历史...")
                api_data = tm_get_transfer_history(tm, tm_id)

                if api_data:
                    # 合并缓存
                    api_club_ids = api_data.get("clubIds", [])
                    combined_map = dict(global_club_cache)
                    combined_map.update(profile["club_id_map"])

                    career, current_club = build_career_from_api(
                        api_data, combined_map, tm
                    )

                    # 更新全局缓存
                    global_club_cache.update(combined_map)

                    if career:
                        source = "transfermarkt_api"
                        log.info(f"    [TM API] ✓ {len(career)} 个俱乐部")

                        # 如果有 profile 页面的当前俱乐部且 API 没有给出，用 profile 的
                        if not current_club and profile["current_club"]:
                            current_club = profile["current_club"]
                    else:
                        log.warning(f"    [TM API] 转会历史为空")
                else:
                    log.warning(f"    [TM API] 转会历史获取失败")

                # TM 请求间隔
                time.sleep(random.uniform(*RATE_LIMIT_TM))

            else:
                log.warning(f"    [TM] 未找到")

            # ── 数据源 2: Wikipedia (仅在 TM 无结果时) ──
            if not career and player.get("wiki_url"):
                log.info(f"    [Wiki] TM 无结果，尝试 Wikipedia...")
                wiki_clubs = wiki_get_career_clubs(player["wiki_url"])
                if wiki_clubs:
                    # Wikipedia 没有时间线，只存俱乐部名
                    career = [{"club": c, "from_year": None, "to_year": None} for c in wiki_clubs]
                    current_club = wiki_clubs[-1] if wiki_clubs else None
                    source = "wikipedia"
                    log.info(f"    [Wiki] ✓ {len(wiki_clubs)} 个俱乐部 (无时间线)")
                else:
                    log.warning(f"    [Wiki] 未找到俱乐部记录")

            # ── 写入数据库 ──
            if career:
                player["career"] = career
                player["current_club"] = current_club
                # 保留向后兼容的 clubs 字段 (仅一线队)
                player["clubs"] = [c["club"] for c in career if not c.get("is_youth")]

                # 日志输出: 青年队加 * 标记
                parts = []
                has_years = any(c.get("from_year") for c in career)
                for c in career:
                    if has_years:
                        from_yr = c.get("from_year") or "?"
                        to_yr = c.get("to_year") or "至今"
                        yr = f"{from_yr}-{to_yr}"
                    else:
                        yr = "年份未知"
                    prefix = "*" if c.get("is_youth") else ""
                    parts.append(f"{prefix}{c['club']} ({yr})")
                career_str = " → ".join(parts)

                youth_count = sum(1 for c in career if c.get("is_youth"))
                senior_count = len(career) - youth_count
                log.info(f"    ✓ [{source}] 当前: {current_club}")
                log.info(f"    ✓ 履历: {senior_count} 一线队 + {youth_count} 青训")
                log.info(f"    ✓ {career_str}")

                progress["processed"][pk] = {
                    "source": source,
                    "club_count": len(career),
                    "current_club": current_club,
                    "done_at": datetime.now().isoformat(),
                }
                success_count += 1
            else:
                log.warning(f"    ✗ 未找到任何俱乐部")
                player["career"] = []
                player["current_club"] = None
                player["clubs"] = []
                progress["processed"][pk] = {
                    "source": "none",
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
