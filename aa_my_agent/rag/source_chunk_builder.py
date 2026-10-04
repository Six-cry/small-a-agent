"""按知识文件类型选择解析器并生成最终 Chunk。"""

from __future__ import annotations

from langchain_core.documents import Document

from .document_loader import DocumentLoader
from .image_parent_child import expand_image_parent_child_documents
from .models import SourceSnapshot
from .structured_pdf_loader import StructuredPdfChunkLoader
from .text_splitter import TextSplitter


class SourceChunkBuilderError(RuntimeError):
    """知识文件没有成功转换为可入库 Chunk。"""


class SourceChunkBuilder:
    """PDF 走 Docling；DOCX/TXT 继续使用原专用 Loader。"""

    def __init__(
        self,
        *,
        document_loader: DocumentLoader,
        text_splitter: TextSplitter,
        pdf_loader: StructuredPdfChunkLoader,
        enable_image_parent_child: bool = False,
    ):
        self.document_loader = document_loader
        self.text_splitter = text_splitter
        self.pdf_loader = pdf_loader
        self.enable_image_parent_child = bool(enable_image_parent_child)

    def build(self, snapshot: SourceSnapshot) -> list[Document]:
        if snapshot.file_type == ".pdf":
            chunks = self.pdf_loader.load_snapshot(snapshot)
            if self.enable_image_parent_child:
                chunks = expand_image_parent_child_documents(chunks)
        else:
            documents = self.document_loader.load_snapshot(snapshot)
            normalized_documents = [
                Document(
                    page_content=document.page_content,
                    metadata={
                        **document.metadata,
                        "content_type": "text",
                        "quality_status": "native_text",
                        "preserve_as_unit": False,
                    },
                )
                for document in documents
            ]
            chunks = self.text_splitter.split_documents(normalized_documents)

        if not chunks:
            raise SourceChunkBuilderError(
                f"知识文件没有生成任何 Chunk：{snapshot.relative_path}"
            )
        for chunk in chunks:
            if chunk.metadata.get("source") != snapshot.relative_path:
                raise SourceChunkBuilderError(
                    f"Chunk 来源与文件不一致：{snapshot.relative_path}"
                )
        return chunks


def create_default_source_chunk_builder(
    *,
    document_loader: DocumentLoader,
    text_splitter: TextSplitter,
) -> SourceChunkBuilder:
    from ..config import RAG_ENABLE_IMAGE_PARENT_CHILD
    from .structured_pdf_loader import create_default_structured_pdf_loader

    return SourceChunkBuilder(
        document_loader=document_loader,
        text_splitter=text_splitter,
        pdf_loader=create_default_structured_pdf_loader(),
        enable_image_parent_child=RAG_ENABLE_IMAGE_PARENT_CHILD,
    )


__all__ = [
    "SourceChunkBuilder",
    "SourceChunkBuilderError",
    "create_default_source_chunk_builder",
]
