"""验证扫描型回归夹具没有文字层，并被标记为机器 OCR 来源。"""

from pathlib import Path

from pypdf import PdfReader

from aa_my_agent.rag.multimodal_parser import detect_pdf_text_quality_by_page


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "aa_my_agent"
    / "eval"
    / "rag"
    / "fixtures"
    / "scan_01"
    / "scan_01_image_only.pdf"
)


def test_scan_fixture_is_image_only_and_requires_ocr() -> None:
    assert FIXTURE.is_file()
    extracted = "".join(
        page.extract_text() or "" for page in PdfReader(str(FIXTURE)).pages
    ).strip()

    assert extracted == ""
    assert detect_pdf_text_quality_by_page(FIXTURE) == {1: "machine_ocr"}
