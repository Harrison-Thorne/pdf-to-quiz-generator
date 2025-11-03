import os
import re
import json
from pathlib import Path

import pdfplumber
from PyPDF2 import PdfReader
from PIL import Image  # pillow
import imagehash       # perceptual hash,  pip install imagehash

PDF_DIR = r"pdf"
OUTPUT_DIR = r"raw_img"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ========== 可配置参数 ==========
# 1) 尺寸过滤：像素过小则舍弃
MIN_IMG_WIDTH = 200   # px
MIN_IMG_HEIGHT = 100  # px

# 2) 图片去重：感知哈希距离阈值（0~64，越小越相似）。
#    当当前图片与已保留的上一张图片 phash 距离 <= HASH_THRESHOLD 时，视为近似重复并舍弃。
HASH_THRESHOLD = 5
HASH_FUNC = imagehash.phash          # 可替换为 ahash / dhash / whash 等


# ========== 工具函数 ==========

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
    pattern = r'(?<=[.?!])\s+(?=(?:[A-Z]|[^0-9]))'
    sentences = re.split(pattern, text)
    return [s.strip() for s in sentences if s.strip()]


def _extract_origin_id_from_caption(caption: str) -> str:
    m = re.match(r'^\s*(?:Fig(?:\.|ure)?|Table)\s*([0-9]+(?:[.\-][0-9A-Za-z]+)*)', caption, re.I)
    if m:
        prefix = re.match(r'^\s*(Fig(?:\.|ure)?|Table)', caption, re.I).group(1)
        number = m.group(1)
        return f"{prefix} {number}"
    return ""


def _sanitize_for_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', '_', name)


def extract_images_and_captions(pdf_path, out_dir):
    figures = []
    stem = Path(pdf_path).stem
    last_kept_hash = None  # 上一张已保留图片的 phash

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(x_tolerance=1, y_tolerance=2)
            for img_index, img in enumerate(page.images, start=1):
                bbox = (img["x0"], img["top"], img["x1"], img["bottom"])
                try:
                    # ---------- 提取 caption ----------
                    caption_words, found_start, last_top = [], False, None
                    for w in words:
                        if w["top"] > img["bottom"]:
                            if not found_start and re.match(r'^(Fig|Figure|Table)', w["text"], re.I):
                                found_start = True
                            if found_start:
                                if last_top is not None and w["top"] - last_top > 20:
                                    break
                                caption_words.append(w["text"])
                                last_top = w["top"]
                    caption = re.sub(r'\s+', ' ', " ".join(caption_words)).strip()
                    if not caption:
                        continue

                    # ---------- origin_id & 文件名 ----------
                    origin_id_raw = _extract_origin_id_from_caption(caption)
                    origin_id = (origin_id_raw or f"FIG_P{page_num}_{img_index}").upper()
                    safe_origin = _sanitize_for_filename(origin_id)
                    img_name = f"{stem}_p{page_num}_{safe_origin}.png"
                    img_path = os.path.join(out_dir, img_name)

                    # ---------- 导出图片 ----------
                    pil_img = page.within_bbox(bbox).to_image(resolution=150).original

                    # —— 尺寸过滤 ——
                    if pil_img.width < MIN_IMG_WIDTH or pil_img.height < MIN_IMG_HEIGHT:
                        print(f"[INFO] 跳过小图 {origin_id}: {pil_img.width}x{pil_img.height}")
                        continue

                    # —— 去重过滤 ——
                    cur_hash = HASH_FUNC(pil_img)
                    if last_kept_hash is not None and abs(cur_hash - last_kept_hash) <= HASH_THRESHOLD:
                        print(f"[INFO] 跳过重复图 {origin_id}: phash dist={abs(cur_hash - last_kept_hash)}")
                        continue

                    # 通过过滤，保存图片
                    pil_img.save(img_path, format="PNG")
                    last_kept_hash = cur_hash  # 更新上一张已保留图片的 hash

                    figure_id_match = re.match(r'^(Fig(?:ure)?\s*\d+(?:\.\d+)*)', caption, re.I)
                    figure_id = origin_id_raw or (figure_id_match.group(1) if figure_id_match else f"fig_p{page_num}_{img_index}")

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


def find_contexts(figures, full_text):
    sentences = split_sentences(full_text)
    total = len(sentences)
    for fig in figures:
        fig_id = fig.get("origin_id", "")
        caption_text = fig.get("caption", "")
        ranges = []
        nums = re.findall(r'\d+(?:\.\d+)*', fig_id)
        if nums:
            num = nums[0]
            patterns = [fr'Figure {num}', fr'Fig\.?\s*{num}', fr'Table {num}']
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
