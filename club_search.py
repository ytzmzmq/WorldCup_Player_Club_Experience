"""
世界杯球员俱乐部查询工具

功能: 输入俱乐部名称（支持中英文），查看该俱乐部的世界杯球员及其履历
"""

import json
import streamlit as st
from pathlib import Path

# ── 页面配置 ─────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="世界杯球员俱乐部查询",
    page_icon="⚽",
    layout="wide",
)

# ── 数据加载 ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "world_cup_db.json"
ALIAS_PATH = SCRIPT_DIR / "club_aliases.json"


@st.cache_data
def load_data():
    """加载数据库和别名映射，构建反向索引"""
    with open(DB_PATH, "r", encoding="utf-8") as f:
        db = json.load(f)

    aliases = {}
    if ALIAS_PATH.exists():
        with open(ALIAS_PATH, "r", encoding="utf-8") as f:
            aliases = json.load(f)

    # 反向索引: 俱乐部原名 -> 球员列表
    club_index = {}
    # 搜索词(小写) -> set(俱乐部原名)
    search_map = {}

    for player in db["players"]:
        if not player.get("is_active", True):
            continue
        for club in player.get("clubs", []):
            if club not in club_index:
                club_index[club] = []
            club_index[club].append(player)

    for club in club_index:
        club_lower = club.lower()
        search_map.setdefault(club_lower, set()).add(club)
        if club in aliases:
            for alias in aliases[club]:
                search_map.setdefault(alias.lower(), set()).add(club)

    return db, aliases, club_index, search_map


db, aliases, club_index, search_map = load_data()

# ── 搜索逻辑 ──────────────────────────────────────────────────────────────────


def search_clubs(query: str) -> list[tuple[str, int]]:
    """
    搜索俱乐部，返回 [(俱乐部名, 球员数)] 按球员数降序排列。
    精确匹配优先，否则子串模糊匹配。
    """
    q = query.strip().lower()
    if not q:
        return []

    # 精确匹配
    if q in search_map:
        clubs = list(search_map[q])
    else:
        # 子串模糊匹配
        matched = set()
        for term, club_set in search_map.items():
            if q in term or term in q:
                matched.update(club_set)
        clubs = list(matched)

    # 按球员数降序
    result = [(c, len(club_index.get(c, []))) for c in clubs]
    result.sort(key=lambda x: -x[1])
    return result


# ── UI ────────────────────────────────────────────────────────────────────────

st.title("⚽ 2026 世界杯球员俱乐部查询")

active_count = sum(1 for p in db["players"] if p.get("is_active", True))
country_count = len(set(p["country"] for p in db["players"] if p.get("is_active", True)))
st.caption(f"共 {active_count} 名活跃球员，覆盖 {country_count} 支国家队")

query = st.text_input(
    "输入俱乐部名称",
    placeholder="支持中文: 皇马、拜仁、巴黎...  英文: Arsenal, Bayern Munich...",
)

# 输入变化时清除上次选中
if query != st.session_state.get("_last_query"):
    st.session_state.pop("_selected_club", None)
    st.session_state["_last_query"] = query

if query:
    results = search_clubs(query)

    if not results:
        st.warning("未找到匹配的俱乐部，请尝试其他关键词")
    else:
        # 构建下拉选项: "俱乐部名 (中文别名) — N人"
        options = []
        option_map = {}
        for club, count in results[:30]:  # 最多显示30个
            alias_str = ""
            if club in aliases:
                alias_str = f" ({', '.join(aliases[club][:2])})"
            label = f"{club}{alias_str} — {count} 名球员"
            options.append(label)
            option_map[label] = club

        selected_label = st.selectbox(
            f"找到 {len(results)} 个匹配俱乐部（按球员数排序）",
            options=options,
            index=0,
        )

        if selected_label:
            selected_club = option_map[selected_label]
            players = club_index.get(selected_club, [])

            st.divider()
            st.subheader(f"{selected_club} — {len(players)} 名球员")

            # 按国家分组展示
            by_country = {}
            for p in players:
                by_country.setdefault(p["country"], []).append(p)

            for country in sorted(by_country.keys()):
                country_players = by_country[country]
                st.markdown(f"**{country}** ({len(country_players)} 人)")

                for p in country_players:
                    clubs_list = p.get("clubs", [])
                    career = " → ".join(clubs_list) if clubs_list else "暂无数据"
                    current = clubs_list[-1] if clubs_list else "未知"

                    with st.expander(f"{p['name']}（当前: {current}）", expanded=False):
                        st.markdown(f"**完整履历:** {career}")
                        if p.get("wiki_url"):
                            st.markdown(f"[维基百科]({p['wiki_url']})")

                st.markdown("")  # 国家间留白
