# 存储目录

`chroma/` 用来保存 Chroma 向量数据库。这些文件由程序生成，不是手写源代码。

独立同步命令会读取 `rag/data/knowledge/`、切分文档、生成 Embedding，并把结果持久化到这里。小 A 启动与问答阶段只读取已有数据库，不负责构建或更新索引。

不要手动修改 `chroma/` 内部文件。日常更新使用 `python -m aa_my_agent.rag.sync_knowledge sync`；只有索引损坏等特殊情况才使用 `rebuild`。

`chroma/` 当前保持为空是刻意的：如果在里面放入 README 或占位文件，而初始化代码只判断“目录是否非空”，可能会把它误认为已经存在有效数据库。
