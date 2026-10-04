---
name: rag-debug
description: 排查本地 RAG 的文件扫描、Manifest、索引同步和检索质量问题
---

# RAG Debug

使用本 Skill 排查本地知识库问题时，遵循下面的顺序。

## 1. 确认问题类型

先判断属于哪一种问题：

- 知识文件没有被扫描；
- Manifest 没有更新；
- 文件变化没有进入差异结果；
- Chunk 没有进入向量数据库；
- 向量数据库存在，但查询没有返回结果；
- 返回结果与问题无关；
- 引用来源或页码错误。

## 2. 检查离线入库链路

按顺序检查：

1. `rag/source_scanner.py`
2. `rag/manifest.py`
3. `rag/index_diff.py`
4. `rag/document_loader.py`
5. `rag/text_splitter.py`
6. `rag/index_manager.py`
7. `rag/vector_store.py`

不要跳过前面的步骤直接判断是向量模型问题。

## 3. 检查在线检索链路

按顺序检查：

1. 用户查询是否合法；
2. `top_k` 是否合理；
3. 查询是否生成 Embedding；
4. Chroma 是否返回候选 Chunk；
5. 距离是否超过 `RAG_MAX_DISTANCE`；
6. 检索结果是否保留来源与页码；
7. `service.py` 是否正确格式化结果。

## 4. 输出要求

最终报告必须包含：

- 问题表现；
- 根因位置；
- 相关文件；
- 判断依据；
- 建议修改；
- 建议添加的测试。

不能在没有证据时直接归因于 Embedding 模型。