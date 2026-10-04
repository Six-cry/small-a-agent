# RAG 最小测试基线

这是一张固定的小考试卷，用来比较 RAG 升级前后是否真的变好。

整个升级过程中遇到的问题、原因、修正和验证结果统一记录在 `aa_my_agent/rag/RAG_UPGRADE_LOG.md`。每次完成新的 RAG 实验或正式修改后，都要追加记录。

它只测试“有没有把正确资料找回来”，暂时不测试主 Agent 最终回答写得好不好。这样一旦失败，可以优先检查 PDF 解析、切块、索引和检索，不会把大模型生成问题混在一起。

## 目录中各文件的作用

- `cases.json`：12 道固定题目，其中11道已根据原始PDF核对并开启。
- `run_eval.py`：逐题检索、自动判分并生成报告。
- `reports/`：保存升级前、升级后的结果。

## 当前题库

题库已经开启5道纯文字题、2道表格题、2道图片题和2道知识库无答案题。

`scan-01` 暂未开启，因为当前知识库中的PDF都带有文字层，没有真正的扫描版PDF。添加真实扫描件并人工确认答案后，再填写并开启该题。

`keywords` 中填写答案原文里最有辨识度的短词。`keyword_mode` 为 `any` 时，多个关键词命中任意一个即可；设为 `all` 时，同一检索片段必须包含全部关键词，适合表格和图片题。比较时会忽略 PDF 抽取过程产生的空格，例如 `内生6G` 可以匹配 `内生 6G`。`pages` 使用 PDF 阅读器显示的页码，从 1 开始；如果答案可能跨页，可以写成 `[3, 4]`。

## 运行方法

在项目根目录执行：

```powershell
python -m aa_my_agent.eval.rag.run_eval --validate-only
```

这一步只检查题库格式，不会调用向量模型。格式正确后运行：

```powershell
python -m aa_my_agent.eval.rag.run_eval
```

程序会先确认知识库索引是最新的，再按当前正式配置进行检索。默认检查前 5 条结果，并使用项目当前的距离阈值。报告会写入：

- `reports/baseline_before_upgrade.md`：便于人阅读和比较。
- `reports/baseline_before_upgrade.json`：保留详细数据，便于以后自动画图或统计。

升级后不要覆盖升级前报告，可以换一个名字：

```powershell
python -m aa_my_agent.eval.rag.run_eval --report-name after_docling
```

## 判分规则

有答案题需要在当前距离阈值内同时满足：

1. 找到预期文件；
2. 找到预期页码（`pages` 留空时不检查页码）；
3. 检索片段符合关键词模式：`any` 要求至少一个，`all` 要求全部出现（`keywords` 留空时不检查关键词）。

无答案题只有在没有任何结果通过当前距离阈值时才算通过。

表格、图片和扫描件题在升级前失败是正常现象。这个失败记录正是后续证明 OCR、表格识别和图片理解有效的参照物。

## Docling独立解析试验

`docling_probe.py`只解析两份图片题对应的PDF并导出Markdown，不会修改正式RAG索引。

安装Docling后，在项目根目录运行：

```powershell
python -m aa_my_agent.eval.rag.docling_probe
```

输出保存在`reports/docling_probe/`。程序会自动检查拓扑图中的`0.232`、`0.992`，以及结构图中的`虚拟队列积压`、`历史决策经验`是否被成功提取。

如果只需要重试第二份PDF的目标页：

```powershell
python -m aa_my_agent.eval.rag.docling_probe --only image-02
```

公式转 LaTeX 测试：

```powershell
python -m aa_my_agent.eval.rag.docling_probe --only formula-01
```

复用已经生成的 Markdown，只执行公式准确性检查：

```powershell
python -m aa_my_agent.eval.rag.docling_probe --only formula-01 --reuse-output
```

## GLM-4V-Flash 图片描述试验

图片测试使用 Docling 定位并裁出图片，然后将单张裁图发送给智谱 `glm-4v-flash` 生成可检索的文字描述。该流程不会修改正式 RAG 索引。

即使 `glm-4v-flash` 是免费模型，调用官方 API 仍需要 API Key。请在 `aa_my_agent/.env` 中配置：

```dotenv
ZHIPUAI_API_KEY=你的智谱APIKey
```

不要把真实 Key 写进 Python 文件或提交到版本库。配置完成后分别运行：

```powershell
python -m aa_my_agent.eval.rag.docling_probe --only picture-01
python -m aa_my_agent.eval.rag.docling_probe --only picture-02
```

输出保存在 `reports/docling_probe/`，裁出的图片保存在其下的 `pictures/`。关键词只在模型生成的图片描述中检查，不会使用图片附近正文冒充识别结果。

图片题还会检查“关系”而不只检查孤立关键词。例如拓扑题必须在同一条边记录中把节点 7、节点 11 与 `(0.232, 0.992)` 对应起来。复用现有报告、避免重新调用 API 的方法是：

```powershell
python -m aa_my_agent.eval.rag.docling_probe --only picture-01 --reuse-output
python -m aa_my_agent.eval.rag.docling_probe --only picture-02 --reuse-output
```

关键词齐全但关系错误时，程序会返回未通过。最终仍需将代表性输出与原 PDF 人工对照。

密集拓扑图探针使用 4 倍 Docling 图片缩放，并且只向 GLM 询问目标节点关系。实验已经证明：针对具体问题读高分辨率裁图可以答对目标边，但让模型一次生成完整边表仍会产生大量错配。因此不能把“单题通过”解释成“整张拓扑已被完整结构化”。

## 全知识库 PDF 解析与 Chunk 合并

全量解析入口：

```powershell
python -m aa_my_agent.eval.rag.batch_parse_pdfs
```

处理规则：

- 递归扫描知识库全部 PDF；
- 使用 Docling 的 PyPdfium 后端，避开特定 PDF 会触发的 `docling-parse` 原生崩溃；
- 普通 PDF 使用 Docling 快速模式，仍识别文字、标题、表格和图片；
- 检测到公式特征的 PDF 开启公式富化，并按页保存检查点；
- 全部页面完成后重新合并为全文 Docling 结构、Documents 和 Chunks；
- 源文件 Hash 未变化且全文四项产物齐全时直接复用；
- 默认会把非装饰裁图发送给已配置的 GLM-4V-Flash；不希望外发图片时使用 `--skip-image-description`。

批报告写入：

- `reports/unified_parser/BATCH_ALL_PDFS.md`
- `reports/unified_parser/batch_all_pdfs.json`

全部 PDF 完成后，将 PDF Docling Chunk 与 DOCX/TXT 的原专用 Loader 结果合并：

```powershell
python -m aa_my_agent.eval.rag.build_full_chunk_set
```

输出写入 `reports/full_knowledge_chunks/`。只要任一当前 PDF 缺少 Hash 匹配的全文缓存，合并器就会拒绝生成一个不完整的“全库”文件。

修改结构化组装规则后，不必重新运行 Docling 或再次调用图片模型，可以从现有全文缓存批量重建：

```powershell
python -m aa_my_agent.eval.rag.rebuild_structured --all-complete
python -m aa_my_agent.eval.rag.build_full_chunk_set
```

当前真实结果为 15 个来源、655 个 Chunk。中文词内的异常空格会在建索引前清理；图片 Chunk 会在原描述前增加紧凑检索摘要，已有的“节点范围 1-12”会展开为节点检索别名，但不会据此编造边关系。

构建并评测隔离测试 Chroma：

```powershell
python -m aa_my_agent.eval.rag.build_structured_test_index
python -m aa_my_agent.eval.rag.run_eval --index-profile structured-test --report-name after_retrieval_index_fix
```

这两个命令只使用 `eval/rag/storage/structured_test_chroma` 和测试 Collection，不会改写正式 Chroma。构建索引会把 Chunk 文本发送给配置的外部 Embedding 服务；运行评测会把题库中的测试问题发送给该服务，执行前必须分别确认相应外发权限。

2026-09-18 的完整结果为 8/11，通过项包括 4 道正文、2 道表格和2道无答案题。三道失败题说明仅靠向量召回和索引文本前缀仍不足以稳定检索指定正文与图片。

用户明确选择先在小 A 中试用后，生产配置已切换到 `rag_collection_knowledge_docling_v1`：15 个来源、655 个 Chunk。切换采用新旧 Collection 并存方式，旧 `rag_collection_knowledge` 没有删除；旧配置和 Manifest 备份在 `eval/rag/storage/production_backups/20260918_205913/`。修改配置后需要重新启动小 A，已经运行的进程不会自动重新导入 Collection 名称。

## RRF 后的本地 Reranker 隔离评测

当前正式检索使用“向量召回 + BM25 召回 + RRF + 证据门槛 + 本地 Cross-Encoder”。RRF 负责合并两个召回排名，精排器再逐条读取“问题-Chunk”关系，并且只给已经通过证据门槛的候选重新打分。正式 Chroma 不需要改变，Reranker 已由 `RagService` 按配置启用。

可选依赖：

```powershell
python -m pip install -r aa_my_agent/requirements-rag-reranker.txt
```

推荐使用适合中文和多语言的本地模型 `BAAI/bge-reranker-v2-m3`。首次使用会下载模型；模型下载完成后，问题和候选 Chunk 都在本机计算，不会调用外部 Reranker API。

运行 11 道固定题的 RRF 与 RRF + Reranker 隔离对比：

```powershell
python -m aa_my_agent.eval.rag.run_rerank_eval `
  --model BAAI/bge-reranker-v2-m3 `
  --index-profile production `
  --report-name after_reranker_isolated
```

默认不会触发知识库同步；如果确实需要先同步，再显式增加 `--sync`。报告写入 `reports/after_reranker_isolated.md/.json`。

2026-09-26 的真实评测结果为：候选池 20 和候选池 10 均保持 11/11，错误回退为 0；候选池 10 已进入正式配置。标准生产评测也改为使用与 `RagService` 相同的检索器工厂：

```powershell
python -m aa_my_agent.eval.rag.run_eval --skip-sync --index-profile production --report-name after_reranker_production
```

正式开关及回退方法：

```dotenv
RAG_ENABLE_RERANKER=true
RAG_RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RAG_RERANKER_CANDIDATE_K=10
```

模型在同一个 Agent 进程中只加载一次；模型缺失或计算失败时自动返回原 RRF 结果。需要临时关闭精排时，把 `RAG_ENABLE_RERANKER` 设为 `false` 并重启小 A。

## 邻接 Chunk 与引用评测

邻接扩展只读取正式 Chroma，不执行向量查询、不调用外部 Embedding，
也不修改索引。脚本会定位已经人工核对过的第 4 页表 1，确认前后
Chunk、页码、类型和 Chunk ID 可以被稳定补回：

```powershell
python -m aa_my_agent.eval.rag.run_adjacent_context_eval
```

报告保存在 `reports/after_adjacent_context.md/.json`。通用的去重、图片
短子块排除、字符预算和缓存刷新另由 `tests/test_adjacent_context.py`
覆盖。

## 邻接 Chunk 与引用评测

邻接扩展只读取正式 Chroma，不执行向量查询、不调用外部 Embedding，
也不修改索引。脚本会定位已经人工核对过的第 4 页表 1，确认前后
Chunk、页码、类型和 Chunk ID 可以被稳定补回：

```powershell
python -m aa_my_agent.eval.rag.run_adjacent_context_eval
```

报告保存在 `reports/after_adjacent_context.md/.json`。通用的去重、图片
短子块排除、字符预算和缓存刷新另由 `tests/test_adjacent_context.py`
覆盖。
