import os
import re
import json
from pathlib import Path
from typing import List, Tuple, Dict, Any

import pdfplumber
from PyPDF2 import PdfReader
from PIL import Image, ImageFilter, ImageStat
import imagehash

PDF_DIR = r"pdf"
OUTPUT_DIR = r"raw_img"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ========== 可配置参数（保持与原版一致） ==========
MIN_IMG_WIDTH = 200    # px
MIN_IMG_HEIGHT = 100   # px

HASH_THRESHOLD = 5
HASH_FUNC = imagehash.phash  # 可替换为 ahash / dhash / whash 等

# ========== 规则与阈值（新增识别策略，但不影响尺寸与去重“参数”） ==========
SEARCH_MARGIN = 30          # caption 搜索窗口的基本扩展
LINE_GAP_STOP = 18          # 同向逐行扫描的行距停止阈值
WORD_JOIN_GAP = 6           # 行内词拼接间隔
CAPTION_MIN_LEN = 8         # caption 最短长度

MERGE_DIST = 12             # 矢量元素近邻合并距离
MIN_GRAPHIC_W = 120         # 认为是图/表的最小宽度
MIN_GRAPHIC_H = 80          # 认为是图/表的最小高度

CHART_LINE_MIN = 6          # 折线/坐标轴线段阈值
BAR_RECT_MIN = 3            # 柱状图矩形阈值
GRID_HV_MIN = (3, 3)        # 表格网格近似阈值（水平+垂直线）

# 英中标签模式
LABEL_REGEX = re.compile(
    r'^\s*(?:Fig(?:\.|ure)?|Figs?|Table|Tab\.?|图表|图|表)\s*([0-9]+(?:[.\-][0-9A-Za-z]+)*)',
    re.I
)

# ========== 文本与通用工具函数 ==========

def extract_text(pdf_path):
    reader = PdfReader(pdf_path)
    text_pages = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text_pages.append(text)
    return text_pages, "\n".join(text_pages)


def split_sentences(text: str):
    # 英文简单句切分，中文基本不切
    pattern = r'(?<=[.?!])\s+(?=(?:[A-Z]|[^0-9]))'
    sentences = re.split(pattern, text)
    return [s.strip() for s in sentences if s.strip()]


def _extract_origin_id_from_caption(caption: str) -> str:
    m = LABEL_REGEX.match(caption or "")
    if m:
        prefix = re.match(r'^\s*(Fig(?:\.|ure)?|Figs?|Table|Tab\.?|图表|图|表)', caption, re.I).group(1)
        number = m.group(1)
        return f"{prefix} {number}"
    return ""


def _normalize_label_for_filename(caption: str) -> str:
    """
    返回标准化标签文本，用于文件名的第三段：
      - 英文转大写，如 FIGURE 2.1 / TABLE 4.3
      - 中文保持原样，如 图 2.1 / 表 3.4
    """
    if not caption:
        return ""
    m = LABEL_REGEX.match(caption)
    if not m:
        return ""
    prefix = re.match(r'^\s*(Fig(?:\.|ure)?|Figs?|Table|Tab\.?|图表|图|表)', caption, re.I).group(1)
    number = m.group(1)
    if re.match(r'^(Fig(?:\.|ure)?|Figs?|Table|Tab\.?)$', prefix, re.I):
        prefix_norm = prefix.upper().replace("FIG.", "FIG").replace("TAB.", "TABLE")
        if prefix_norm == "FIGS":
            prefix_norm = "FIGURES"
        return f"{prefix_norm} {number}"
    # 中文
    return f"{prefix} {number}"


def _sanitize_for_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', '_', name)


def _bbox_expand(b: Tuple[float, float, float, float], margin: float) -> Tuple[float, float, float, float]:
    x0, y0, x1, y1 = b
    return (x0 - margin, y0 - margin, x1 + margin, y1 + margin)


def _bbox_union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _bbox_area(b):
    return max(0.0, b[2]-b[0]) * max(0.0, b[3]-b[1])


def _bbox_center(b):
    return ((b[0]+b[2]) * 0.5, (b[1]+b[3]) * 0.5)


def _bbox_dist(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    dx = max(0, max(bx0 - ax1, ax0 - bx1))
    dy = max(0, max(by0 - ay1, ay0 - by1))
    return (dx ** 2 + dy ** 2) ** 0.5


def _clip_to_page(b, page):
    x0, y0, x1, y1 = b
    px0, py0, px1, py1 = page.bbox  # (0, 0, w, h) 或类似
    x0 = max(px0, min(x0, px1))
    y0 = max(py0, min(y0, py1))
    x1 = max(px0, min(x1, px1))
    y1 = max(py0, min(y1, py1))
    if x1 - x0 <= 1 or y1 - y0 <= 1:
        return None
    return (x0, y0, x1, y1)


def _words_in_rect(words, rect):
    x0, y0, x1, y1 = rect
    return [w for w in (words or []) if (w["x0"] >= x0 and w["x1"] <= x1 and w["top"] >= y0 and w["bottom"] <= y1)]


def _join_words_linewise(words: List[Dict[str, Any]]) -> str:
    if not words:
        return ""
    words_sorted = sorted(words, key=lambda w: (round(w["top"]/5)*5, w["x0"]))
    lines = []
    current_line = []
    last_top = None
    last_x1 = None
    for w in words_sorted:
        if last_top is None:
            current_line = [w]
            last_top = w["top"]
            last_x1 = w["x1"]
            continue
        if abs(w["top"] - last_top) <= WORD_JOIN_GAP:
            if w["x0"] - last_x1 > WORD_JOIN_GAP:
                current_line.append({"text": " "})
            current_line.append(w)
            last_x1 = w["x1"]
        else:
            lines.append("".join(x["text"] for x in current_line))
            current_line = [w]
            last_top = w["top"]
            last_x1 = w["x1"]
    if current_line:
        lines.append("".join(x["text"] for x in current_line))
    return " ".join(s.strip() for s in lines if s.strip())


def _pick_best_caption(candidates: List[Tuple[str, float]]) -> str:
    if not candidates:
        return ""
    def score(item):
        text, dist = item
        has_label = 1 if LABEL_REGEX.match(text or "") else 0
        length_penalty = max(0, len(text) - 260) * 0.002
        return (has_label * 10) - (dist * 0.01) - length_penalty
    best = max(candidates, key=score)
    return best[0]


def _truncate_caption_like(text: str) -> str:
    if not text:
        return ""
    cut = re.split(r'(?<=[。．.!?;；])\s+', text, maxsplit=1)
    return cut[0].strip()


def _find_caption_four_directions(page, words, bbox) -> str:
    x0, y0, x1, y1 = bbox
    page_w, page_h = page.width, page.height
    regions = {
        "bottom": (x0, y1, x1, min(page_h, y1 + 3 * SEARCH_MARGIN)),
        "top":    (x0, max(0, y0 - 3 * SEARCH_MARGIN), x1, y0),
        "right":  (x1, max(0, y0 - SEARCH_MARGIN), min(page_w, x1 + 3 * SEARCH_MARGIN), min(page_h, y1 + SEARCH_MARGIN)),
        "left":   (max(0, x0 - 3 * SEARCH_MARGIN), max(0, y0 - SEARCH_MARGIN), x0, min(page_h, y1 + SEARCH_MARGIN)),
    }
    candidates = []
    for side, rect in regions.items():
        local_words = _words_in_rect(words, rect)
        if not local_words:
            continue
        text_full = _join_words_linewise(local_words)
        if not text_full or len(text_full) < CAPTION_MIN_LEN:
            continue
        axis_sorted = sorted(local_words, key=lambda w: (w["top"], w["x0"])) if side in ("bottom", "top") \
                      else sorted(local_words, key=lambda w: (w["x0"], w["top"]))
        lines = []
        last_axis = None
        for w in axis_sorted:
            cur_axis = w["top"] if side in ("bottom", "top") else w["x0"]
            if last_axis is None:
                lines.append(w["text"])
                last_axis = cur_axis
            else:
                gap = abs(cur_axis - last_axis)
                if gap > LINE_GAP_STOP:
                    break
                lines.append(w["text"])
                last_axis = cur_axis
        text_candidate = re.sub(r'\s+', ' ', " ".join(lines)).strip()
        if len(text_candidate) >= CAPTION_MIN_LEN:
            if side == "bottom":
                dist = rect[1] - y1
            elif side == "top":
                dist = y0 - rect[3]
            elif side == "right":
                dist = rect[0] - x1
            else:
                dist = x0 - rect[2]
            dist = max(0.0, float(dist))
            candidates.append((text_candidate, dist))
    best = _pick_best_caption(candidates)
    if best:
        best = _truncate_caption_like(best)
    return best

# ========== 矢量元素聚类与类型启发 ==========
def _gather_graphic_boxes(page) -> List[Tuple[float, float, float, float, Dict[str, int]]]:
    boxes = []
    primitives = []
    for ln in getattr(page, "lines", []):
        primitives.append(((ln["x0"], ln["top"], ln["x1"], ln["bottom"]), {"lines": 1, "rects": 0, "curves": 0}))
    for rc in getattr(page, "rects", []):
        primitives.append(((rc["x0"], rc["top"], rc["x1"], rc["bottom"]), {"lines": 0, "rects": 1, "curves": 0}))
    for cv in getattr(page, "curves", []):
        primitives.append(((cv["x0"], cv["top"], cv["x1"], cv["bottom"]), {"lines": 0, "rects": 0, "curves": 1}))

    clusters: List[Tuple[Tuple[float, float, float, float], Dict[str, int]]] = []
    for bbox, stat in primitives:
        merged = False
        ex_bbox = _bbox_expand(bbox, MERGE_DIST)
        for i, (cb, cstat) in enumerate(clusters):
            if _bbox_dist(ex_bbox, cb) <= MERGE_DIST:
                new_bbox = _bbox_union(cb, bbox)
                new_stat = {
                    "lines": cstat["lines"] + stat["lines"],
                    "rects": cstat["rects"] + stat["rects"],
                    "curves": cstat["curves"] + stat["curves"],
                }
                clusters[i] = (new_bbox, new_stat)
                merged = True
                break
        if not merged:
            clusters.append((bbox, stat))
    for cb, st in clusters:
        w = cb[2] - cb[0]
        h = cb[3] - cb[1]
        if w >= MIN_GRAPHIC_W and h >= MIN_GRAPHIC_H:
            boxes.append((cb[0], cb[1], cb[2], cb[3], st))
    return boxes


def _is_likely_chart(stats: Dict[str, int]) -> bool:
    return (stats.get("lines", 0) + stats.get("curves", 0)) >= CHART_LINE_MIN


def _is_likely_barchart(stats: Dict[str, int]) -> bool:
    return stats.get("rects", 0) >= BAR_RECT_MIN


def _is_likely_table(stats: Dict[str, int]) -> bool:
    return (stats.get("lines", 0) >= GRID_HV_MIN[0] + GRID_HV_MIN[1]) or \
           (stats.get("lines", 0) >= GRID_HV_MIN[0] and stats.get("rects", 0) >= GRID_HV_MIN[1])

# ========== 文本块过滤（减少乱截图） ==========
def _text_and_edge_score(words, bbox, pil_img):
    bx0, by0, bx1, by1 = bbox
    area = max(1.0, (bx1 - bx0) * (by1 - by0))
    w_in = _words_in_rect(words, bbox)
    text_area = 0.0
    for w in w_in:
        ww = max(0.0, w["x1"] - w["x0"])
        hh = max(0.0, w["bottom"] - w["top"])
        text_area += ww * hh
    text_ratio = min(1.0, text_area / area)

    edges = pil_img.convert("L").filter(ImageFilter.FIND_EDGES)
    mean_edge = ImageStat.Stat(edges).mean[0] / 255.0
    return text_ratio, mean_edge


def _looks_like_plain_text(text_ratio, mean_edge, stats_hint=None):
    if stats_hint:
        if (stats_hint.get("lines", 0) + stats_hint.get("curves", 0)) >= 6:
            return False
        if stats_hint.get("rects", 0) >= 3:
            return False
    return (text_ratio > 0.55 and mean_edge < 0.10)

# ========== 主提取 ==========
def extract_images_and_captions(pdf_path, out_dir):
    figures = []
    stem = Path(pdf_path).stem
    kept_hashes = []  # 用集合逻辑去重（阈值/算法不变）

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(x_tolerance=1, y_tolerance=2) or []

            # 来源1：位图
            raster_boxes = [(img["x0"], img["top"], img["x1"], img["bottom"]) for img in page.images]

            # 来源2：矢量聚类
            graphic_boxes_stats = _gather_graphic_boxes(page)
            graphic_boxes = [(gx0, gy0, gx1, gy1) for (gx0, gy0, gx1, gy1, _st) in graphic_boxes_stats]

            # 合并两类候选，做几何合并
            candidates: List[Tuple[float, float, float, float]] = []
            for b in raster_boxes + graphic_boxes:
                merged = False
                for i, cb in enumerate(candidates):
                    inter_x0 = max(b[0], cb[0]); inter_y0 = max(b[1], cb[1])
                    inter_x1 = min(b[2], cb[2]); inter_y1 = min(b[3], cb[3])
                    inter = max(0, inter_x1 - inter_x0) * max(0, inter_y1 - inter_y0)
                    uni = _bbox_area(b) + _bbox_area(cb) - inter + 1e-6
                    iou = inter / uni
                    if iou > 0.3 or _bbox_dist(b, cb) < 10:
                        candidates[i] = _bbox_union(cb, b)
                        merged = True
                        break
                if not merged:
                    candidates.append(b)

            # 逐候选导出
            for idx, bbox in enumerate(candidates, start=1):
                try:
                    # 先裁剪到页面，避免越界报错
                    clipped = _clip_to_page(bbox, page)
                    if clipped is None:
                        continue

                    # 找 caption（四向）
                    caption = _find_caption_four_directions(page, words, clipped)

                    # 生成 origin_id 供 JSON；同时准备用于命名的标签
                    origin_id_raw = _extract_origin_id_from_caption(caption) if caption else ""
                    origin_id = (origin_id_raw or f"FIG_P{page_num}_{idx}").upper()

                    # 高分辨率栅格化
                    pil_img = page.within_bbox(clipped).to_image(resolution=180).original

                    # —— 尺寸过滤（不变）——
                    if pil_img.width < MIN_IMG_WIDTH or pil_img.height < MIN_IMG_HEIGHT:
                        print(f"[INFO] 跳过小图 {origin_id}: {pil_img.width}x{pil_img.height}")
                        continue

                    # —— 正文块过滤（新增）——
                    stats_hint = None
                    for gx0, gy0, gx1, gy1, st in graphic_boxes_stats:
                        if _bbox_dist(clipped, (gx0, gy0, gx1, gy1)) < 5:
                            stats_hint = st
                            break
                    txt_ratio, edge_score = _text_and_edge_score(words, clipped, pil_img)
                    if _looks_like_plain_text(txt_ratio, edge_score, stats_hint):
                        print(f"[INFO] 跳过正文块 {origin_id}: text_ratio={txt_ratio:.2f}, edge_score={edge_score:.2f}")
                        continue

                    # —— 去重过滤（不变：阈值/算法）——
                    cur_hash = HASH_FUNC(pil_img)
                    if any(abs(cur_hash - h) <= HASH_THRESHOLD for h in kept_hashes):
                        print(f"[INFO] 跳过重复图 {origin_id}: 已有近似 phash")
                        continue

                    # 文件命名：优先用标签名；格式 = pdf名_页码_图片标签
                    label_for_name = _normalize_label_for_filename(caption)
                    if label_for_name:
                        file_tail = _sanitize_for_filename(label_for_name)
                    else:
                        file_tail = _sanitize_for_filename(origin_id)  # 兜底
                    img_name = f"{stem}_p{page_num}_{file_tail}.png"
                    img_path = os.path.join(out_dir, img_name)

                    # 保存
                    pil_img.save(img_path, format="PNG")
                    kept_hashes.append(cur_hash)

                    # figure_id 保持与 caption/标签一致（用于 JSON）
                    figure_id_match = re.match(r'^(Fig(?:ure)?s?[\s\-]*\d+(?:\.\d+)*)', caption, re.I) if caption else None
                    figure_id = origin_id_raw or (figure_id_match.group(1) if figure_id_match else f"fig_p{page_num}_{idx}")

                    figures.append({
                        "figure_id": figure_id,
                        "origin_id": origin_id,
                        "page": page_num,
                        "caption": caption,
                        "image_path": img_path,
                        "width": pil_img.width,
                        "height": pil_img.height,
                        "phash": str(cur_hash)
                    })
                except Exception as e:
                    print(f"[WARN] 图片提取失败 {pdf_path} 第{page_num}页: {e}")
    return figures

# ========== 语境抓取 ==========
def find_contexts(figures, full_text):
    sentences = split_sentences(full_text)
    total = len(sentences)
    for fig in figures:
        fig_id = fig.get("origin_id", "")
        caption_text = fig.get("caption", "")
        ranges = []
        nums = re.findall(r'\d+(?:\.\d+)*', (fig_id + " " + caption_text))
        if nums:
            num = nums[0]
            patterns = [
                fr'(?:Figure|Fig\.?|图|图表)\s*{re.escape(num)}',
                fr'(?:Table|Tab\.?|表)\s*{re.escape(num)}'
            ]
            for i, s in enumerate(sentences):
                if any(re.search(p, s, re.I) for p in patterns):
                    start = max(0, i - 2)
                    end = min(total, i + 3)
                    if end - start < 5:
                        end = min(total, end + (5 - (end - start)))
                    ranges.append((start, end))
        merged = []
        for r in sorted(ranges):
            if not merged:
                merged.append(r)
            else:
                ps, pe = merged[-1]
                cs, ce = r
                if cs <= pe:
                    merged[-1] = (ps, max(pe, ce))
                else:
                    merged.append(r)
        fig["contexts"] = [" ".join(sentences[s:e]) for s, e in merged]
    return figures

# ========== 主流程 ==========
def process_pdf(pdf_path):
    print(f"处理: {pdf_path}")
    paper_id = Path(pdf_path).stem
    out_dir = os.path.join(OUTPUT_DIR, paper_id)
    os.makedirs(out_dir, exist_ok=True)

    _, full_text = extract_text(pdf_path)
    figures = extract_images_and_captions(pdf_path, out_dir)
    figures = find_contexts(figures, full_text)

    out_json = os.path.join(out_dir, f"{paper_id}.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"paper_id": paper_id, "figures": figures}, f, ensure_ascii=False, indent=2)

    print(f"✅ 结果保存到 {out_json}")


if __name__ == "__main__":
    for f in os.listdir(PDF_DIR):
        if f.lower().endswith('.pdf'):
            process_pdf(os.path.join(PDF_DIR, f))
