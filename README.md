# 🧠 PDF-to-Quiz Generator

> **从学术 PDF 自动生成基于图像的选择题数据集**  
> _Extract figures, context, and auto-generate questions with AI_

---

## 🌟 项目简介

本项目实现了一个**从 PDF 文件中自动提取示例图表并生成对应选择题**的完整流水线。  
从提取图片、生成上下文、AI 出题到汇总成 Excel，全过程自动化，可用于教学、题库构建或模型训练。

---

## 🧩 项目结构

```bash
project_root/
│
├── pdf/                     # 📥 放置源 PDF 文件
├── raw_img/                 # 🖼️ 第一步提取的原始图片（按 PDF 分类）
├── img_set/                 # 📚 第二步生成的上下文图片集（每图一个文件夹）
├── pic2jsonl_output/        # 🧾 第三步 AI 输出的 JSONL 题目数据
├── final_excel_output/      # 📊 第四步合并输出的 Excel 文件
│
├── extract_pdf2img.py       # Step 1: 提取 PDF 图片与 caption
├── figTo4bgImg.py           # Step 2: 生成每图的上下文页合集
├── autointerface_build_jsonl.py  # Step 3: 调用 API 生成题目 JSONL
└── jsonl_to_xlsx.py         # Step 4: 汇总所有 JSONL 为 Excel
```

---

## 🚀 使用流程

### 🥇 Step 1: 提取 PDF 图片

```bash
python extract_pdf2img.py
```

从 `pdf/` 文件夹提取图像到 `raw_img/`，自动识别图号（Fig/Table）和标题，并过滤小图或重复图。

**主要参数** | **作用** | **默认值**
---|---|---
`MIN_IMG_WIDTH` / `MIN_IMG_HEIGHT` | 最小图片尺寸过滤 | 300 / 200
`HASH_THRESHOLD` | 去重相似度阈值（越小越严格） | 5
`HASH_FUNC` | 哈希算法（可选 `ahash` / `dhash` / `phash` 等） | `imagehash.phash`

📁 输出：`raw_img/{pdf_name}/` 下保存各页图像与描述。

---

### 🥈 Step 2: 生成四页上下文图集

```bash
python figTo4bgImg.py
```

读取 `raw_img/`，根据图片文件名中的页码，从 `pdf/` 中渲染上下文页（N-1、N、N+1），生成四图合集。

**主要参数** | **作用** | **默认值**
---|---|---
`RENDER_DPI` | 渲染清晰度（数值越高越清晰） | 220
`SKIP_IF_EXISTS` | 是否跳过已存在文件 | True
`PDF_DIR`, `RAW_IMG_DIR`, `OUTPUT_BASE_DIR` | 自定义路径 | 见脚本定义

📁 输出：`img_set/{图像文件名}/` 中包含 4 张图片（原图 + 上下文页）。

---

### 🥉 Step 3: AI 出题生成 JSONL

```bash
python autointerface_build_jsonl.py
```

将每个 `img_set/` 文件夹下的四张图发送到 AI 接口，由 GPT-4o 模型生成包含题目、选项、答案、解析的 JSON。

**主要参数** | **说明** | **默认值**
---|---|---
`API_BASE` | API 地址 | `https://api.ai-gaochao.cn/v1`
`API_MODEL` | 模型名称 | `gpt-4o`
`API_KEY` | API 密钥（推荐设为环境变量 `AIGC_API_KEY`） | 自行设置
`EXPECTED_PNG_COUNT` | 每组期望图片数 | 4
`RETRY_TOTAL` / `RETRY_BACKOFF` | 请求重试策略 | 3 / 1.5
`TIMEOUT` | 连接与读取超时 | (10, 120)

📁 输出：`pic2jsonl_output/dataset.jsonl`  
🪵 日志：`build_jsonl_logs/run.log`

---

### 🏁 Step 4: 合并 JSONL 为 Excel

```bash
python jsonl_to_xlsx.py
```

将所有 `pic2jsonl_output/*.jsonl` 合并为一个 Excel 文件：

**命令参数** | **作用** | **默认值**
---|---|---
`-d / --dir` | 输入 JSONL 目录 | `./pic2jsonl_output`
`-o / --output` | 输出 Excel 文件路径 | `./final_excel_output/finalOutput.xlsx`
`--sep` | 展开 JSON 的字段分隔符 | `.`
`--sheet-name` | Excel 工作表名称 | `Sheet1`

📁 输出：`final_excel_output/finalOutput.xlsx`

---

## ⚙️ 环境依赖

```bash
pip install pdfplumber PyPDF2 pillow imagehash pymupdf requests openpyxl pandas
```

可选依赖（用于更美观的日志显示）：
```bash
pip install tqdm
```

---

## 📊 最终输出示例

| pdf | page | originID | question | options | answer | analysis | pic |
|------|------|-----------|-----------|----------|----------|-----------|------|
| sample.pdf | 23 | Fig 1.2 | Which region corresponds to... | [A. ..., B. ..., C. ..., D. ...] | B | 因为该曲线处于... | sample_p23_FIG1.2.png |

Excel 文件路径：`final_excel_output/finalOutput.xlsx`

---

## 🧰 调试与技巧

- ✅ 确认每步输出目录存在再执行下一步；
- 🔑 请务必务必务必填写并检查第三个py代码中 `API_KEY` 是否有效，否则会返回 401；
- 🚀 可跳过某步直接使用中间结果目录；
- 🧱 支持批量 PDF 自动处理，结果自动汇总；
- 🧪 若想生成更清晰的上下文图，可适当提高 `RENDER_DPI`。

---

## 📜 License

本项目遵循 **MIT License**。

---

## ❤️ 致谢

感谢以下开源项目：
- [PyMuPDF](https://pymupdf.readthedocs.io/)
- [pdfplumber](https://github.com/jsvine/pdfplumber)
- [imagehash](https://pypi.org/project/ImageHash/)
- [pandas](https://pandas.pydata.org/)

> Made with ❤️ by MY

