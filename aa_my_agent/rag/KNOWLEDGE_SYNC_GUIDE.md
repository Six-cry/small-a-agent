# 小 A 知识库独立同步使用说明

## 为什么把入库与小 A 分开

PDF 的 Docling 解析、OCR、公式与图片处理、文本切块、Embedding、
Chroma 写入和 BM25 更新可能持续数分钟。现在这些操作只由独立命令
执行，小 A 启动和回答问题时只读取上一次成功构建的索引。

这样可以避免：

- 新 PDF 让小 A 启动长时间停住；
- 解析模型与 Reranker 同时占用大量内存；
- 文档入库失败影响正常问答；
- 查询期间 Chroma 正在被另一个流程修改。

## 日常推荐流程

### 1. 先退出小 A

如果小 A 正在运行，输入 `q` 退出。不要在小 A 查询知识库的同时执行
同步或完整重建。

### 2. 添加、替换或删除知识文件

知识文件目录：

```text
aa_my_agent/rag/data/knowledge/
```

支持 PDF、DOCX 和 TXT。PDF 使用 Docling；DOCX/TXT 使用各自的专用
Loader。

### 3. 只查看待处理变化

```powershell
python -m aa_my_agent.rag.sync_knowledge status
```

`status` 是快速只读操作，相当于 dry-run。它根据文件名、大小和修改
时间列出新增、修改、删除文件；不会读取整份 PDF、解析文件、生成
Embedding、打开写入流程或修改数据库。

### 4. 执行增量同步

```powershell
python -m aa_my_agent.rag.sync_knowledge sync
```

增量同步只处理发生变化的文件：

```text
扫描知识目录
→ 对比 Manifest
→ 解析新增/修改文件
→ 结构化切块
→ 生成图片检索子块
→ 调用 Embedding
→ 写入 Chroma
→ 更新 Manifest
→ 更新 BM25
→ 最终一致性校验
```

PDF 第一次解析可能较慢。终端长时间停留在某一页不一定是卡死；逐页
检查点会被保存，再次运行时可以复用。不要因为暂时没有新输出就直接
删除 Chroma 或 Manifest。

同步可能将文档 Chunk 发送给当前配置的外部 Embedding 服务；启用
图片描述时，PDF 裁图也可能发送给已配置的视觉模型。

### 5. 再次快速检查

```powershell
python -m aa_my_agent.rag.sync_knowledge status
```

确认输出中没有新增、修改或删除文件，并显示索引状态一致。

如果需要计算全部文件的完整 SHA-256，并同时核对 Manifest 与 Chroma，
可以运行深度只读校验：

```powershell
python -m aa_my_agent.rag.sync_knowledge verify
```

`verify` 可能需要较长时间，但同样不会解析 PDF、生成 Embedding 或写库。

### 6. 启动小 A

```powershell
python -m aa_my_agent.main
```

小 A 只读取现有 Chroma 和 BM25。发现文件尚未同步时只会显示提醒，
不会在启动或检索过程中自动解析、切块、向量化或写库。

## 完整重建

```powershell
python -m aa_my_agent.rag.sync_knowledge rebuild
```

`rebuild` 不是日常命令。它只适用于索引签名变化、Manifest 与 Chroma
无法修复地不一致等情况。完整重建会重新处理全部文件并重新生成全部
向量，耗时长且会产生外部模型调用；执行前应先备份
`aa_my_agent/storage/chroma/`、Manifest 和 BM25 索引。

## 常见现象

- 小 A 提示“尚未入库”：退出小 A，运行 `status`，确认后运行 `sync`。
- `status` 显示一致：不需要运行 `sync`。
- 怀疑文件时间戳、Manifest 或 Chroma 异常：运行 `verify` 深度检查。
- 同步失败：先保留终端错误信息；不要手工删除旧库。新增或修改文件
  只有成功写入后才会成为可检索内容。
- 首次问答仍可能等待约 30 秒：这是本地 Reranker 首次加载，与 PDF
  入库已经无关；同一小 A 进程中的后续查询会复用模型。

## 新旧机制对比

| 场景 | 旧机制 | 当前机制 |
|---|---|---|
| 启动小 A | 发现变化后可能立即解析和入库 | 只读检查并提醒 |
| 知识检索 | 可能再次触发自动同步 | 只读检索现有索引 |
| 添加 PDF | 下次启动时自动处理 | 手动运行独立 `sync` |
| 入库失败 | 容易与启动/回答混在一起 | 在独立终端中单独处理 |
| 日常排查 | 难区分解析问题和检索问题 | `status/sync` 与问答链路明确分离 |
