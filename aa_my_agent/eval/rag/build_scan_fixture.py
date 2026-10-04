"""生成没有文字层的扫描型 PDF 回归夹具，并检查其页面视觉内容。

该文件只用于验证 OCR 技术链路，不代表真实扫描件质量。正式启用 scan-01
前仍需加入一份真实扫描 PDF，并根据原件重新填写题目和答案。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "scan_01"
IMAGE_PATH = FIXTURE_DIR / "scan_01_source.png"
PDF_PATH = FIXTURE_DIR / "scan_01_image_only.pdf"
FONT_PATH = Path("C:/Windows/Fonts/msyh.ttc")


def build_fixture() -> Path:
    if not FONT_PATH.is_file():
        raise FileNotFoundError(f"缺少中文字体：{FONT_PATH}")
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1654, 2339), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(str(FONT_PATH), 58)
    body_font = ImageFont.truetype(str(FONT_PATH), 40)
    small_font = ImageFont.truetype(str(FONT_PATH), 30)

    draw.text((150, 150), "扫描件 OCR 回归样本", fill="black", font=title_font)
    draw.line((150, 245, 1504, 245), fill="black", width=3)
    lines = [
        "设备编号：OCR-2026-0919",
        "巡检区域：核心通信机房",
        "检修结论：备用链路已通过人工复核",
        "复核日期：2026年9月19日",
    ]
    y = 360
    for line in lines:
        draw.text((180, y), line, fill="black", font=body_font)
        y += 110
    draw.rectangle((145, 320, 1510, y + 20), outline="black", width=3)
    draw.text(
        (180, 2050),
        "说明：本页内容全部绘制在图像中，PDF 不包含可提取文字层。",
        fill=(40, 40, 40),
        font=small_font,
    )

    image.save(IMAGE_PATH, format="PNG")
    image.save(PDF_PATH, format="PDF", resolution=150.0)
    reader = PdfReader(str(PDF_PATH))
    extracted = "".join(page.extract_text() or "" for page in reader.pages).strip()
    if extracted:
        raise RuntimeError("扫描夹具意外包含文字层")
    return PDF_PATH


def main() -> int:
    path = build_fixture()
    print(f"扫描型 PDF：{path}")
    print("文字层检查：0 个字符")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
