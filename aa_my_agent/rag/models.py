"""RAG 模块内部使用的数据结构。

这一层只描述数据，不读取文件、不访问 Chroma，也不调用模型。
"""

from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """知识文件在某一时刻的状态快照。

    relative_path:
        相对于 rag/data/knowledge 的路径，统一使用正斜杠。
    file_hash:
        文件内容的 SHA-256，用来判断内容是否发生变化。
    file_size:
        文件大小，单位为字节。
    modified_at_ns:
        文件最后修改时间的纳秒时间戳。
    file_type:
        小写扩展名，例如 .pdf、.txt。
    """

    relative_path: str
    file_hash: str
    file_size: int
    modified_at_ns: int
    file_type: str

    def to_dict(self) -> dict:
        """转换为可写入 JSON 的普通字典。"""
        return asdict(self) # 把 SourceSnapshot 实例变成字典

    @classmethod
    def from_dict(cls, data: dict) -> "SourceSnapshot":
        """从普通字典恢复快照，并复用 dataclass 的字段校验。"""
        return cls(
            relative_path=str(data["relative_path"]),
            file_hash=str(data["file_hash"]),
            file_size=int(data["file_size"]),
            modified_at_ns=int(data["modified_at_ns"]),
            file_type=str(data["file_type"]),
        )
    
@dataclass(frozen=True, slots=True)
class IndexSignature:
    """构建当前向量索引时使用的关键配置。

    这些配置任意一个发生改变，都不能继续混用旧索引。
    """

    collection_name: str
    embedding_model: str
    embedding_dimension: int
    chunk_size: int
    chunk_overlap: int
    separators: tuple[str, ...]
    pipeline_version: str = "docling-structured-v1"
    pdf_parser: str = "docling-pypdfium2"
    formula_mode: str = "auto-formula-enrichment-v1"
    image_prompt_version: str = "conservative-v1+topology-index-safe-v1"
    image_parent_child_version: str = "disabled-v1"

    def to_dict(self) -> dict:
        """转换为可以写入 JSON 的普通字典。"""
        return {
            "collection_name": self.collection_name,
            "embedding_model": self.embedding_model,
            "embedding_dimension": (
                self.embedding_dimension
            ),
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "separators": list(self.separators),
            "pipeline_version": self.pipeline_version,
            "pdf_parser": self.pdf_parser,
            "formula_mode": self.formula_mode,
            "image_prompt_version": self.image_prompt_version,
            "image_parent_child_version": self.image_parent_child_version,
        }

    @classmethod
    def from_dict(cls, data: dict,) -> "IndexSignature":
        """从 Manifest 字典恢复索引签名。"""
        return cls(
            collection_name=str(
                data["collection_name"]
            ),
            embedding_model=str(
                data["embedding_model"]
            ),
            embedding_dimension=int(
                data["embedding_dimension"]
            ),
            chunk_size=int(
                data["chunk_size"]
            ),
            chunk_overlap=int(
                data["chunk_overlap"]
            ),
            separators=tuple(
                str(item)
                for item in data["separators"]
            ),
            # 旧 Manifest 没有这些字段时按当前 655-Chunk 正式版本读取，
            # 避免仅因升级了读取代码就立刻阻断小 A；以后版本变化仍会比较失败。
            pipeline_version=str(
                data.get("pipeline_version", "docling-structured-v1")
            ),
            pdf_parser=str(data.get("pdf_parser", "docling-pypdfium2")),
            formula_mode=str(
                data.get("formula_mode", "auto-formula-enrichment-v1")
            ),
            image_prompt_version=str(
                data.get(
                    "image_prompt_version",
                    "conservative-v1+topology-index-safe-v1",
                )
            ),
            image_parent_child_version=str(
                data.get("image_parent_child_version", "disabled-v1")
            ),
        )


@dataclass(frozen=True, slots=True)
class IndexedSourceRecord:
    """一份已经成功写入 Chroma 的知识文件记录。

    SourceSnapshot 只表示当前看到的文件。
    IndexedSourceRecord 表示文件已经成功完成了入库。
    """

    relative_path: str
    file_hash: str
    file_size: int
    modified_at_ns: int # 当时入库时的修改时间
    file_type: str
    chunk_ids: tuple[str, ...] # 这本书被切成了哪些索引卡片
    indexed_at: str # 入库时间

    def to_dict(self) -> dict:
        """转换为可以写入 Manifest 的普通字典。"""
        return {
            "relative_path": self.relative_path,
            "file_hash": self.file_hash,
            "file_size": self.file_size,
            "modified_at_ns": self.modified_at_ns,
            "file_type": self.file_type,
            "chunk_ids": list(self.chunk_ids),
            "indexed_at": self.indexed_at,
        }

    @classmethod
    def from_dict(cls, data: dict,) -> "IndexedSourceRecord":
        """从 Manifest 字典恢复已入库记录。"""
        return cls(
            relative_path=str(
                data["relative_path"]
            ),
            file_hash=str(
                data["file_hash"]
            ),
            file_size=int(
                data["file_size"]
            ),
            modified_at_ns=int(
                data["modified_at_ns"]
            ),
            file_type=str(
                data["file_type"]
            ),
            chunk_ids=tuple(
                str(chunk_id)
                for chunk_id in data["chunk_ids"]
            ),
            indexed_at=str(
                data["indexed_at"]
            ),
        )

    @classmethod
    def from_snapshot(cls, snapshot: SourceSnapshot,
        chunk_ids: Iterable[str], indexed_at: str,
    ) -> "IndexedSourceRecord":
        """在文件成功入库后，从扫描快照生成记录。"""
        return cls(
            relative_path=snapshot.relative_path,
            file_hash=snapshot.file_hash,
            file_size=snapshot.file_size,
            modified_at_ns=(
                snapshot.modified_at_ns
            ),
            file_type=snapshot.file_type,
            chunk_ids=tuple(chunk_ids),
            indexed_at=indexed_at,
        )
