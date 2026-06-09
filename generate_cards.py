"""
沙尔克04 × 2026世界杯 — 小红书球员卡片批量生成器 v3
================================================
依赖: pip install Pillow opencv-python
字体: 使用 Windows 系统自带的微软雅黑 (msyh.ttc / msyhbd.ttc)
用法: python generate_cards.py
"""

import os
import sys
import json
from pathlib import Path
from urllib.request import urlretrieve

# Windows 终端 UTF-8 输出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ============================================================
# 全局配置
# ============================================================

# 画布尺寸 (小红书 3:4)
CARD_W = 1080
CARD_H = 1440

# 主题色
BG_COLOR_TOP    = (0, 30, 75)      # 深蓝渐变起点
BG_COLOR_BOTTOM = (0, 77, 157)     # 沙尔克皇家蓝 #004D9D
TEXT_COLOR       = (255, 255, 255)
ACCENT_COLOR     = (100, 170, 255)  # 浅蓝强调色
SUBTITLE_COLOR   = (160, 200, 255)  # 副标题颜色
DIVIDER_COLOR    = (60, 120, 200)   # 分隔线颜色
FOOTER_COLOR     = (130, 155, 190)  # 底部水印颜色
FOREIGN_NAME_COLOR = (140, 180, 230)  # 外文名颜色 (柔和蓝)

# 布局参数
IMAGE_RATIO   = 0.48          # 球员图片占卡片高度的比例
GRADIENT_H    = 140           # 渐变过渡区域高度
MARGIN_X      = 72            # 左右边距
CONTENT_W     = CARD_W - 2 * MARGIN_X

# 字体路径
FONT_BOLD   = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_REGULAR = r"C:\Windows\Fonts\msyh.ttc"

# 字号
TITLE_SIZE      = 56   # 中文名
FOREIGN_NAME_SIZE = 30 # 外文名
POSITION_SIZE   = 26   # 位置信息
SUBTITLE_SIZE   = 28   # 小标题
BODY_SIZE       = 30   # 正文
FOOTER_SIZE     = 20   # 底部水印

# 国旗尺寸
FLAG_H = 44  # 国旗高度

# 路径
BASE_DIR    = Path(__file__).parent.resolve()
PIC_DIR     = BASE_DIR / "pictures"
FLAG_DIR    = BASE_DIR / "flags"
DATA_FILE   = BASE_DIR / "players_data.json"
OUTPUT_DIR  = BASE_DIR / "output_cards"

# 国家 → ISO 3166-1 alpha-2 代码 (用于下载国旗)
COUNTRY_ISO = {
    "德国": "de", "波黑": "ba", "土耳其": "tr", "阿尔及利亚": "dz",
    "澳大利亚": "au", "奥地利": "at", "加纳": "gh", "日本": "jp",
    "南非": "za", "韩国": "kr", "瑞士": "ch", "美国": "us", "乌拉圭": "uy",
}


# ============================================================
# 工具函数
# ============================================================

def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """加载字体，兼容 .ttf 和 .ttc"""
    try:
        return ImageFont.truetype(path, size)
    except Exception as e:
        print(f"  [警告] 无法加载字体 {path}: {e}")
        return ImageFont.load_default()


def wrap_text_cn(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """中文友好的自动换行：逐字符测量宽度折行，不拆断英文单词"""
    lines, current = [], ""
    for char in text:
        test = current + char
        bbox = font.getbbox(test)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def draw_text_block(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
    x: int, y: int, max_width: int, fill=TEXT_COLOR, line_spacing: int = 12,
) -> int:
    """绘制自动换行文本块，返回底部 Y 坐标"""
    lines = wrap_text_cn(text, font, max_width)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        bbox = font.getbbox(line)
        y += (bbox[3] - bbox[1]) + line_spacing
    return y


def measure_text_block(text: str, font: ImageFont.FreeTypeFont,
                       max_width: int, line_spacing: int = 12) -> int:
    """测量文本块总高度（不绘制）"""
    lines = wrap_text_cn(text, font, max_width)
    h = 0
    for line in lines:
        bbox = font.getbbox(line)
        h += (bbox[3] - bbox[1]) + line_spacing
    return h


# ============================================================
# 国旗下载
# ============================================================

def download_flags():
    """下载所有需要的国旗图片 (64px 宽, PNG)"""
    FLAG_DIR.mkdir(parents=True, exist_ok=True)
    for country, code in COUNTRY_ISO.items():
        flag_path = FLAG_DIR / f"{code}.png"
        if not flag_path.exists():
            url = f"https://flagcdn.com/160x120/{code}.png"
            try:
                urlretrieve(url, str(flag_path))
                print(f"  [OK] 下载国旗: {country} ({code}.png)")
            except Exception as e:
                print(f"  [失败] 无法下载 {country} 国旗: {e}")


# ============================================================
# 图像处理：人脸检测 + 智能裁剪
# ============================================================

def detect_face_center(image_path: str):
    """OpenCV Haar 人脸检测，通过 numpy 中间层兼容 Unicode 路径"""
    try:
        raw = np.fromfile(str(image_path), dtype=np.uint8)
        img_cv = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    except Exception:
        img_cv = cv2.imread(str(image_path))
    if img_cv is None:
        return None

    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return (x + w // 2, y + h // 2)


def smart_crop(img: Image.Image, face_center, target_w: int, target_h: int):
    """智能裁剪：优先保证人脸在画面内，否则偏上居中"""
    orig_w, orig_h = img.size
    target_ratio = target_w / target_h

    if orig_w / orig_h > target_ratio:
        crop_h = orig_h
        crop_w = int(crop_h * target_ratio)
    else:
        crop_w = orig_w
        crop_h = int(crop_w / target_ratio)

    cx = orig_w // 2
    left = max(0, min(cx - crop_w // 2, orig_w - crop_w))

    if face_center is not None:
        _, fy = face_center
        top = int(fy - crop_h * 0.30)
    else:
        top = int(orig_h * 0.10)
    top = max(0, min(top, orig_h - crop_h))

    cropped = img.crop((left, top, left + crop_w, top + crop_h))
    return cropped.resize((target_w, target_h), Image.LANCZOS)


# ============================================================
# 渐变融合 + 背景
# ============================================================

def apply_gradient_blend(card, player_img, img_area_h, gradient_h):
    """球员图片底部 Alpha 渐变融入背景"""
    if player_img.mode != "RGBA":
        player_img = player_img.convert("RGBA")
    mask = Image.new("L", (CARD_W, img_area_h), 255)
    mask_draw = ImageDraw.Draw(mask)
    blend_start = img_area_h - gradient_h
    for y in range(blend_start, img_area_h):
        alpha = int(255 * (1 - (y - blend_start) / gradient_h))
        mask_draw.line([(0, y), (CARD_W, y)], fill=alpha)
    player_img.putalpha(mask)
    card.paste(player_img, (0, 0), player_img)
    return card


def draw_gradient_background(draw):
    """垂直渐变背景"""
    for y in range(CARD_H):
        r = y / CARD_H
        c = tuple(int(BG_COLOR_TOP[i] + (BG_COLOR_BOTTOM[i] - BG_COLOR_TOP[i]) * r)
                  for i in range(3))
        draw.line([(0, y), (CARD_W, y)], fill=c)


# ============================================================
# 单张卡片生成
# ============================================================

def generate_card(player: dict, output_dir: Path) -> bool:
    """为单个球员生成一张小红书卡片"""
    name        = player["name"]
    name_cn     = player["name_cn"]
    nationality = player.get("nationality", "")
    position    = player.get("position", "")
    image_file  = player["image_file"]
    schalke_yrs = player["schalke_years"]
    schalke_txt = player["schalke_story"]
    after_txt   = player["after_story"]

    img_path = PIC_DIR / image_file
    if not img_path.exists():
        print(f"  [跳过] {name_cn}: 图片不存在 -> {img_path}")
        return False

    # ── 加载字体 ──
    font_title    = load_font(FONT_BOLD, TITLE_SIZE)
    font_foreign  = load_font(FONT_REGULAR, FOREIGN_NAME_SIZE)
    font_pos      = load_font(FONT_REGULAR, POSITION_SIZE)
    font_sub      = load_font(FONT_BOLD, SUBTITLE_SIZE)
    font_body     = load_font(FONT_REGULAR, BODY_SIZE)
    font_footer   = load_font(FONT_REGULAR, FOOTER_SIZE)

    # ── 加载国旗 ──
    iso = COUNTRY_ISO.get(nationality, "")
    flag_img = None
    if iso:
        flag_path = FLAG_DIR / f"{iso}.png"
        if flag_path.exists():
            try:
                flag_img = Image.open(flag_path).convert("RGBA")
                # 缩放到目标高度，保持宽高比
                fw_orig, fh_orig = flag_img.size
                flag_w = int(FLAG_H * fw_orig / fh_orig)
                flag_img = flag_img.resize((flag_w, FLAG_H), Image.LANCZOS)
            except Exception:
                flag_img = None

    # ── 画布 + 渐变背景 ──
    card = Image.new("RGBA", (CARD_W, CARD_H), BG_COLOR_BOTTOM)
    draw = ImageDraw.Draw(card)
    draw_gradient_background(draw)

    # ── 处理球员图片 ──
    print(f"  处理图片: {image_file}")
    player_img = Image.open(img_path).convert("RGB")
    face = detect_face_center(img_path)
    print(f"    {'检测到人脸: ' + str(face) if face else '未检测到人脸，偏上居中裁剪'}")

    img_area_h = int(CARD_H * IMAGE_RATIO)
    player_img = smart_crop(player_img, face, CARD_W, img_area_h)
    card = apply_gradient_blend(card, player_img, img_area_h, GRADIENT_H)

    # ── 计算文字布局：动态分配空间填满卡片 ──
    title_h    = font_title.getbbox("X")[3] + 8
    foreign_h  = font_foreign.getbbox("X")[3] + 4
    sub1_h     = font_sub.getbbox("X")[3] + 16
    pos_h      = font_pos.getbbox("X")[3] + 8
    story1_h   = measure_text_block(schalke_txt, font_body, CONTENT_W, 14)
    story2_h   = measure_text_block(after_txt, font_body, CONTENT_W, 14)
    footer_space = 56

    # 可用文字区域
    text_area_top = img_area_h + 24
    text_area_bottom = CARD_H - footer_space
    available_h = text_area_bottom - text_area_top

    # 固定内容高度 (含外文名)
    fixed_h = (title_h + 6 + foreign_h + 18  # 中文名 + 外文名
               + pos_h + 24                    # 位置
               + sub1_h + 12                   # 副标题1
               + story1_h + 28                 # 正文1 + 间距
               + sub1_h + 12                   # 副标题2
               + story2_h)                     # 正文2

    # 计算额外间距来填满卡片
    extra_space = available_h - fixed_h
    section_gap = max(20, min(48, 20 + extra_space * 0.3))
    sub_to_body = max(10, min(20, 10 + extra_space * 0.1))

    # ── 开始绘制文字 ──
    text_y = text_area_top

    # 装饰短线
    draw.line([(MARGIN_X, text_y - 8), (MARGIN_X + 64, text_y - 8)],
              fill=ACCENT_COLOR, width=4)

    # ── 标题：中文名 + 国旗 ──
    draw.text((MARGIN_X, text_y), name_cn, font=font_title, fill=TEXT_COLOR)

    # 国旗 (右对齐)
    if flag_img:
        flag_x = CARD_W - MARGIN_X - flag_img.width
        # 垂直居中对齐标题行
        flag_y = text_y + (title_h - flag_img.height) // 2
        card.paste(flag_img, (flag_x, flag_y), flag_img)
        draw = ImageDraw.Draw(card)  # 刷新 draw 绑定

    text_y += title_h + 6

    # ── 外文名 ──
    draw.text((MARGIN_X, text_y), name, font=font_foreign, fill=FOREIGN_NAME_COLOR)
    text_y += foreign_h + 18

    # ── 位置信息 (无号码) ──
    draw.text((MARGIN_X, text_y), position, font=font_pos, fill=ACCENT_COLOR)
    text_y += pos_h + section_gap

    # ── 副标题1 + 正文1 ──
    sub1 = f">> 矿厂岁月 ({schalke_yrs})"
    draw.text((MARGIN_X, text_y), sub1, font=font_sub, fill=SUBTITLE_COLOR)
    text_y += sub1_h + sub_to_body

    text_y = draw_text_block(draw, schalke_txt, font_body,
                             MARGIN_X, text_y, CONTENT_W, TEXT_COLOR, 14)
    text_y += section_gap

    # 分隔线
    line_y = text_y - section_gap // 2
    draw.line([(MARGIN_X, line_y), (CARD_W - MARGIN_X, line_y)],
              fill=(*DIVIDER_COLOR, 80), width=2)

    # ── 副标题2 + 正文2 ──
    sub2 = ">> 现状与世界杯"
    draw.text((MARGIN_X, text_y), sub2, font=font_sub, fill=SUBTITLE_COLOR)
    text_y += sub1_h + sub_to_body

    text_y = draw_text_block(draw, after_txt, font_body,
                             MARGIN_X, text_y, CONTENT_W, TEXT_COLOR, 14)

    # ── 底部水印 ──
    footer = "Auf geht's, Schalke!  |  2026 World Cup"
    fbbox = font_footer.getbbox(footer)
    fw = fbbox[2] - fbbox[0]
    draw.text(((CARD_W - fw) // 2, CARD_H - 46), footer,
              font=font_footer, fill=(*FOOTER_COLOR, 160))

    # ── 保存 ──
    safe_name = name.replace(" ", "_").replace("'", "")
    out_path = output_dir / f"{safe_name}_card.png"
    card.convert("RGB").save(out_path, "PNG", quality=95)
    print(f"  [OK] 已生成: {out_path.name}")
    return True


# ============================================================
# 主入口
# ============================================================

def main():
    print("=" * 60)
    print("  沙尔克04 x 2026世界杯 — 小红书球员卡片生成器 v3")
    print("=" * 60)

    if not DATA_FILE.exists():
        print(f"[错误] 数据文件不存在: {DATA_FILE}")
        sys.exit(1)

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        players = json.load(f)
    print(f"\n加载了 {len(players)} 名球员数据")

    # 下载国旗
    print("\n下载国旗图片...")
    download_flags()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n输出目录: {OUTPUT_DIR}\n")

    success = skipped = 0
    for i, player in enumerate(players, 1):
        print(f"[{i:02d}/{len(players)}] {player['name_cn']} ({player['name']})")
        if generate_card(player, OUTPUT_DIR):
            success += 1
        else:
            skipped += 1
        print()

    print("=" * 60)
    print(f"  生成完毕: 成功 {success} 张, 跳过 {skipped} 张")
    print(f"  输出目录: {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
