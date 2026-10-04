# 小 a：可扩展的 Agent Harness

小 a 是一个 Python 命令行 Agent 学习与实践项目。主循环直接实现模型调用、工具执行与结果回传，并接入旅游规划 Skill、本地 RAG、联网研究、隔离上下文的子 Agent、MCP、长期记忆和运行日志。

项目参考 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)。本仓库提供小 a 的独立公开副本，保留上游 MIT 许可证；第三方 Skill 的许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。当前定位是学习与作品集项目，已知限制见下文。

## 快速开始

建议使用 Python 3.11。请在本 README 所在的仓库根目录运行命令。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item aa_my_agent/.env.example aa_my_agent/.env
```

Linux / macOS 创建和激活环境的命令为：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp aa_my_agent/.env.example aa_my_agent/.env
```

在本机 `aa_my_agent/.env` 中填写 `ANTHROPIC_API_KEY`、`MODEL_ID`；模板的 `ANTHROPIC_BASE_URL` 指向 Anthropic 官方服务，使用兼容服务时改成对应地址，保持该字段非空。聊天客户端采用 Anthropic Messages 协议，不能把仅支持 OpenAI Chat Completions 的地址直接填入。

```powershell
python -m aa_my_agent.main
```

输入问题开始使用，输入 `q` 退出。发布副本没有真实密钥、个人记忆、知识原文和预建向量库；初次启动时知识索引尚未建立的提醒属于预期行为。

## 主要机制

| 模块 | 实现内容 |
|---|---|
| `aa_my_agent/main.py`、`agent.py` | CLI 会话循环、模型—工具循环、每回合状态和指标 |
| `tools/`、`hooks/` | 工具 Schema 与 Handler、权限检查、敏感路径保护、联网熔断 |
| `prompt/`、`skills/` | 按可用工具构建提示词，按需加载任务规范与资源 |
| `context/` | 大输出落盘、历史摘要、失败兜底、工具调用配对保护 |
| `memory/` | 相关记忆选择、跨会话事实提取及 Markdown 存储 |
| `rag/` | 独立文档入库、混合检索、精排、图片父子块、邻接补全和来源标识 |
| `subagents/` | 使用新上下文同步执行子任务，研究预算和报告复用 |
| `mcp/`、`mcp_servers/` | 外部工具发现、命名隔离、权限元数据与本轮只读缓存 |
| `telemetry.py`、`recovery/` | 结构化日志、Token 统计、模型请求有限重试与收尾恢复 |

主循环由模型选择下一步行动；旅游业务规范位于 `skills/travel-planner-v2`。子 Agent 当前同步执行，未实现并行团队调度。Shell 审批不等同于操作系统沙箱。

## 可选能力

基本聊天需要聊天服务配置；联网搜索还需要 `TAVILY_API_KEY`。这些配置只保存在本机 `.env`。

PDF 的 Docling 解析和扫描件支持：

```powershell
python -m pip install -r aa_my_agent/requirements-rag-multimodal.txt
```

本地 BGE Cross-Encoder 精排：

```powershell
python -m pip install -r aa_my_agent/requirements-rag-reranker.txt
```

精排默认开启，缺少依赖或模型加载失败时回退到原融合结果；也可以在 `.env` 中设置 `RAG_ENABLE_RERANKER=false`。依赖文件以已有验证环境的直接依赖版本为基准，PDF/OCR/精排模型会在需要时另行加载。

## 本地知识库

示例材料位于 [examples/knowledge/demo_guide.txt](examples/knowledge/demo_guide.txt)，内容是虚构园区，仅用于演示流程。实际文档请自行放到 `aa_my_agent/rag/data/knowledge/`。

```powershell
New-Item -ItemType Directory -Force aa_my_agent/rag/data/knowledge
Copy-Item examples/knowledge/demo_guide.txt aa_my_agent/rag/data/knowledge/
python -m aa_my_agent.rag.sync_knowledge status
```

填写 `DASHSCOPE_API_KEY` 等 Embedding 配置后，在确认允许将文档文本发送到配置的 Embedding 服务时执行：

```powershell
python -m aa_my_agent.rag.sync_knowledge sync
python -m aa_my_agent.rag.sync_knowledge status
python -m aa_my_agent.main
```

PDF 图片描述可能调用外部视觉服务；按问题查看图片默认禁止外发。详细说明见 [知识库同步指南](aa_my_agent/rag/KNOWLEDGE_SYNC_GUIDE.md)。启动和在线检索不自动解析、向量化新文档。

## MCP

MCP 默认关闭，公开副本不包含个人 `servers.json`。先复制模板：

```powershell
Copy-Item aa_my_agent/mcp/servers.example.json aa_my_agent/mcp/servers.json
```

再在 `.env` 中设置 `MCP_ENABLED=true`。模板默认只启用本地 `travel-demo`，用于验证连接；其他服务按需启用并配置凭证。高德需要 Node.js 和 npm，模板使用 `npx`，Windows 下应改为 `npx.cmd`。FlyAI 还需要在 `aa_my_agent/mcp/flyai_cli/` 执行 `npm ci`。服务说明见 [旅游 MCP 设置](aa_my_agent/mcp/TRAVEL_MCP_SETUP.md)。

## 离线测试与发布检查

```powershell
python -m pip install -r aa_my_agent/requirements-dev.txt
python -m pytest
python publication/check_release.py
```

pytest 使用测试占位配置并阻止外部网络连接，不需要真实 API Key。测试范围包含小 a 的主循环、工具、恢复、权限、上下文、MCP 与 RAG 单元测试；不包含原教程的独立测试。GitHub Actions 在 Windows 和 Linux 的 Python 3.11 上运行同一套检查。

公开打包和本次验证结果见 [publication/UPGRADE_LOG.md](publication/UPGRADE_LOG.md)。该文件区分复用已有依赖的本地测试与干净环境安装验证；后者不能由前者替代。

## 评测与已知限制

[历史检索报告](aa_my_agent/eval/rag/reports/README.md)保留小题库的历史判分表，未包含原始文档、向量库和检索正文。历史 11/11 是指定题库的检索结果，不是最终回答准确率，也不是本次发布重新运行的结果。原题库依赖用户自行提供对应资料，不能直接用演示文本复现。

- RAG 服务默认 Top 5，主 Agent 与子 Agent 工具入口仍默认 Top 3；公开副本保留该已登记问题，未借打包改变检索行为。
- 研究缓存按粗粒度范围判断复用，存在不同研究对象误复用报告的已知问题。
- 当前未启用要求完成全部 Todo 的 Stop Hook，`completed` 不保证所有普通 Todo 均已完成。
- 公式识别、密集拓扑图精确关系与真实扫描件质量需要对照源页面验证。
- 向量库与 Manifest 更新不是跨存储完整事务；异常退出后可能需要一致性检查和修复。
- 长期记忆与摘要可能遗漏或误解信息，应以最新用户确认及原始证据为准。

各子系统保留自己的升级记录；详细历史见 [RAG_UPGRADE_LOG.md](aa_my_agent/rag/RAG_UPGRADE_LOG.md)、[BASELINE.md](aa_my_agent/BASELINE.md) 及模块内的升级日志。

## 上传到 GitHub

把本文件所在的整个目录作为仓库根目录。先运行发布检查，创建一个空的 GitHub 仓库，再使用 GitHub Desktop 或 Git 推送此目录。公开副本不继承原工作目录的 Git 历史；本机配置与后续运行数据由忽略规则排除。

依赖安装、填写密钥和构建个人知识库应在下载后的本机副本中完成。
