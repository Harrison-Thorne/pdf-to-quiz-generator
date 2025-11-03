# -*- coding: utf-8 -*-
"""
Build JSONL by scanning 4 PNGs per folder, sending to gpt-4o, and parsing result.

Author: you
"""
import os
import re
import json
import base64
import time
import logging
from typing import Dict, List, Tuple, Optional
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter, Retry

# ---------------------
# 配置区（按需修改）
# ---------------------
SCRIPT_DIR = Path(__file__).parent  # 脚本所在目录
PROJECT_ROOT = SCRIPT_DIR            # 项目根目录设为脚本所在目录
IMG_SET_DIR = os.path.join(PROJECT_ROOT, "img_set")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "pic2jsonl_output")
LOG_DIR = os.path.join(PROJECT_ROOT, "build_jsonl_logs")
OUTPUT_JSONL = os.path.join(OUTPUT_DIR, "dataset.jsonl")

API_BASE = "https://api.ai-gaochao.cn/v1"
API_MODEL = "gpt-4o"


# 注意注意注意注意注意注意注意注意注意注意注意注意注意注意注意这里填写你的KEY
API_KEY = os.environ.get("AIGC_API_KEY", "请务必在这填写您的KEY！！！！！！！！！！！！！！！！！！")


# 每个主文件夹期望 PNG 数量
EXPECTED_PNG_COUNT = 4
# 请求重试策略
RETRY_TOTAL = 3
RETRY_BACKOFF = 1.5
TIMEOUT = (10, 120)  # (连接超时, 读超时)

# ---------------------
# 日志
# ---------------------
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, "run.log"),
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
logging.getLogger("").addHandler(console)


# ---------------------
# 工具函数
# ---------------------
def parse_folder_name(folder_name: str) -> Optional[Dict[str, str]]:
    """
    解析主文件夹名，示例：
    digital-integrated-circuits-analysis-and-design_compress_p23_FIGURE 1.2
    -> pdf: digital-integrated-circuits-analysis-and-design_compress.pdf
       page: 23
       originID: FIGURE 1.2
       pic: folder_name (原样)
    """
    # 匹配：前缀 + _p数字 + _ + 其余作为 originID
    m = re.match(r"^(?P<pdfbase>.+?)_p(?P<page>\d+)_+(?P<originID>.+)$", folder_name)
    if not m:
        logging.error(f"无法解析主文件夹名：{folder_name}")
        return None

    pdfbase = m.group("pdfbase")
    page = m.group("page")
    originID = m.group("originID")
    pdf = f"{pdfbase}.pdf"

    return {
        "pdf": pdf,
        "page": page,
        "originID": originID,
        "pic": folder_name,
    }


def list_pngs(folder_path: str) -> List[str]:
    """列出文件夹内 PNG（不区分大小写），按文件名排序。"""
    if not os.path.isdir(folder_path):
        return []
    files = [f for f in os.listdir(folder_path) if f.lower().endswith(".png")]
    files.sort()
    return [os.path.join(folder_path, f) for f in files]


def encode_image_b64(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_session() -> requests.Session:
    sess = requests.Session()
    retries = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST", "GET"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def call_gpt_with_images(
    session: requests.Session,
    b64_images: List[str],
    context_fields: Dict[str, str],
) -> Dict[str, str]:
    """
    把 4 张 base64 图片作为 prompt 背景信息，调用 gpt-4o。
    通过严格约束回复为 JSON（question/options/answer/analysis）。
    """
    # 组装 messages，user.content 支持 text + 多个 image_url（base64）
    image_parts = []
    for b64 in b64_images:
        image_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )

    sys_prompt = (
        "You are an expert skilled in generating high-quality multiple-choice questions based on textbook/paper page images. The system will provide 4 segmented images of the same page, arranged in reading order. Strictly rely on the visible information in these images (graphs, labels, text fragments), avoiding the use of external knowledge beyond the images.\n\n"
        "Please complete: Generate 1 medium-to-high difficulty multiple-choice question and return **strict JSON** containing only the following keys:"
        "question, options, answer, analysis.\n"
        "Field requirements:\n"
        "1) question: The stem must be rigorously phrased and independently understandable, referencing elements in the images (such as curves/coordinates/labels) when necessary, avoiding vague references;\n"
        "2) options: **Array**, provide 4 strings in the order of A, B, C, D; **每个字符串必须以选项字母+英文句点+空格开始**，示例格式：\n"
        "   \"A. The region labeled 'linear region'\",\n"
        "   \"B. The region labeled 'quasi quadratic region'\",\n"
        "   \"C. The region labeled 'saturation effects'\",\n"
        "   \"D. The region beyond the 'saturation effects'\";\n"
        "   distractors should be plausible but incorrect, and closely related to the stem information;\n"
        "- options must be a **string array of length 4**，且每个元素都符合“A. …”格式；\n"
        "3) answer: **Only 1 uppercase letter** (A/B/C/D), do not include option content or symbols;\n"
        "4) analysis: Provide concise reasoning explaining why the correct option is right and why the distractors are wrong, strictly based on the information within the images;\n\n"
        "Output specifications:\n"
        "- Output only one JSON object, prohibit any additional text, explanations, prefixes, suffixes, or code fences;\n"
        "- options must be a string array of length 4;\n"
        "- Maintain objectivity and neutrality, avoid going beyond the scope and inferring knowledge outside the images;\n"
        "- If formulas are involved, use raw LaTeX notation and perform JSON legal escaping (e.g., \\\\(x+y\\\\) format).\n"
    )

    # 将抽取的上下文字段也附带进来，让模型更聚焦
    ctx_text = (
        f"[Context]\n"
        f"pdf={context_fields.get('pdf','')}\n"
        f"page={context_fields.get('page','')}\n"
        f"originID={context_fields.get('originID','')}\n"
        f"pic={context_fields.get('pic','')}\n"
        f"Please create questions based on the content of the image, avoiding topics beyond the scope or external knowledge not depicted in the image."
    )

    payload = {
        "model": API_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {
                "role": "user",
                "content": [{"type": "text", "text": ctx_text}] + image_parts,
            },
        ],
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    url = f"{API_BASE}/chat/completions"
    resp = session.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    # 解析返回文本（OpenAI 兼容接口通常在 choices[0].message.content）
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )

    # 尝试解析 JSON
    try:
        parsed = json.loads(content)
        if not all(k in parsed for k in ("question", "options", "answer", "analysis")):
            raise ValueError("缺少必要字段")
        # 规范化 options
        if not isinstance(parsed.get("options"), list) or len(parsed["options"]) != 4:
            raise ValueError("options 必须是 4 个字符串的数组")
        return {
            "question": str(parsed["question"]).strip(),
            "options": json.dumps(parsed["options"], ensure_ascii=False),
            "answer": str(parsed["answer"]).strip(),
            "analysis": str(parsed["analysis"]).strip(),
        }
    except Exception as e:
        logging.warning(f"JSON 解析失败，尝试兜底处理：{e}")
        # 兜底：如果不是严格 JSON，就把全文放到 analysis，其它留空
        return {
            "question": "",
            "options": "",
            "answer": "",
            "analysis": content,  # 保留原文，方便后续人工清洗
        }


def build_record(
        meta: Dict[str, str],
        model_fields: Dict[str, str],
) -> Dict[str, str]:
    """构造最终 JSONL 记录。"""

    # ----------------------------------------------------
    # *** 兼容性修改区域 ***
    # 设定你需要的图片后缀，如果需要 .jpg 就改成 ".jpg"
    IMAGE_SUFFIX = ".png"  # 默认使用 .png
    # ----------------------------------------------------

    pic_with_suffix = f"{meta['pic']}{IMAGE_SUFFIX}"

    return {
        "pdf": meta["pdf"],
        "page": meta["page"],
        "originID": meta["originID"],
        "partedID": "",
        "question": model_fields.get("question", ""),
        "options": model_fields.get("options", ""),
        "answer": model_fields.get("answer", ""),
        "analysis": model_fields.get("analysis", ""),
        "pic": pic_with_suffix,  # 使用带后缀的新变量
    }


def process_one_folder(
    session: requests.Session, folder_path: str, folder_name: str
) -> Optional[Dict[str, str]]:
    """处理单个主文件夹：读取 4 PNG -> 调 API -> 产出一条记录。"""
    try:
        meta = parse_folder_name(folder_name)
        if not meta:
            return None

        pngs = list_pngs(folder_path)
        if not pngs:
            logging.error(f"未找到 PNG：{folder_path}")
            return None

        if len(pngs) != EXPECTED_PNG_COUNT:
            logging.warning(
                f"PNG 数量异常（期望 {EXPECTED_PNG_COUNT}，实际 {len(pngs)}）：{folder_path}"
            )

        # 只取前 4 张，避免超量
        pngs = pngs[:EXPECTED_PNG_COUNT]
        b64_list = [encode_image_b64(p) for p in pngs]

        model_fields = call_gpt_with_images(session, b64_list, meta)
        record = build_record(meta, model_fields)
        return record

    except requests.HTTPError as http_err:
        logging.error(f"HTTP 错误：{http_err} | folder={folder_name}")
    except requests.RequestException as req_err:
        logging.error(f"请求异常：{req_err} | folder={folder_name}")
    except Exception as e:
        logging.exception(f"处理文件夹失败：{folder_name} | {e}")
    return None


def main():
    logging.info("开始处理...")
    if not os.path.isdir(IMG_SET_DIR):
        logging.error(f"图集目录不存在：{IMG_SET_DIR}")
        return

    session = build_session()

    # 简单 API 健康检查（可选）
    try:
        r = session.get(f"{API_BASE}/models", headers={"Authorization": f"Bearer {API_KEY}"}, timeout=TIMEOUT)
        if r.status_code >= 400:
            logging.warning(f"模型列表检查失败：HTTP {r.status_code}（可忽略）")
    except Exception as e:
        logging.warning(f"模型列表检查异常（可忽略）：{e}")

    # 遍历第一层主文件夹
    folders = [
        name for name in os.listdir(IMG_SET_DIR)
        if os.path.isdir(os.path.join(IMG_SET_DIR, name))
    ]
    folders.sort()

    total = len(folders)
    ok = 0
    skipped = 0

    with open(OUTPUT_JSONL, "w", encoding="utf-8") as fout:
        for idx, folder_name in enumerate(folders, start=1):
            folder_path = os.path.join(IMG_SET_DIR, folder_name)
            logging.info(f"[{idx}/{total}] 处理：{folder_name}")

            record = process_one_folder(session, folder_path, folder_name)
            if record is None:
                skipped += 1
                continue

            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            ok += 1
            # 轻微节流，避免 QPS 过高（按需调整/去掉）
            time.sleep(0.4)

    logging.info(f"处理完成：成功 {ok} / 共 {total}，跳过 {skipped}。")
    logging.info(f"输出文件：{OUTPUT_JSONL}")


if __name__ == "__main__":
    main()
