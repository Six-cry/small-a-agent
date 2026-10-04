# 来源与第三方许可证

## Learn Claude Code

小 a 参考 shareAI Lab 的 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 教学实现，并在主模型—工具循环之外增加旅游业务规范、RAG、MCP、记忆、恢复与验证模块。

上游版权声明为 `Copyright (c) 2024 shareAI Lab`。根目录 [LICENSE](LICENSE) 保留完整 MIT 许可文本与版权声明。项目介绍应区分上游参考机制、项目中的扩展实现和个人实际完成的工作。

## Skill Creator

`aa_my_agent/skills/skill-creator/` 保留其独立 Apache License 2.0 文本：[LICENSE.txt](aa_my_agent/skills/skill-creator/LICENSE.txt)。根目录 MIT 说明不覆盖此目录的独立许可证。

## 数据与依赖

发布目录没有附带个人知识文档、模型权重或向量数据库。`examples/knowledge/demo_guide.txt` 是为公开副本编写的虚构示例；`eval/rag/fixtures/scan_01/` 是既有合成测试夹具，不是真实用户扫描件。

Python、Node.js、模型与在线服务依赖各自保持原许可证和使用条件；依赖文件用于安装，不将其安装目录或权重一并复制进仓库。
