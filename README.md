# 2026 FIFA 世界杯球员俱乐部履历库

追踪 2026 美加墨世界杯全部 48 支参赛队、1248 名球员的俱乐部履历，并提供支持中英文搜索的在线查询工具。

## 数据概览

| 指标 | 数量 |
|------|------|
| 参赛国家队 | 48 |
| 活跃球员 | 1248 |
| 涵盖俱乐部 | 1956 |
| 已补全履历 | 1239 |
| 中文别名映射 | 1763 |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动网页查询
streamlit run club_search.py
```

打开 http://localhost:8501 ，输入俱乐部名称即可搜索。支持中文（"皇马""拜仁""巴黎"）和英文（"Arsenal""Bayern Munich"）。

## 工具链

项目由四个脚本组成，前两个构成数据流水线，后两个面向使用：

**数据流水线：**

```
维基百科名单页 ──▶ update_world_cup_db.py ──▶ world_cup_db.json
                                                     │
                      Transfermarkt API + Wikipedia   │
                              │                      ▼
                    enrich_player_careers_v2.py ──▶ 俱乐部履历补全
```

| 脚本 | 功能 | 依赖 |
|------|------|------|
| `update_world_cup_db.py` | 从维基百科抓取最新大名单，增量更新数据库 | `requests`, `beautifulsoup4` |
| `enrich_player_careers_v2.py` | 从 Transfermarkt API 补全球员俱乐部履历（含时间线），Wikipedia 作为备选，支持断点续跑 | `cloudscraper`, `beautifulsoup4`, `requests` |

**查询工具：**

| 脚本 | 功能 | 依赖 |
|------|------|------|
| `club_search.py` | Streamlit 网页应用，中英文俱乐部搜索 | `streamlit` |
| `search_club.py` | 命令行查询工具 | — |

```bash
# 命令行查询示例
python search_club.py --club "曼联"
python search_club.py --club "Bayern Munich"
python search_club.py --club "barcelona" --threshold 0.6
```

## 核心数据文件

| 文件 | 说明 |
|------|------|
| `world_cup_db.json` | 主数据库，存储所有球员信息及俱乐部履历 |
| `club_aliases.json` | 俱乐部中英文别名映射（覆盖五大联赛、中超、日韩、南美、中东等） |
| `new_players.json` | 最近一次更新产生的新增球员列表 |
| `enrich_progress.json` | 履历补全进度记录（断点续跑用） |

## 环境要求

- Python 3.10+
- 完整依赖见 `requirements.txt`
