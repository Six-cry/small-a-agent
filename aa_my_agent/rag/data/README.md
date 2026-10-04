# 数据目录

`knowledge/` 用来存放需要进入 RAG 知识库的原始文件，例如 PDF 和 UTF-8 编码的 TXT。

这里保存的是知识源，不保存 Chroma 生成的向量文件。向量数据统一写入 `storage/chroma/`。

后续构建知识库时，文档加载器会遍历 `knowledge/`，忽略不支持的文件类型，并把文件路径、页码等信息写入文档元数据，方便 Agent 在回答中说明资料来源。

不要把 API Key、密码、`.env` 或其他敏感文件放进 `knowledge/`。
