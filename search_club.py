"""
2026 FIFA World Cup 俱乐部球员查询工具

根据俱乐部名称，在本地数据库中搜索曾效力于该俱乐部的所有世界杯参赛球员。
支持模糊匹配，按国家队分组输出。

用法:
  python search_club.py --club "Schalke 04"
  python search_club.py --club "manchester united"
  python search_club.py --club "bayern" --threshold 0.6
"""

import argparse
import json
import sys
import io
from pathlib import Path
from difflib import SequenceMatcher

# Windows 终端 UTF-8 兼容
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

DB_PATH = Path(__file__).parent / "world_cup_db.json"

# ── ANSI 颜色 ────────────────────────────────────────────────────────────────
# 在 Windows 10+ 终端和大多数 Unix 终端下可用
C_RESET  = "\033[0m"
C_BOLD   = "\033[1m"
C_DIM    = "\033[2m"
C_RED    = "\033[31m"
C_GREEN  = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE   = "\033[34m"
C_CYAN   = "\033[36m"
C_WHITE  = "\033[37m"

# 国家队旗帜 emoji 映射（常见国家）
_FLAGS = {
    "algeria": "🇩🇿", "argentina": "🇦🇷", "australia": "🇦🇺",
    "austria": "🇦🇹", "belgium": "🇧🇪", "bosnia and herzegovina": "🇧🇦",
    "brazil": "🇧🇷", "canada": "🇨🇦", "cape verde": "🇨🇻",
    "colombia": "🇨🇴", "croatia": "🇭🇷", "curaçao": "🇨🇼",
    "czech republic": "🇨🇿", "dr congo": "🇨🇩", "ecuador": "🇪🇨",
    "egypt": "🇪🇬", "england": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "france": "🇫🇷",
    "germany": "🇩🇪", "ghana": "🇬🇭", "haiti": "🇭🇹",
    "iran": "🇮🇷", "iraq": "🇮🇶", "ivory coast": "🇨🇮",
    "japan": "🇯🇵", "jordan": "🇯🇴", "mexico": "🇲🇽",
    "morocco": "🇲🇦", "netherlands": "🇳🇱", "new zealand": "🇳🇿",
    "norway": "🇳🇴", "panama": "🇵🇦", "paraguay": "🇵🇾",
    "portugal": "🇵🇹", "qatar": "🇶🇦", "saudi arabia": "🇸🇦",
    "scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "senegal": "🇸🇳", "south africa": "🇿🇦",
    "south korea": "🇰🇷", "spain": "🇪🇸", "sweden": "🇸🇪",
    "switzerland": "🇨🇭", "tunisia": "🇹🇳", "turkey": "🇹🇷",
    "united states": "🇺🇸", "uruguay": "🇺🇾", "uzbekistan": "🇺🇿",
}


def get_flag(country: str) -> str:
    return _FLAGS.get(country.lower(), "🏳️")


# ── 模糊匹配 ─────────────────────────────────────────────────────────────────

def club_match(query: str, club_name: str, threshold: float = 0.6) -> bool:
    """
    判断查询词是否与俱乐部名匹配。
    规则（满足任一即命中）:
    1. 查询词整体是俱乐部名的子串（忽略大小写）
    2. 俱乐部名整体是查询词的子串
    3. 查询词拆分后，有 >=50% 的词出现在俱乐部名中
    4. SequenceMatcher 相似度 >= threshold
    """
    q = query.lower().strip()
    c = club_name.lower().strip()

    # 整体子串匹配
    if q in c or c in q:
        return True

    # 逐词匹配: 查询词的多数词出现在俱乐部名中
    q_words = [w for w in q.split() if len(w) > 1]  # 忽略单字符词
    if q_words:
        c_set = set(c.split())
        hits = sum(1 for w in q_words if w in c_set)
        if hits >= len(q_words) * 0.5:
            return True

    # 模糊相似度
    ratio = SequenceMatcher(None, q, c).ratio()
    return ratio >= threshold


def search_players(db: dict, query: str, threshold: float) -> list[dict]:
    """搜索匹配的球员，返回带 matched_clubs 字段的列表"""
    results = []

    for player in db["players"]:
        if not player.get("is_active", True):
            continue
        clubs = player.get("clubs") or []
        if not clubs:
            continue

        matched = [c for c in clubs if club_match(query, c, threshold)]
        if matched:
            results.append({
                **player,
                "matched_clubs": matched,
            })

    return results


# ── 格式化输出 ────────────────────────────────────────────────────────────────

def print_results(query: str, results: list[dict], db: dict):
    """按国家队分组，美观输出"""

    if not results:
        enriched_count = sum(1 for p in db["players"] if p.get("clubs"))
        total_count = len(db["players"])
        print()
        print(f"  {C_YELLOW}⚠  未找到效力于 \"{query}\" 的世界杯参赛球员{C_RESET}")
        print()
        print(f"  {C_DIM}当前已补全履历: {enriched_count} / {total_count} 名球员。{C_RESET}")
        if enriched_count < total_count:
            print(f"  {C_DIM}请运行 enrich_player_careers.py 补全球员履历后重试。{C_RESET}")
        print()
        return

    # 按国家队分组
    groups: dict[str, list[dict]] = {}
    for p in results:
        country = p["country"]
        groups.setdefault(country, []).append(p)

    # 排序: 按国家名, 组内按球员名
    sorted_countries = sorted(groups.keys())

    # ── 标题 ──
    print()
    print(f"{C_BOLD}{C_CYAN}╔{'═' * 62}╗{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}║{C_RESET}{C_BOLD}  🏆 2026 FIFA World Cup — 俱乐部球员查询{C_RESET}{' ' * 18}{C_BOLD}{C_CYAN}║{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}╠{'═' * 62}╣{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}║{C_RESET}  {C_WHITE}查询俱乐部:{C_RESET}  {C_GREEN}{C_BOLD}{query}{C_RESET}{' ' * max(0, 49 - len(query))}{C_BOLD}{C_CYAN}║{C_RESET}")
    total = len(results)
    nations = len(sorted_countries)
    print(f"{C_BOLD}{C_CYAN}║{C_RESET}  {C_WHITE}命中球员:{C_RESET}    {C_YELLOW}{C_BOLD}{total}{C_RESET} 人，来自 {C_YELLOW}{nations}{C_RESET} 支国家队{' ' * max(0, 26 - len(str(total)) - len(str(nations)))}{C_BOLD}{C_CYAN}║{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}╚{'═' * 62}╝{C_RESET}")

    # ── 按国家输出 ──
    for country in sorted_countries:
        players = sorted(groups[country], key=lambda x: x["name"])
        flag = get_flag(country)

        print()
        print(f"  {C_BOLD}{flag}  {country}{C_RESET}  {C_DIM}({len(players)} 人){C_RESET}")
        print(f"  {C_DIM}{'─' * 56}{C_RESET}")

        for p in players:
            name = p["name"]
            matched = p["matched_clubs"]
            # 高亮匹配的俱乐部名
            matched_str = ", ".join(
                f"{C_GREEN}{C_BOLD}{c}{C_RESET}" for c in matched
            )
            # 显示全部俱乐部（未匹配的灰色）
            all_clubs = p.get("clubs", [])
            other = [c for c in all_clubs if c not in matched]
            if other:
                other_str = f"  {C_DIM}(其他: {', '.join(other)}){C_RESET}"
            else:
                other_str = ""

            print(f"    {C_WHITE}●{C_RESET} {C_BOLD}{name}{C_RESET}")
            print(f"      {C_CYAN}▸{C_RESET} {matched_str}{other_str}")

    # ── 底部统计 ──
    print()
    print(f"  {C_DIM}{'─' * 60}{C_RESET}")
    print(f"  {C_DIM}共 {total} 名球员来自 {nations} 支国家队曾效力于 \"{query}\"{C_RESET}")
    print()


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="2026 FIFA World Cup 俱乐部球员查询工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python search_club.py --club "Bayern Munich"
  python search_club.py --club "slavia"
  python search_club.py --club "manchester united" --threshold 0.5
        """,
    )
    parser.add_argument(
        "--club", required=True,
        help="要查询的俱乐部名称（支持模糊匹配）",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.6,
        help="模糊匹配相似度阈值，0~1，默认 0.6",
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="数据库文件路径，默认同目录下的 world_cup_db.json",
    )

    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DB_PATH
    if not db_path.exists():
        print(f"{C_RED}错误: 数据库文件不存在: {db_path}{C_RESET}", file=sys.stderr)
        print(f"{C_DIM}请先运行 update_world_cup_db.py 和 enrich_player_careers.py{C_RESET}", file=sys.stderr)
        sys.exit(1)

    with open(db_path, "r", encoding="utf-8") as f:
        db = json.load(f)

    results = search_players(db, args.club, args.threshold)
    print_results(args.club, results, db)


if __name__ == "__main__":
    main()
