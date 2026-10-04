# RAG 包的统一入口。
#
# 这个包负责完整的知识检索流程：
# 知识文件 -> 文档对象 -> 文本块 -> 向量 -> 相似度检索。
#
# Agent 的循环和工具调度不放在这里。Agent 只需要调用 service.py
# 对外提供的 RagService 接口，不需要了解 RAG 内部的实现细节。
#
# 等功能实现完成后，可以在这里统一导出 RagService，让其他模块使用：
# from aa_my_agent.rag import RagService
