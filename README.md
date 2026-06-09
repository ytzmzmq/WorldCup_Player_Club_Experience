# 世界杯球员俱乐部查询 ⚽

查询 2026 美加墨世界杯参赛球员的俱乐部经历。输入俱乐部名称（支持中英文），即可查看曾效力于该俱乐部的所有世界杯球员。

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 Streamlit 网页应用
streamlit run club_search.py
```

## CLI 查询

```bash
python search_club.py --club "曼联"
python search_club.py --club "Bayern Munich"
python search_club.py --club "barcelona" --threshold 0.6
```

## 数据来源

- 球员数据：Wikipedia + Transfermarkt 混合抓取
- 覆盖范围：2026 世界杯所有参赛球队（1251 名球员）
- 俱乐部别名：支持中英文、简称等多种写法

## 项目结构

| 文件 | 说明 |
|------|------|
| `club_search.py` | Streamlit 网页查询工具 |
| `search_club.py` | 命令行查询工具 |
| `world_cup_db.json` | 世界杯球员数据库 |
| `club_aliases.json` | 俱乐部中英文别名映射 |
| `update_world_cup_db.py` | 数据库更新脚本 |
| `enrich_player_careers.py` | 球员生涯数据扩充脚本 |
