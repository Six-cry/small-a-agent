"""RAG 向量存储层。

主要职责：

1. 创建 Embedding 模型。
2. 连接 Chroma 向量数据库。
3. 校验 Chunk 的内容指纹和唯一 ID。
4. 将 Chunk 写入 Chroma。
5. 根据 chunk_id 删除旧 Chunk。
6. 执行相似度检索。
7. 在完整重建时清空 Collection。

这个模块不负责：

- 扫描知识文件
- 读取 PDF、TXT、DOCX
- 切分文档
- 判断文件新增、修改、删除
- 更新 Manifest
- 自动构建整个知识库

上述流程将由后面的 IndexManager 统一协调。
"""
import math

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from ..config import (
    CHROMA_DIR,
    DASHSCOPE_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    RAG_COLLECTION_NAME,
    RAG_TOP_K,
)
from .text_splitter import (
    calculate_text_hash,
    create_chunk_id,
)


class VectorStoreError(RuntimeError):
    """向量数据库操作失败。"""

def create_embedding_model() -> OpenAIEmbeddings:
    """创建入库和查询共同使用的 Embedding 模型。"""
    if not DASHSCOPE_API_KEY:
        raise ValueError(
            "没有配置 DASHSCOPE_API_KEY"
        )

    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        api_key=DASHSCOPE_API_KEY,
        base_url=EMBEDDING_BASE_URL,
        dimensions=EMBEDDING_DIMENSION,
        chunk_size=10,
        check_embedding_ctx_length=False,
    )


class VectorStoreService:
    """负责管理 Chroma 中的 Chunk。"""

    def __init__(
        self,
        embedding_model: Embeddings | None = None,
        collection_name: str | None = None,
        persist_directory: Path | str | None = None,
    ):
        if embedding_model is None:
            embedding_model = (
                create_embedding_model()
            )

        if collection_name is None:
            collection_name = (
                RAG_COLLECTION_NAME
            )

        if persist_directory is None:
            persist_directory = CHROMA_DIR

        if (
            not isinstance(collection_name, str)
            or not collection_name.strip()
        ):
            raise ValueError(
                "collection_name 不能为空"
            )

        self.embedding_model = embedding_model

        self.collection_name = (
            collection_name.strip()
        )

        self.persist_directory = Path(
            persist_directory
        )

        self.persist_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        # 这里只连接或创建 Collection。
        # 不再自动读取、切分和嵌入知识文件。
        self.vector_store = (
            self._connect_vector_store()
        )

    def _connect_vector_store(self,) -> Chroma:
        """连接 Chroma Collection。

        如果 Collection 不存在，Chroma 会创建一个空的
        Collection，但不会自动加入任何知识文件。
        """
        try:
            return Chroma(
                collection_name=(
                    self.collection_name
                ),
                embedding_function=(
                    self.embedding_model
                ),
                persist_directory=str(
                    self.persist_directory
                ),
            )
        except Exception as exc:
            raise VectorStoreError(
                "无法连接 Chroma 向量数据库："
                f"{self.persist_directory}"
            ) from exc

    @staticmethod
    def _is_sha256(value: Any) -> bool:
        """判断一个值是否为小写 SHA-256 字符串。"""
        if not isinstance(value, str):
            return False

        if len(value) != 64:
            return False

        return all(
            character in "0123456789abcdef"
            for character in value
        )

    @staticmethod
    def _get_required_text_metadata(
        document: Document,
        field_name: str,
    ) -> str:
        """读取必须存在的字符串元数据。"""
        value = document.metadata.get(
            field_name
        )

        if (
            not isinstance(value, str)
            or not value.strip()
        ):
            raise VectorStoreError(
                "Chunk 缺少有效元数据："
                f"{field_name}"
            )

        return value.strip()

    def _validate_chunk(self, chunk: Document,) -> str:
        """检查 Chunk 的内容和指纹是否一致。

        返回校验成功后的 chunk_id。"""
        if not isinstance(chunk, Document):
            raise TypeError(
                "chunks 中的元素必须是 Document"
            )

        if (
            not isinstance(
                chunk.page_content,
                str,
            )
            or not chunk.page_content.strip()
        ):
            raise VectorStoreError(
                "Chunk 的 page_content 不能为空"
            )

        source = (
            self._get_required_text_metadata(
                document=chunk,
                field_name="source",
            )
        )

        file_hash = (
            self._get_required_text_metadata(
                document=chunk,
                field_name="file_hash",
            )
        )

        content_hash = (
            self._get_required_text_metadata(
                document=chunk,
                field_name="content_hash",
            )
        )

        chunk_id = (
            self._get_required_text_metadata(
                document=chunk,
                field_name="chunk_id",
            )
        )

        if not self._is_sha256(file_hash):
            raise VectorStoreError(
                f"文件 Hash 格式错误：{source}"
            )

        if not self._is_sha256(content_hash):
            raise VectorStoreError(
                f"内容 Hash 格式错误：{source}"
            )

        if not self._is_sha256(chunk_id):
            raise VectorStoreError(
                f"Chunk ID 格式错误：{source}"
            )

        chunk_index = chunk.metadata.get(
            "chunk_index"
        )

        total_chunks = chunk.metadata.get(
            "total_chunks"
        )

        if (
            isinstance(chunk_index, bool) # bool是int的子类
            or not isinstance(chunk_index, int)
            or chunk_index < 0
        ):
            raise VectorStoreError(
                "Chunk 缺少有效的 chunk_index："
                f"{source}"
            )

        if (
            isinstance(total_chunks, bool)
            or not isinstance(total_chunks, int)
            or total_chunks <= 0
        ):
            raise VectorStoreError(
                "Chunk 缺少有效的 total_chunks："
                f"{source}"
            )

        if chunk_index >= total_chunks:
            raise VectorStoreError(
                "chunk_index 不能大于或等于 "
                f"total_chunks：{source}"
            )

        # 重新计算文本内容指纹。
        expected_content_hash = (
            calculate_text_hash(
                chunk.page_content
            )
        )

        if content_hash != expected_content_hash:
            raise VectorStoreError(
                "Chunk 的 content_hash 与实际内容"
                f"不一致：{source}，"
                f"chunk_index={chunk_index}"
            )

        page = chunk.metadata.get("page")

        if not isinstance(page, int):
            page = None

        # 使用同样的规则重新生成 Chunk ID。
        expected_chunk_id = create_chunk_id(
            source=source,
            file_hash=file_hash,
            page=page,
            chunk_index=chunk_index,
            content=chunk.page_content,
        )

        if chunk_id != expected_chunk_id:
            raise VectorStoreError(
                "Chunk ID 与 Chunk 内容或来源"
                f"不一致：{source}，"
                f"chunk_index={chunk_index}"
            )

        return chunk_id

    @staticmethod
    def _clean_metadata_value(value: Any, ) -> str | int | float | bool | None:
        """将元数据转换成 Chroma 支持的类型。"""
        if value is None:
            return None

        if isinstance(value, (str, int, float, bool),):
            return value

        # PDF Loader 偶尔可能返回列表、日期等复杂类型。
        # Chroma 不能直接保存这些类型，因此转成字符串。
        return str(value)

    def _prepare_chunk(self, chunk: Document,) -> Document:
        """生成一个适合写入 Chroma 的 Document。

        不修改 TextSplitter 返回的原始 Chunk。
        """
        cleaned_metadata = {}

        for key, value in (
            chunk.metadata.items()
        ):
            cleaned_value = (
                self._clean_metadata_value(
                    value
                )
            )

            # Chroma 元数据不保存 None。
            if cleaned_value is not None:
                cleaned_metadata[str(key)] = (
                    cleaned_value
                )

        return Document(
            page_content=chunk.page_content,
            metadata=cleaned_metadata,
        )

    def add_chunks(self, chunks: Iterable[Document],) -> tuple[str, ...]:
        """校验、嵌入并写入一批 Chunk。

        Chroma 中的向量 ID 直接使用 chunk_id。

        返回成功写入的全部 chunk_id。
        """
        chunk_list = list(chunks)

        if not chunk_list:
            return ()

        chunk_ids = []
        seen_chunk_ids = set()
        prepared_chunks = []

        for chunk in chunk_list:
            chunk_id = self._validate_chunk(chunk)

            # 防止同一批次中出现重复 Chunk。
            if chunk_id in seen_chunk_ids:
                raise VectorStoreError(
                    "同一批入库数据中出现重复的 "
                    f"chunk_id：{chunk_id}"
                )

            seen_chunk_ids.add(chunk_id)
            chunk_ids.append(chunk_id)

            prepared_chunks.append(
                self._prepare_chunk(chunk)
            )

        try:
            returned_ids = (
                self.vector_store.add_documents(
                    documents=prepared_chunks,
                    ids=chunk_ids,
                )
            ) #returned_ids是list
        except Exception as exc:
            raise VectorStoreError(
                "Chunk 写入 Chroma 失败，"
                f"本批次共 {len(chunk_list)} 个 Chunk"
            ) from exc

        # 正常情况下返回值应和传入的 ID 一致。
        if (
            returned_ids
            and set(returned_ids) != set(chunk_ids) # 比较元素用集合不需要考虑位置
        ):
            raise VectorStoreError(
                "Chroma 返回的 ID 与请求写入的 "
                "chunk_id 不一致"
            )

        return tuple(chunk_ids)# 元组只能读取，不能修改

    def delete_chunks(self, chunk_ids: Iterable[str], ) -> None:
        """根据 chunk_id 删除 Chroma 中的 Chunk。"""
        normalized_ids = []
        seen_ids = set()

        for chunk_id in chunk_ids:
            if (
                not isinstance(chunk_id, str)
                or not chunk_id.strip()
            ):
                raise ValueError(
                    "chunk_id 必须是非空字符串"
                )

            normalized_id = chunk_id.strip()

            if normalized_id in seen_ids:
                continue

            seen_ids.add(normalized_id)
            normalized_ids.append(normalized_id)

        if not normalized_ids:
            return

        try:
            self.vector_store.delete(ids=normalized_ids)
        except Exception as exc:
            raise VectorStoreError(
                "从 Chroma 删除 Chunk 失败，"
                f"请求删除 {len(normalized_ids)} 个"
            ) from exc

    def reset_collection(self) -> None:
        """清空当前 Collection，并核对没有残留 Chunk。

        保留 Collection 本身，避免删除后重连时依赖底层客户端的私有接口。
        """
        existing_ids = self.get_chunk_ids()
        if existing_ids:
            self.delete_chunks(existing_ids)
        remaining_ids = self.get_chunk_ids()
        if remaining_ids:
            raise VectorStoreError(
                "清空 Chroma Collection 后仍有残留 Chunk："
                f"{len(remaining_ids)} 个"
            )

    def get_chunk_ids(self, ) -> tuple[str, ...]:
        """返回 Collection 中目前存在的全部 ID。"""
        try:
            result = self.vector_store.get(
                include=[]
            )
        except Exception as exc:
            raise VectorStoreError(
                "无法读取 Chroma 中的 Chunk ID"
            ) from exc

        ids = result.get("ids", [])

        return tuple(
            str(chunk_id)
            for chunk_id in ids
        )

    def get_all_documents(self) -> list[Document]:
        """读取 Collection 中全部 Chunk，供本地 BM25 建立旁路索引。"""
        try:
            result = self.vector_store.get(
                include=["documents", "metadatas"]
            )
        except Exception as exc:
            raise VectorStoreError(
                "无法读取 Chroma 中的完整 Chunk"
            ) from exc

        ids = result.get("ids", [])
        documents = result.get("documents", [])
        metadatas = result.get("metadatas", [])
        if not (
            isinstance(ids, list)
            and isinstance(documents, list)
            and isinstance(metadatas, list)
            and len(ids) == len(documents) == len(metadatas)
        ):
            raise VectorStoreError(
                "Chroma 返回的 ID、正文和元数据数量不一致"
            )

        rows: list[tuple[str, Document]] = []
        for chunk_id, content, metadata in zip(
            ids,
            documents,
            metadatas,
            strict=True,
        ):
            if not isinstance(content, str) or not isinstance(metadata, dict):
                raise VectorStoreError("Chroma 返回了无效的 Chunk 内容")
            normalized_metadata = dict(metadata)
            normalized_metadata.setdefault("chunk_id", str(chunk_id))
            rows.append(
                (
                    str(chunk_id),
                    Document(
                        page_content=content,
                        metadata=normalized_metadata,
                    ),
                )
            )

        rows.sort(key=lambda row: row[0])
        return [document for _chunk_id, document in rows]

    def get_documents_by_ids(
        self,
        chunk_ids: Iterable[str],
    ) -> dict[str, Document]:
        """按 ID 批量读取父 Chunk，供检索子块命中后展开完整内容。"""
        normalized_ids = tuple(
            dict.fromkeys(
                chunk_id.strip()
                for chunk_id in chunk_ids
                if isinstance(chunk_id, str) and chunk_id.strip()
            )
        )
        if not normalized_ids:
            return {}
        try:
            result = self.vector_store.get(
                ids=list(normalized_ids),
                include=["documents", "metadatas"],
            )
        except Exception as exc:
            raise VectorStoreError("无法按 ID 读取图片父 Chunk") from exc

        ids = result.get("ids", [])
        documents = result.get("documents", [])
        metadatas = result.get("metadatas", [])
        if not (
            isinstance(ids, list)
            and isinstance(documents, list)
            and isinstance(metadatas, list)
            and len(ids) == len(documents) == len(metadatas)
        ):
            raise VectorStoreError("按 ID 读取时 Chroma 返回的数据数量不一致")

        found: dict[str, Document] = {}
        for chunk_id, content, metadata in zip(
            ids, documents, metadatas, strict=True
        ):
            if not isinstance(content, str) or not isinstance(metadata, dict):
                raise VectorStoreError("按 ID 读取时 Chroma 返回了无效 Chunk")
            normalized_metadata = dict(metadata)
            normalized_metadata.setdefault("chunk_id", str(chunk_id))
            found[str(chunk_id)] = Document(
                page_content=content,
                metadata=normalized_metadata,
            )
        return found

    def count(self) -> int:
        """返回 Collection 中的 Chunk 数量。"""
        return len(self.get_chunk_ids())
    
    def search_with_scores(
        self,
        query: str,
        k: int = RAG_TOP_K,
    ) -> list[tuple[Document, float]]:
        """执行相似度检索并返回原始距离。
    
        返回格式：
    
        [
            (Document, distance),
            (Document, distance),
        ]
    
        对当前 Chroma 的距离分数来说：
    
        - 距离越小，表示越相似
        - 距离越大，表示越不相关
        - 完全相同的内容距离通常接近 0
    
        注意：这里返回的是 distance，不是“百分比相似度”。
        """
        if (
            not isinstance(query, str)
            or not query.strip()
        ):
            raise ValueError(
                "query 不能为空"
            )
    
        if (
            isinstance(k, bool)
            or not isinstance(k, int)
            or k <= 0
        ):
            raise ValueError(
                "k 必须是正整数"
            )
    
        if self.count() == 0:
            return []
    
        try:
            raw_results = (
                self.vector_store
                .similarity_search_with_score(
                    query=query.strip(),
                    k=k,
                )
            )
        except Exception as exc:
            raise VectorStoreError(
                "Chroma 带分数检索失败"
            ) from exc
    
        results: list[
            tuple[Document, float]
        ] = []
    
        for document, score in raw_results:
            try:
                distance = float(score)
            except (TypeError, ValueError) as exc:
                raise VectorStoreError(
                    "Chroma 返回了无效的距离分数："
                    f"{score}"
                ) from exc
    
            if not math.isfinite(distance):
                raise VectorStoreError(
                    "Chroma 返回了非有限距离："
                    f"{distance}"
                )
    
            results.append(
                (document, distance)
            )
    
        return results
    
    
    def search(
        self,
        query: str,
        k: int = RAG_TOP_K,
    ) -> list[Document]:
        """兼容原有接口，只返回 Document。"""
        scored_results = (
            self.search_with_scores(
                query=query,
                k=k,
            )
        )
    
        return [
            document
            for document, _distance
            in scored_results
        ]

def main() -> None:
    """人工检查 Chroma 连接状态。"""
    service = VectorStoreService()

    print("Chroma 连接成功")
    print(
        f"Collection：{service.collection_name}"
    )
    print(
        "持久化目录："
        f"{service.persist_directory}"
    )
    print(
        f"当前 Chunk 数量：{service.count()}"
    )


if __name__ == "__main__":
    main()
