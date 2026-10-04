# RAG 图片入库结构设计（草案）

> 状态：阶段 A 已接入正式 Loader、Manifest 与 Chroma；图片父子 Chunk 和阶段 B“按用户问题重新读原图”的代码已实现，但现有正式索引迁移及真实外部调用评测尚未完成。

## 目标

让 PDF 中的流程图、架构图、网络拓扑和统计图既能被文本检索找到，又不把视觉模型的猜测当成已经核实的原文事实。

当前实验说明，图片处理需要分成两个能力：

1. **检索召回**：用图注、邻近正文和保守图片摘要找到相关图片；
2. **精确读图**：对检索到的原始裁图，围绕用户的具体问题进行视觉核对。

密集拓扑图不适合在入库时强制生成“完整边表”。本次实验中，通用描述虽然读到了数字，却产生了大量错误的节点—边对应；使用高分辨率裁图并只询问目标关系后才得到正确答案。

## 建议流程

```text
PDF
  -> Docling 按页解析
  -> 正文、表格、公式走各自的文本化流程
  -> 图片单独裁出为 PNG
  -> 生成保守图片摘要
  -> 完整摘要作为 image_summary 父 Chunk
  -> 从图题、章节和检索摘要生成 image_retrieval 短子 Chunk
  -> 检索命中短子 Chunk后按 parent_chunk_id 返回完整父 Chunk
  -> 返回完整上下文、来源页码、图号和裁图路径
  -> 如问题要求精确节点/箭头/数值关系，再调用独立的按问题读图步骤
```

“按问题读图”不应偷偷混入普通向量检索。若以后实现，应作为单独、可观察的视觉核对工具，并在结果中注明模型和未人工核验状态。

## 文件保存建议

裁图不要写入 Chroma，也不要把 Base64 放进 Chunk。建议保存在：

```text
aa_my_agent/rag/data/derived/images/
  <file_hash前12位>/
    page_0004_picture_002_<image_hash前12位>.png
```

这样可以：

- 由文件 Hash 区分不同版本；
- 由页码和图片序号定位原文；
- 由图片 Hash 检测重复图片；
- 删除或更新源 PDF 时精确清理其派生图片。

正式实现前，`rag/data/derived/` 应加入适当的忽略或持久化策略，避免误提交大量派生图片。

## 一个图片对应一个独立 Document

图片摘要不要直接拼进整页正文后再统一切成 500 字符。建议每张图片先生成一个独立的 LangChain `Document`：

```text
[图片摘要]
图号：图 3
图注：强化学习中要素交互关系
图片类型：流程图
可确认内容：SDN控制平面包含“虚拟队列积压”和“历史决策经验”；电力通信网包含“跳数”“丢包率”“转发时延”。
不确定内容：箭头方向尚未经过人工核验。
```

对于很长的图片描述，可以按结构拆成多个 `image_detail` Chunk，但同一条节点—边、框—成员或箭头关系不能被切断。

## 元数据字段

现有必需字段继续保留：

| 字段 | 示例 | 用途 |
|---|---|---|
| `source` | `论文.pdf` | 相对来源路径 |
| `file_name` | `论文.pdf` | 展示文件名 |
| `file_type` | `.pdf` | 文件类型 |
| `file_hash` | SHA-256 | 源文件版本 |
| `file_size` | `123456` | 与现有 Manifest 对齐 |
| `page` | `3` | 保持 LangChain 的零基页码；展示时加 1 |
| `chunk_id` | SHA-256 | 稳定索引 ID |
| `chunk_index` | `18` | 文件内统一序号 |
| `total_chunks` | `42` | 文件 Chunk 总数 |
| `content_hash` | SHA-256 | 文本内容校验 |

图片新增字段建议全部使用 Chroma 可直接保存的字符串、整数、浮点数或布尔值：

| 字段 | 示例 | 用途 |
|---|---|---|
| `content_type` | `image_summary` | 与正文、表格、公式区分 |
| `picture_index` | `2` | 当前页中的图片序号 |
| `figure_label` | `图 2` | 图号 |
| `figure_caption` | `12个节点组成的网络拓扑` | 原始图注 |
| `image_path` | 派生 PNG 相对路径 | 供后续精确读图 |
| `image_hash` | SHA-256 | 图片去重和一致性校验 |
| `image_width` | `1417` | 判断是否需要重新渲染 |
| `image_height` | `1061` | 判断是否需要重新渲染 |
| `image_kind` | `network_topology` | 图片类别 |
| `description_model` | `glm-4v-flash` | 追踪描述来源 |
| `description_prompt_version` | `topology-v2` | 提示词升级后可重建 |
| `quality_status` | `unverified` | `unverified`、`verified` 或 `rejected` |
| `has_uncertainty` | `true` | 是否包含未确认内容 |

边界框可暂存成字符串，如 `"x1,y1,x2,y2"`。不要直接保存列表或字典，避免 Chroma 元数据兼容问题。

## 质量规则

图片摘要写入测试索引前至少满足：

1. 裁图不为空，宽高达到最低阈值；小字密集图应重新高分辨率渲染；
2. 图注和图片必须来自同一页、同一个 Docling Picture；
3. 描述只陈述图中明确可见的内容；看不清时写“不确定”，不能补全；
4. 关系型题目必须检查同一关系单元，不能只检查孤立关键词；
5. `quality_status=rejected` 的描述不得写入可检索正文；
6. 外部视觉 API 失败时保留图注和裁图路径，不让整个 PDF 入库失败；
7. 向外部 API 发送图片属于数据外发，应允许通过配置关闭。

## 推荐的两阶段实现

### 阶段 A：测试索引中的图片摘要

- 保留现有生产 Loader；
- 新建 Docling PDF 实验 Loader；
- 生成独立 `image_summary` Document；
- 只重建测试 Chroma；
- 运行 11 道现有题，要求原 9 道不退化，并让 2 道图片题都通过关系级检查。

### 阶段 B：按问题精确读图

- 仅当检索命中 `image_summary` 且问题涉及节点、箭头、数值或包含关系时触发；
- 把用户当前问题和对应高分辨率裁图一起交给视觉模型；
- 返回“模型回答 + 原图路径 + 来源页码 + 未核验标记”；
- 不把这次临时回答自动回写知识库，避免错误不断累积。

## 当前实现状态

- 阶段 A 已完成：每张有效图片生成独立 `image_summary` Document，并随 PDF 结构化 Chunk 进入正式索引。
- `image_parent_child.py` 已实现 `image_retrieval` 短子块；原 `image_summary` ID 不变，子块通过 `parent_chunk_id` 指向父块，最终检索按父 ID 去重并返回完整父块。
- 离线对现有 655 个 Chunk 转换得到 93 个图片子块；93/93 父链接有效、93/93 子块短于父块，转换后总数应为 748。
- 当前正式 Chroma 和隔离测试 Chroma 均未写入这 93 个向量；真实 Embedding 评测因内部 PDF 派生文本外发尚未得到足够明确的授权而停止。
- `RAG_ENABLE_IMAGE_PARENT_CHILD` 当前为 `False`，因此在隔离评测和正式迁移前，即使新增或修改 PDF 也不会混入新的子块结构。
- 独立 `sync_knowledge sync` 中，新增或修改 PDF 会走 Docling；可通过 `RAG_DESCRIBE_PDF_IMAGES=false` 禁止向外部视觉服务发送图片。小 A 启动和检索不会触发该流程。
- 拓扑图采用高分辨率裁图；通用摘要保持保守，不生成未经验证的完整边表。
- 对精确拓扑关系，优先采用阶段 B 的按问题读图方式。
- 阶段 B 已提供 `inspect_knowledge_image` 工具及路径边界检查，但 `RAG_ALLOW_ON_DEMAND_IMAGE_VERIFY` 默认是 `false`；启用后才会把单张裁图和当前问题发送给 GLM，结果标记为 `machine_unverified` 且不写回知识库。
- 阶段 B 已通过模拟客户端测试，尚未执行真实 GLM 调用，因此不能声称真实图片核验已经通过。
