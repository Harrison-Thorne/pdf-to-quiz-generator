# -*- coding: utf-8 -*-
"""
批量处理 raw_img 下的 PNG：
- 从文件名解析 {pdf名_无扩展名} 与 {页码N}
- 定位 pdf/{pdf名}.pdf
- 提取并渲染 N-1、N、N+1 三页为图片（越界自动裁剪）
- 在项目根目录下创建以「图片完整文件名（不含扩展名）」命名的文件夹
- 复制原 PNG 与三张上下文页图片到该文件夹（共4张）
"""

import os
import re
import sys
import shutil
from pathlib import Path

# ========== 可按需修改的路径与参数 ==========
SCRIPT_DIR = Path(__file__).parent  # 脚本所在目录
PROJECT_ROOT = SCRIPT_DIR            # 项目根目录设为脚本所在目录
PDF_DIR = PROJECT_ROOT / "pdf"                             # PDF 文件目录
RAW_IMG_DIR = PROJECT_ROOT / "raw_img"                    # 已按 PDF 分类的原始图片父目录
OUTPUT_BASE_DIR = PROJECT_ROOT / "img_set"                  # 输出文件的基础目录

# 渲染清晰度（DPI）。200~300 一般较清晰，数值越大图片越大
RENDER_DPI = 220

# 碰到已存在的输出文件时是否跳过渲染（True=跳过，False=覆盖）
SKIP_IF_EXISTS = True
# ========================================

# 惰性导入 PyMuPDF，方便给出更友好的提示
try:
    import fitz  # PyMuPDF
except ImportError as e:
    print("缺少依赖：PyMuPDF（pymupdf）。请先安装：\n  pip install pymupdf")
    sys.exit(1)


FILENAME_RE = re.compile(r"""
    ^(?P<pdf>.+?)          # 最短匹配 PDF 基名
    _p(?P<page>\d+)        # _p{页码}
    _.*                    # _{图片originID...}
    \.png$                 # 扩展名
""", re.IGNORECASE | re.VERBOSE)


def parse_image_filename(fname: str):
    """
    从文件名解析出 (pdf_stem, page_num)
    文件名格式：{pdf名_无扩展名}_p{页码}_{图片originID}.png
    """
    m = FILENAME_RE.match(fname)
    if not m:
        return None, None
    pdf_stem = m.group("pdf").strip()
    try:
        page_num = int(m.group("page"))
    except ValueError:
        page_num = None
    return pdf_stem, page_num


def find_pdf_by_stem(pdf_dir: Path, pdf_stem: str) -> Path:
    """
    在 pdf_dir 下寻找名为 {pdf_stem}.pdf 的文件（Windows 对大小写不敏感，这里仍按精确名）
    若不存在，尝试不区分大小写匹配。
    """
    exact = pdf_dir / f"{pdf_stem}.pdf"
    if exact.exists():
        return exact

    # 尝试大小写无关匹配
    cand = None
    target_lower = f"{pdf_stem}.pdf".lower()
    for p in pdf_dir.glob("*.pdf"):
        if p.name.lower() == target_lower:
            cand = p
            break
    return cand if cand and cand.exists() else None


def clamp(n, low, high):
    return max(low, min(n, high))


def render_pdf_pages_to_images(pdf_path: Path, page_numbers_1based, out_dir: Path, dpi=220, skip_if_exists=True):
    """
    将 PDF 的 1-based 页码列表渲染为 PNG 图片到 out_dir。
    命名：context_p{页码}.png
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    try:
        total = doc.page_count  # 1-based 的最大页是 total
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)

        for n in page_numbers_1based:
            if n < 1 or n > total:
                continue
            page = doc.load_page(n - 1)  # PyMuPDF 0-based
            pix = page.get_pixmap(matrix=mat, alpha=False)
            out_name = out_dir / f"context_p{n}.png"
            if out_name.exists() and skip_if_exists:
                # 跳过
                continue
            pix.save(out_name.as_posix())
    finally:
        doc.close()


def process_single_image(img_path: Path):
    """
    处理单张 PNG：
    - 解析文件名拿到 pdf_stem 与 N
    - 确定 PDF 路径
    - 计算需要渲染的三页（带边界处理）
    - 在项目根目录下创建输出目录（图片完整文件名不带扩展名）
    - 复制原 PNG & 渲染三页 PNG
    """
    fname = img_path.name
    pdf_stem, page_num = parse_image_filename(fname)
    if not pdf_stem or not page_num:
        print(f"[跳过] 文件名不符合约定：{fname}")
        return

    pdf_path = find_pdf_by_stem(PDF_DIR, pdf_stem)
    if not pdf_path:
        print(f"[缺失PDF] 找不到对应 PDF：{PDF_DIR / (pdf_stem + '.pdf')}")
        return

    # 读取总页数以进行边界裁剪
    try:
        with fitz.open(pdf_path) as doc:
            total = doc.page_count
    except Exception as e:
        print(f"[错误] 无法打开 PDF：{pdf_path}，原因：{e}")
        return

    n_prev = clamp(page_num - 1, 1, total)
    n_curr = clamp(page_num, 1, total)
    n_next = clamp(page_num + 1, 1, total)

    # 输出目录：项目根目录 / 图片完整文件名（无扩展名）
    out_dir = OUTPUT_BASE_DIR / img_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # 复制原 PNG
    dst_png = out_dir / fname
    try:
        if not (dst_png.exists() and SKIP_IF_EXISTS):
            shutil.copy2(img_path, dst_png)
    except Exception as e:
        print(f"[错误] 复制原图失败：{img_path} -> {dst_png}，原因：{e}")

    # 渲染并保存三张上下文页
    pages_to_render = []
    # 用有序添加，避免 N-1 与 N 或 N 与 N+1 重复（当 N=1 或 N=total 时）
    for p in (n_prev, n_curr, n_next):
        if p not in pages_to_render:
            pages_to_render.append(p)

    try:
        render_pdf_pages_to_images(
            pdf_path=pdf_path,
            page_numbers_1based=pages_to_render,
            out_dir=out_dir,
            dpi=RENDER_DPI,
            skip_if_exists=SKIP_IF_EXISTS
        )
        print(f"[完成] {fname} -> {out_dir}（共 {1 + len(pages_to_render)} 张）")
    except Exception as e:
        print(f"[错误] 渲染失败：{pdf_path} @ {pages_to_render}，原因：{e}")


def main():
    if not RAW_IMG_DIR.exists():
        print(f"[错误] 输入图片目录不存在：{RAW_IMG_DIR}")
        sys.exit(1)
    if not PDF_DIR.exists():
        print(f"[错误] PDF 目录不存在：{PDF_DIR}")
        sys.exit(1)

    # 遍历 raw_img 下所有子文件夹里的 PNG
    # 目录结构约定：raw_img/{pdf名_无扩展名}/*.png
    total_count = 0
    for sub in RAW_IMG_DIR.iterdir():
        if sub.is_dir():
            for img_path in sub.glob("*.png"):
                total_count += 1
                process_single_image(img_path)

    if total_count == 0:
        print(f"[提示] 未在 {RAW_IMG_DIR} 下找到任何 PNG。")
    else:
        print(f"[总结] 已处理图片数量：{total_count}")


if __name__ == "__main__":
    main()
