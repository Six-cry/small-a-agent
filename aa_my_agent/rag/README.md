# RAG 模块

这个目录负责知识库检索，不负责 Agent 循环，也不负责生成最终回答。

RAG 升级过程中遇到的问题、原因、修改、验证结果和遗留事项统一记录在本目录的 `RAG_UPGRADE_LOG.md`。开始新的 RAG 工作前应先阅读该文件，完成实验或正式修改后应追加记录。

## 数据流

```text
rag/data/knowledge 中的 PDF、DOCX、TXT
        ↓
source_chunk_builder.py 按类型路由
        ├─ PDF → structured_pdf_loader.py → Docling结构化Chunk
        └─ DOCX/TXT → document_loader.py → text_splitter.py
        ↓
vector_store.py 生成向量并保存到 Chroma
        ↓
同一批 Chunk 建立本地 BM25 关键词旁路索引
        ↓
同步命令结束；小 A 只读取已经完成的索引
        ↓
hybrid_retriever.py 同时执行 Chroma 与 BM25 召回
        ↓
按 chunk_id 去重、RRF 融合、证据门槛过滤
        ↓
reranked_retriever.py 对已通过门槛的候选执行本地精排
        ↓
adjacent_context.py 补充同文件前后 Chunk
        ↓
service.py 整理核心证据、补充上下文与可靠引用
        ↓
tools/definitions.py 中的 search_knowledge 工具
        ↓
现有 Agent 主模型根据参考资料回答
```

## 文件职责

- `source_chunk_builder.py`：生产文件路由；PDF 走 Docling，DOCX/TXT 保留原专用 Loader。
- `structured_pdf_loader.py`：按文件 Hash 复用 PDF 结构化缓存，缺失时运行 Docling、公式路由与可选图片描述。
- `document_loader.py`：读取 DOCX/TXT；仍保留旧 PDF Loader 接口用于兼容，但默认生产路由不再使用它处理 PDF。
- `text_splitter.py`：把长文档切成带重叠的文本块。
- `vector_store.py`：管理 Embedding、Chroma 持久化和相似度检索。
- `lexical_index.py`：使用同一批 Chunk 建立和持久化本地 BM25 关键词索引，不调用外部模型。
- `hybrid_retriever.py`：执行 Chroma 与 BM25 双路候选召回、按 `chunk_id` 去重、RRF 融合和证据过滤。
- `reranked_retriever.py`：对通过证据门槛的候选执行本地 Cross-Encoder 精排，失败时回退 RRF。
- `adjacent_context.py`：在精排后读取同一来源前后 Chunk；排除图片检索短子块，并执行全局去重和字符预算。
- `index_status.py`：在启动和检索阶段只读比较知识文件与 Manifest；发现变化时只提醒，不执行入库。
- `sync_knowledge.py`：独立知识库管理入口，复用 `IndexManager` 的 `status/sync/rebuild`。
- `KNOWLEDGE_SYNC_GUIDE.md`：添加文件、查看状态、增量同步、重建和启动小 A 的完整操作说明。
- `image_parent_child.py`：从完整 `image_summary` 生成短 `image_retrieval` 子块，保留父块 ID，并建立 `parent_chunk_id`。
- `image_verifier.py`：命中图片父块后，按当前问题安全定位原裁图并执行一次性视觉核验；结果不回写知识库。
- `service.py`：向工具层提供混合检索后的稳定参考资料接口；BM25 异常时自动退回纯 Chroma。
- `RAG_UPGRADE_LOG.md`：持续保存 RAG 升级历史、实验结论和后续事项。
- `IMAGE_INGESTION_DESIGN.md`：图片摘要、裁图、元数据、质量门槛与尚未完成的按问题精确读图设计。
- `multimodal_parser.py`：统一解析 PDF 的文字、表格、公式和图片，输出可审查的 Markdown、JSON、结构化 Chunk 与裁图。
- `structured_document_builder.py`：把 Docling 元素按结构组装为 `text`、`table`、`formula`、`image_summary` Documents，使表格/公式/图片与相邻说明保持在同一语义单元。
- `__init__.py`：RAG 包入口，后续可以统一导出 `RagService`。

## 统一多模态 PDF 解析实验

首次使用先安装实验依赖：

```powershell
pip install -r aa_my_agent/requirements-rag-multimodal.txt
```

解析整份 PDF：

```powershell
python -m aa_my_agent.rag.multimodal_parser "aa_my_agent/rag/data/knowledge/文件名.pdf"
```

只解析连续页：

```powershell
python -m aa_my_agent.rag.multimodal_parser "aa_my_agent/rag/data/knowledge/文件名.pdf" --pages 2-4
```

输出默认保存在 `aa_my_agent/eval/rag/reports/unified_parser/`。其中：

- `combined.md`：按页合并的文字、Markdown 表格、LaTeX 公式和图片描述；
- `pages/`：逐页 Markdown；
- `images/`：Docling 高分辨率裁图；
- `manifest.json`：页码、图注、图片 Hash、模型、质量状态和各类数量。
- `docling_document.json`：不含图片二进制的 Docling 结构缓存；调整分块规则时可以直接复用；
- `documents.jsonl`：结构适配后的语义 Documents；
- `chunks.jsonl`：经过现有 `TextSplitter`、带稳定 `chunk_id` 的最终待入库 Chunks。

只修改了结构分块规则时，不必重新运行耗时的 Docling 公式模型，可从缓存重建：

```powershell
python -m aa_my_agent.eval.rag.rebuild_structured --list
python -m aa_my_agent.eval.rag.rebuild_structured "aa_my_agent/eval/rag/reports/unified_parser/考虑排队时延的系统保护通信网络路由选择算法_刘川_facc3bb0cdde_pages_0002-0004"
```

第一条命令用于列出真实目录；第二条是当前代表性样本的完整示例。文档中的“解析结果目录”只是说明文字，不能把这几个字原样当成路径。

结构化分块采用以下规则：

- 普通正文继续使用原有递归字符切分；
- 公式与紧邻的引导句、`式中/其中`变量解释组成一个 Document；
- 表格与表题、`见表`引导段、表后说明组成一个 Document；
- 图片与图题、相关正文、视觉模型摘要和裁图路径组成一个 Document；
- 上述三类结构块带有 `preserve_as_unit=true`，原有 `TextSplitter` 不再从中间拆开；
- 超长表格只按完整数据行拆分，每段重复表头并共享 `group_id`。

未配置 `ZHIPUAI_API_KEY` 时仍会解析文字、表格、公式并保存裁图，只是图片状态会标记为 `caption_only`。统一解析器本身只生成产物；独立同步命令由 `structured_pdf_loader.py` 读取这些 Chunk，再交给现有向量存储与 Manifest 流程。

## 独立知识库同步与在线只读检索

小 A 启动和知识检索不再写入知识库，只运行轻量只读检查。发现新增、修改或删除文件时会继续使用上一次成功索引，并提示退出小 A 后执行：

```powershell
python -m aa_my_agent.rag.sync_knowledge status
python -m aa_my_agent.rag.sync_knowledge sync
python -m aa_my_agent.rag.sync_knowledge verify
```

`status` 相当于快速 dry-run，只比较文件元数据；`verify` 执行完整 Hash 与 Chroma 只读校验；`sync` 才会执行 `SourceScanner → IndexDiff → IndexManager` 增量入库。发生新增或修改时，`SourceChunkBuilder` 根据文件类型选择解析器：

```text
.pdf  → StructuredPdfChunkLoader → Docling
.docx → Docx2txtLoader → 原TextSplitter
.txt  → TextLoader → 原TextSplitter
```

PDF 处理规则：

- 优先查找 `aa_my_agent/storage/rag_pdf_cache/` 中与源文件 Hash 匹配的完整缓存；
- 为兼容已完成的升级结果，也会只读复用 `eval/rag/reports/unified_parser/` 中的完整缓存；
- 没有缓存时使用 PyPdfium 后端运行 Docling；
- 检测到公式特征时按页保存检查点并启用公式富化；
- 普通 PDF 使用快速模式，发现未解码公式时回退到公式模式；
- 解析和向量化成功后才删除被修改文件的旧 Chunk；失败时继续保留旧索引；
- `RAG_DESCRIBE_PDF_IMAGES=false` 可关闭外部 GLM 图片描述；默认开启，开启时图片可能发送给配置的外部视觉服务。

未变化文件只比较快照和 Manifest，不加载 Docling、Embedding 或图片服务。

完整日常操作、失败处理和 `rebuild` 注意事项见
[`KNOWLEDGE_SYNC_GUIDE.md`](KNOWLEDGE_SYNC_GUIDE.md)。

## 正式混合检索

当前查询不再只取 Chroma 的最终 3 条，而是先建立较大的候选池：

```text
用户问题
  ├─ Chroma 稠密向量召回 20 条
  └─ BM25 精确词项召回 20 条
          ↓
      按 chunk_id 合并去重
          ↓
      RRF 倒数排名融合
          ↓
  向量距离或强关键词证据门槛
          ↓
      BGE Reranker 精排 10 条候选
          ↓
      最终最多保留 5 条核心证据
          ↓
      每条补充前后各 1 个 Chunk（全局最多 8000 字符）
```

BM25 文件位于 `aa_my_agent/storage/rag_bm25_index.json`。它记录当前 Collection、全部 Chunk ID 指纹、文本、元数据和分词结果。第一次检索以及 Chroma 内容发生变化后，系统会先核对指纹；不一致时从 Chroma 中的正式 Chunk 自动重建，不重新解析 PDF，也不调用外部 Embedding。

默认配置位于 `config.py`：

- `RAG_RETRIEVAL_MODE=hybrid`：正式启用混合检索；临时设为 `dense` 可退回纯向量模式；
- Chroma 和 BM25 分别召回 20 条候选；
- RRF 常数为 60；
- 最终返回数量由 `RAG_TOP_K=5` 控制；固定题库实测 Top 3 会截掉两条已经召回的正确证据，而 Top 5 在保留两道无答案题的同时达到 11/11；
- 词法补救要求 BM25 前 5、查询词权重覆盖率不低于 0.15，并至少匹配 3 个词项；
- BM25 缺失、损坏或构建失败时自动使用 `dense-fallback`，不阻断知识检索。
- Reranker 默认使用本地 `BAAI/bge-reranker-v2-m3`，模型失败时保留原 RRF 顺序。
- 邻接扩展发生在证据过滤和精排之后；相邻块不改变核心排名，也不能绕过无答案拒答。
- 返回结果为核心证据和带独立文件名、页码、Chunk ID 的相邻上下文；Agent 只能使用这些真实引用标识。

图片结果的父子展开已进入正式索引：短 `image_retrieval` 子块只负责召回；一旦命中，检索器按照 `parent_chunk_id` 读取并返回原 `image_summary` 完整父块，同时把同一父图片的重复候选合并为一条。邻接扩展会排除正式索引中的这些短子块，不会把检索卡片误当成相邻正文。

若图片父块包含 `image_path`，结果会提示可调用 `inspect_knowledge_image` 进行精确视觉核验。该工具默认关闭外部发送，只有显式设置 `RAG_ALLOW_ON_DEMAND_IMAGE_VERIFY=true` 才会把“单张裁图 + 当前问题”发给 GLM；返回结果标记为机器未核验，并且不会写回 Chroma。

Manifest 的索引签名除 Embedding 和字符切块参数外，还记录解析流水线版本、PDF 后端、公式模式、图片提示词版本和图片父子结构版本。当前 655-Chunk 正式库标记为 `docling-structured-v1 / disabled-v1`；隔离评测通过并正式迁移时必须同步提升版本，后续策略变化会要求完整重建，不会被文件 Hash 的“未变化”误跳过。

## 扫描件 OCR 回归

`aa_my_agent/eval/rag/fixtures/scan_01/scan_01_image_only.pdf` 是一个没有文字层的合成回归夹具。`pypdf` 对其提取结果为 0 个字符，但 Docling + RapidOCR 能恢复设备编号、巡检区域、检修结论和日期。解析后的正文标记为 `quality_status=machine_ocr`，不会再误标成 `native_text`。该夹具只证明技术链路可工作，不代表真实扫描件质量；固定题库中的 `scan-01` 仍需用户提供真实扫描 PDF 后才能启用。

## 隔离的结构化测试 Chroma

将当前代表性样本的 `chunks.jsonl` 写入独立测试库：

```powershell
python -m aa_my_agent.eval.rag.build_structured_test_index
```

该命令固定使用：

- 测试目录：`aa_my_agent/eval/rag/storage/structured_test_chroma`；
- 测试 Collection：`rag_structured_docling_test_v1`；
- 正式目录和正式 Collection 都有硬性禁止保护；
- Embedding 模型与生产配置相同，为 `text-embedding-v4`、1024 维；
- 构建后自动核对 Chroma ID，并运行 `table-01`、`image-01` 冒烟检索。

注意：真实 Embedding 会把 Chunk 文本发送给当前配置的外部 Embedding 服务。只有在确认允许外发相应文档文本后才能执行。

测试索引构建完成后，可只运行当前样本覆盖的题目：

```powershell
python -m aa_my_agent.eval.rag.run_eval --index-profile structured-test --only table-01,image-01,no-answer-01,no-answer-02 --report-name structured_test_partial
```

`structured-test` 会自动跳过生产 Manifest 同步，不会更新正式 Chroma。当前测试库只包含一篇论文第 2–4 页，不能用它评价其余文档对应的题目。

密集拓扑图在普通入库模式下只生成保守索引摘要，不生成完整边表。需要核对具体关系时使用：

```powershell
python -m aa_my_agent.rag.multimodal_parser "aa_my_agent/rag/data/knowledge/文件名.pdf" --pages 4 --image-question "节点7和节点11之间的边标注是什么？"
```

`manifest.json` 中的表格、公式和视觉描述默认标记为机器结果或 `unverified`。生成了 LaTeX 或图片文字不等于内容已经完全正确。

## 批量解析知识库全部 PDF

```powershell
python -m aa_my_agent.eval.rag.batch_parse_pdfs
```

批处理会递归发现 `rag/data/knowledge` 中的全部 PDF，逐个生成 Docling 缓存、Markdown、裁图、语义 Documents 和最终 Chunks。每完成一个文件都会更新：

- `eval/rag/reports/unified_parser/batch_all_pdfs.json`；
- `eval/rag/reports/unified_parser/BATCH_ALL_PDFS.md`。

再次运行时，源文件 Hash 没有变化且四项核心产物完整的 PDF 会直接复用。单个文件失败不会删除其他文件已经生成的结果。默认会把有效裁图发送给已配置的 GLM-4V-Flash；只有获得相应图片外发授权后才能启动。若只需要本地 Docling、裁图和图题，可显式使用 `--skip-image-description`。

全部 PDF 完成后，合并 PDF 与其他格式：

```powershell
python -m aa_my_agent.eval.rag.build_full_chunk_set
```

该步骤执行：

```text
全部 PDF → 只接受 Docling 全文 chunks.jsonl
DOCX/TXT → 原专用 Loader + 原 TextSplitter
两部分合并 → full_knowledge_chunks.jsonl
```

只要任一 PDF 缺少全文 Manifest、Docling 缓存、Documents 或 Chunks，合并就会拒绝执行，绝不会用旧 `PyPDFLoader` 结果冒充新版 PDF Chunk。合并结果保存在 `eval/rag/reports/full_knowledge_chunks/`。隔离测试 Chroma 构建器默认读取这份完整结果。

## 架构边界

RAG 模块只返回检索资料，不再次调用聊天模型。最终总结由当前 Agent 的主模型完成，避免一次问题调用两个聊天模型。

知识库构建与知识库查询要分开：查询可以作为安全工具自动调用；重建向量库耗时并会修改存储，不应默认暴露给模型。
