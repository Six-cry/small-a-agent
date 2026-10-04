"""Use Docling to probe image and formula extraction on representative PDFs.

This is an isolated experiment. It does not touch the RAG manifest, Chroma,
embeddings, or the production document loader.
"""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import re


THIS_FILE = Path(__file__).resolve()
APP_DIR = THIS_FILE.parents[2]
KNOWLEDGE_DIR = APP_DIR / "rag" / "data" / "knowledge"
OUTPUT_DIR = THIS_FILE.parent / "reports" / "docling_probe"

PROBES = (
    (
        "image-01",
        "image_01_routing_topology",
        "考虑排队时延的系统保护通信网络路由选择算法_刘川.pdf",
        4,
        ("0.232", "0.992"),
    ),
    (
        "image-02",
        "image_02_sdn_diagram",
        (
            "Service Performance Deviation Awareness-Based Power "
            "Communication Network Routing Optimization Strategy.pdf"
        ),
        5,
        ("虚拟队列积压", "历史决策经验"),
    ),
)

FORMULA_PROBE = (
    "formula-01",
    "formula_01_queueing_model_latex",
    "考虑排队时延的系统保护通信网络路由选择算法_刘川.pdf",
    (2, 3),
)

PICTURE_PROBES = (
    (
        "picture-01",
        "picture_01_glm4v_routing_topology",
        "考虑排队时延的系统保护通信网络路由选择算法_刘川.pdf",
        4,
        ("0.232", "0.992", "7", "11"),
    ),
    (
        "picture-02",
        "picture_02_glm4v_sdn_diagram",
        (
            "Service Performance Deviation Awareness-Based Power "
            "Communication Network Routing Optimization Strategy.pdf"
        ),
        5,
        ("虚拟队列积压", "历史决策经验"),
    ),
)

PICTURE_MODEL = "glm-4v-flash"
PICTURE_IMAGES_SCALE = 4.0
PICTURE_PROMPT = """请仔细分析这张技术图，不要猜测看不清的内容。
1. 原样抄录图中所有可见的中文、英文、数字和符号。
2. 描述分组、包含关系、节点、连线、箭头及箭头方向。
3. 如果是网络拓扑图，请列出相连的节点对及每条边的标注。
4. 如果是流程图或架构图，请列出每个框，并说明它属于哪个较大的框。
请用中文输出详细、可供知识库检索的描述。"""
TOPOLOGY_PROMPT = """这是一张包含12个节点的密集网络拓扑图。请逐条沿着线段核对，
不要根据数值出现的位置猜测端点，也不要补写看不清的边。

只回答这个问题：节点7和节点11是否直接相连；如果相连，连接它们的边标注是什么。
固定格式：目标边：节点7—节点11 = (第一个数值, 第二个数值)
如果无法看清，请输出“目标边：无法确认”。不要列出或推测任何其他边。"""

# These are deliberately structural checks rather than exact-string checks so
# harmless LaTeX spacing differences do not affect the result.
FORMULA_EXPECTATIONS = (
    ("公式（1）", (r"N_{ij}=", r"\lambda_{ij}", r"\mu_{ij}-\lambda_{ij}")),
    ("公式（2）", (r"N=", r"\sum_{i,j}", r"\lambda_{ij}")),
    ("公式（3）", (r"T=", r"\frac{1}{\gamma}", r"\sum_{i,j}")),
    (
        "公式（4）",
        (r"T_{\text{path}_{k}}=", r"\sum_{i=1}^{n-1}", r"\Delta t"),
    ),
    ("公式（5）", (r"P=", r"\prod_{i=1}^{n-1}", r"A_{e_{i}}A_{c_{i}}")),
    ("公式（6）", (r"\min T_{\text{path}_{k}}", r"P_{\text{path}_{k}}>P_{0}")),
)


def normalize_text(value: str) -> str:
    """Ignore whitespace inserted during PDF parsing."""
    return "".join(value.casefold().split())


def normalize_latex(value: str) -> str:
    """Ignore formatting whitespace emitted by the formula model."""
    return re.sub(r"\s+", "", value.replace(r"\ ", ""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="独立验证Docling图片与公式解析效果")
    parser.add_argument(
        "--only",
        choices=(
            "image-01",
            "image-02",
            "formula-01",
            "picture-01",
            "picture-02",
        ),
        help="只运行指定测试；不填写时运行原有两项图片测试",
    )
    parser.add_argument(
        "--reuse-output",
        action="store_true",
        help="公式或GLM图片测试复用已有报告，只重新执行准确性检查",
    )
    return parser.parse_args()


def check_picture_relationship(case_id: str, description_text: str) -> tuple[bool, str]:
    """Check the answer relationship, not only isolated expected terms."""
    if case_id == "picture-01":
        required = ("节点7", "节点11", "0.232", "0.992")
        for line in description_text.splitlines():
            normalized_line = normalize_text(line)
            if all(normalize_text(part) in normalized_line for part in required):
                return True, "同一条边记录包含节点7、节点11和(0.232, 0.992)"
        return False, "没有一条边记录同时对应节点7、节点11和(0.232, 0.992)"

    if case_id == "picture-02":
        normalized = normalize_text(description_text)
        start = normalized.find(normalize_text("SDN控制平面"))
        if start < 0:
            return False, "描述中没有找到SDN控制平面"
        # Limit the check to the nearby grouping statement so that two terms
        # appearing elsewhere in a long caption cannot create a false pass.
        grouping_window = normalized[start : start + 320]
        required = ("虚拟队列积压", "历史决策经验")
        has_members = all(normalize_text(part) in grouping_window for part in required)
        has_relation = any(
            normalize_text(word) in grouping_window
            for word in ("包含", "组成", "部分")
        )
        if has_members and has_relation:
            return True, "SDN控制平面的局部分组描述包含两个目标组成部分"
        return False, "两个目标词没有在SDN控制平面的局部分组关系中同时出现"

    return False, f"没有为{case_id}定义关系检查规则"


def build_picture_prompt(case_id: str, caption: str) -> str:
    """Build a task-specific prompt while preserving the source caption."""
    prompt = TOPOLOGY_PROMPT if case_id == "picture-01" else PICTURE_PROMPT
    if caption:
        prompt += f"\n这张图在原文中的图注是：{caption}"
    return prompt


def is_target_picture(case_id: str, caption: str) -> bool:
    """Avoid sending unrelated decorations and charts in the focused probe."""
    if case_id != "picture-01":
        return True
    normalized_caption = normalize_text(caption)
    return (
        normalize_text("12个节点") in normalized_caption
        and normalize_text("网络拓扑") in normalized_caption
    )


def run_formula_probe(
    document_converter_type: type,
    pdf_format_option_type: type,
    input_format_type: type,
    pdf_pipeline_options_type: type,
    reuse_output: bool = False,
) -> bool:
    """Convert formula pages with Docling's formula-to-LaTeX model enabled."""
    case_id, label, file_name, page_range = FORMULA_PROBE
    pdf_path = KNOWLEDGE_DIR / file_name
    if not pdf_path.is_file():
        print(f"[文件不存在] {pdf_path}")
        return False

    first_page, last_page = page_range
    output_path = OUTPUT_DIR / f"{label}.md"
    if reuse_output:
        if not output_path.is_file():
            print(f"[结果不存在] 无法复用：{output_path}")
            return False
        print(f"\n复用公式解析结果：{output_path}")
        markdown = output_path.read_text(encoding="utf-8")
    else:
        pipeline_options = pdf_pipeline_options_type()
        pipeline_options.do_formula_enrichment = True
        converter = document_converter_type(
            format_options={
                input_format_type.PDF: pdf_format_option_type(
                    pipeline_options=pipeline_options,
                )
            }
        )

        print(
            f"\n开始公式增强解析：{file_name}"
            f"（第 {first_page}-{last_page} 页）"
        )
        try:
            result = converter.convert(str(pdf_path), page_range=page_range)
            markdown = result.document.export_to_markdown()
        except Exception as exc:
            print(f"[解析失败] {type(exc).__name__}: {exc}")
            return False

        output_path.write_text(markdown, encoding="utf-8")

    latex_blocks = [
        block.strip()
        for block in re.findall(r"\$\$(.+?)\$\$", markdown, flags=re.DOTALL)
        if block.strip()
    ]
    undecoded_count = markdown.count("<!-- formula-not-decoded -->")

    print(f"解析结果：{output_path}")
    print(f"  [统计] 已解码 LaTeX 公式：{len(latex_blocks)} 个")
    print(f"  [统计] 未解码公式占位符：{undecoded_count} 个")
    for index, latex in enumerate(latex_blocks, start=1):
        one_line_latex = " ".join(latex.split())
        print(f"  [公式 {index}] {one_line_latex}")

    if not latex_blocks:
        print("[未通过] 没有得到任何 $$...$$ 格式的 LaTeX 公式")
        return False
    if undecoded_count:
        print("[部分通过] 已得到 LaTeX，但仍有公式未成功解码")
        return False

    accurate_count = 0
    print("  [逐项核对] 对照原 PDF 检查关键数学结构：")
    for index, (formula_name, expected_parts) in enumerate(
        FORMULA_EXPECTATIONS,
        start=1,
    ):
        if index > len(latex_blocks):
            print(f"    [不正确] {formula_name}：没有对应的 LaTeX 公式")
            continue
        normalized_latex = normalize_latex(latex_blocks[index - 1])
        missing_parts = [
            part
            for part in expected_parts
            if normalize_latex(part) not in normalized_latex
        ]
        if missing_parts:
            print(
                f"    [不正确] {formula_name}：缺少或误识别 "
                + "、".join(missing_parts)
            )
        else:
            accurate_count += 1
            print(f"    [正确] {formula_name}")

    print(
        f"[格式通过] {len(latex_blocks)} 个公式均已转换为 LaTeX，"
        "没有遗留占位符"
    )
    if accurate_count != len(FORMULA_EXPECTATIONS):
        print(
            f"[内容部分通过] 与原 PDF 的关键结构相比，"
            f"{accurate_count}/{len(FORMULA_EXPECTATIONS)} 个公式通过自动核对"
        )
        return False

    print("[内容通过] 所有公式的关键数学结构均与原 PDF 一致")
    return True


def run_picture_probe(
    probe: tuple[str, str, str, int, tuple[str, ...]],
    document_converter_type: type,
    pdf_format_option_type: type,
    input_format_type: type,
    pdf_pipeline_options_type: type,
    openai_client_type: type,
    reuse_output: bool = False,
) -> bool:
    """Extract pictures with Docling and describe them using GLM-4V-Flash."""
    case_id, label, file_name, page_number, expected_terms = probe
    pdf_path = KNOWLEDGE_DIR / file_name
    if not pdf_path.is_file():
        print(f"[文件不存在] {pdf_path}")
        return False

    output_path = OUTPUT_DIR / f"{label}.md"
    if reuse_output:
        if not output_path.is_file():
            print(f"[结果不存在] 无法复用：{output_path}")
            return False
        report = output_path.read_text(encoding="utf-8")
        description_text = report.split("## 关键词检查", maxsplit=1)[0]
        print(f"\n复用图片描述结果：{output_path}")
        all_found = all(
            normalize_text(term) in normalize_text(description_text)
            for term in expected_terms
        )
        relationship_ok, relationship_detail = check_picture_relationship(
            case_id,
            description_text,
        )
        print(f"  [关键词{'通过' if all_found else '未通过'}]")
        print(
            f"  [关系{'通过' if relationship_ok else '未通过'}] "
            f"{relationship_detail}"
        )
        return all_found and relationship_ok

    api_key = os.getenv("ZHIPUAI_API_KEY", "").strip()
    if not api_key:
        print(
            "[缺少配置] 没有找到 ZHIPUAI_API_KEY。"
            "请将智谱 API Key 写入 aa_my_agent/.env 后重试。"
        )
        return False

    pipeline_options = pdf_pipeline_options_type()
    pipeline_options.generate_picture_images = True
    pipeline_options.images_scale = PICTURE_IMAGES_SCALE
    converter = document_converter_type(
        format_options={
            input_format_type.PDF: pdf_format_option_type(
                pipeline_options=pipeline_options,
            )
        }
    )

    print(
        f"\n开始 GLM-4V-Flash 图片描述：{file_name}"
        f"（仅第 {page_number} 页）"
    )
    try:
        result = converter.convert(
            str(pdf_path),
            page_range=(page_number, page_number),
        )
    except Exception as exc:
        print(f"[解析失败] {type(exc).__name__}: {exc}")
        return False

    picture_dir = OUTPUT_DIR / "pictures" / label
    picture_dir.mkdir(parents=True, exist_ok=True)
    report_parts = [
        f"# {case_id}：GLM-4V-Flash 图片描述测试",
        "",
        f"- 来源文件：`{file_name}`",
        f"- PDF 页码：{page_number}",
        f"- 图片描述模型：智谱 `{PICTURE_MODEL}` API",
        "- 图片提取：Docling",
        f"- Docling 图片缩放倍率：{PICTURE_IMAGES_SCALE}",
        "- 说明：以下关键词检查只针对图片描述，不检查图片附近的正文。",
        "",
    ]
    descriptions: list[str] = []
    client = openai_client_type(
        api_key=api_key,
        base_url="https://open.bigmodel.cn/api/paas/v4",
    )

    for index, picture in enumerate(result.document.pictures, start=1):
        caption = picture.caption_text(result.document).strip()
        page_no = picture.prov[0].page_no if picture.prov else page_number
        image_name = f"picture_{index:02d}.png"
        image_path = picture_dir / image_name
        picture_image = picture.get_image(result.document)
        if picture_image is not None:
            picture_image.save(image_path, format="PNG")

        description = ""
        api_error = ""
        skipped_reason = ""
        if not is_target_picture(case_id, caption):
            skipped_reason = "聚焦探针跳过非目标图片，未发送给视觉模型"
        elif picture_image is not None:
            image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
            prompt = build_picture_prompt(case_id, caption)
            try:
                response = client.chat.completions.create(
                    model=PICTURE_MODEL,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": (
                                            "data:image/png;base64,"
                                            + image_base64
                                        )
                                    },
                                },
                            ],
                        }
                    ],
                    temperature=0.0,
                    max_tokens=1000,
                )
                description = (response.choices[0].message.content or "").strip()
            except Exception as exc:
                api_error = f"{type(exc).__name__}: {exc}"
        descriptions.append(description)

        report_parts.extend(
            [
                f"## 图片 {index}",
                "",
                f"- Docling 页码：{page_no}",
                f"- 图注：{caption or '无'}",
                (
                    f"- 裁剪图：`pictures/{label}/{image_name}`"
                    if picture_image is not None
                    else "- 裁剪图：未生成"
                ),
                "",
                description or "（GLM-4V-Flash 没有生成图片描述）",
                *(["", f"- 跳过原因：{skipped_reason}"] if skipped_reason else []),
                *(["", f"- API 错误：`{api_error}`"] if api_error else []),
                "",
            ]
        )

    description_text = "\n".join(descriptions)
    report_parts.extend(
        [
            "## 关键词检查",
            "",
        ]
    )
    all_found = True
    for term in expected_terms:
        found = normalize_text(term) in normalize_text(description_text)
        status = "找到" if found else "未找到"
        report_parts.append(f"- [{status}] `{term}`")
        all_found = all_found and found

    relationship_ok, relationship_detail = check_picture_relationship(
        case_id,
        description_text,
    )
    report_parts.extend(
        [
            "",
            "## 关系检查",
            "",
            f"- [{'通过' if relationship_ok else '未通过'}] {relationship_detail}",
        ]
    )

    report_parts.extend(
        [
            "",
            "## 完整 Docling Markdown",
            "",
            result.document.export_to_markdown(),
            "",
        ]
    )
    output_path.write_text("\n".join(report_parts), encoding="utf-8")

    print(f"解析结果：{output_path}")
    print(f"  [统计] 检测到图片：{len(result.document.pictures)} 张")
    for term in expected_terms:
        found = normalize_text(term) in normalize_text(description_text)
        status = "找到" if found else "未找到"
        print(f"  [{status}] {term}")
    print(
        f"  [关系{'通过' if relationship_ok else '未通过'}] "
        f"{relationship_detail}"
    )

    if not result.document.pictures:
        print("[未通过] Docling 没有检测到任何图片区域")
        return False
    if all_found and relationship_ok:
        print("[语义通过] 图片描述包含预期关键词及其正确关系")
        return True

    if all_found:
        print("[语义未通过] 关键词齐全，但它们之间的对应关系不正确")
        return False

    print("[关键词未通过] GLM-4V-Flash 图片描述没有包含全部预期关键词")
    return False


def main() -> int:
    try:
        from dotenv import load_dotenv
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter
        from docling.document_converter import PdfFormatOption
        from openai import OpenAI
    except ModuleNotFoundError:
        print("缺少 Docling、python-dotenv 或 openai，请先安装项目依赖。")
        return 2

    # Match aa_my_agent/config.py: the Agent owns its own .env file.
    load_dotenv(APP_DIR / ".env", override=True)
    args = parse_args()
    reusable_cases = {FORMULA_PROBE[0], *(probe[0] for probe in PICTURE_PROBES)}
    if args.reuse_output and args.only not in reusable_cases:
        print("--reuse-output 只能用于 formula-01、picture-01 或 picture-02")
        return 2
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conversion_failed = False

    if args.only == FORMULA_PROBE[0]:
        formula_ok = run_formula_probe(
            DocumentConverter,
            PdfFormatOption,
            InputFormat,
            PdfPipelineOptions,
            reuse_output=args.reuse_output,
        )
        return 0 if formula_ok else 1

    picture_probe = next(
        (probe for probe in PICTURE_PROBES if probe[0] == args.only),
        None,
    )
    if picture_probe is not None:
        picture_ok = run_picture_probe(
            picture_probe,
            DocumentConverter,
            PdfFormatOption,
            InputFormat,
            PdfPipelineOptions,
            OpenAI,
            reuse_output=args.reuse_output,
        )
        return 0 if picture_ok else 1

    converter = DocumentConverter()

    for case_id, label, file_name, page_number, expected_terms in PROBES:
        if args.only == FORMULA_PROBE[0]:
            continue
        if args.only is not None and case_id != args.only:
            continue

        pdf_path = KNOWLEDGE_DIR / file_name
        if not pdf_path.is_file():
            print(f"[文件不存在] {pdf_path}")
            conversion_failed = True
            continue

        print(f"\n开始解析：{file_name}（仅第 {page_number} 页）")

        try:
            result = converter.convert(
                str(pdf_path),
                page_range=(page_number, page_number),
            )
            markdown = result.document.export_to_markdown()
        except Exception as exc:
            print(f"[解析失败] {type(exc).__name__}: {exc}")
            conversion_failed = True
            continue

        output_path = OUTPUT_DIR / f"{label}.md"
        output_path.write_text(markdown, encoding="utf-8")
        normalized_markdown = normalize_text(markdown)

        print(f"解析结果：{output_path}")
        for term in expected_terms:
            found = normalize_text(term) in normalized_markdown
            status = "找到" if found else "未找到"
            print(f"  [{status}] {term}")

    if args.only != FORMULA_PROBE[0]:
        print(
            "\n说明：未找到目标词不代表程序报错，"
            "而是说明Docling默认解析仍没有读出对应图片信息。"
        )
    return 1 if conversion_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
