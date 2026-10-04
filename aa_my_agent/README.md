# 小a（aa_my_agent）

> 此处保留模块说明。公开副本的安装、可选依赖、测试和当前限制请以[根目录 README](../README.md) 为准。个人 MCP 配置不随发布副本附带。

小a是一个用于学习 Agent Harness 的独立项目。它参考上游
`shareAI-lab/learn-claude-code` 的 `s01_agent_loop` 到 `s11_error_recovery`
等阶段代码，并在其上实验旅游规划、RAG、联网研究和持久化记忆。公开副本不附带上游教程目录。

当前目标不是一次性复制一个生产级 Claude Code，而是保持基础 Agent
循环清晰，在可回归的前提下逐项理解和增加能力。

## 运行

1. 安装依赖：

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. 将 `aa_my_agent/.env.example` 复制为 `aa_my_agent/.env`，填写自己的
   服务地址、模型和 API Key。不要提交 `.env`。

3. 从仓库根目录运行：

   ```powershell
   python -m aa_my_agent.main
   ```

## 两层循环

小a保留了参考 Harness 的两个核心循环：

1. `main.py` 的 CLI 循环持续接收用户输入；
2. `agent.py` 的 Agent 循环持续执行“调用模型 → 执行工具 → 把结果交回
   模型”，直到模型结束本轮。

这两个循环是项目的核心基线。上下文压缩、记忆、RAG、联网和日志都应
作为外围能力接入，不应把旅游业务判断直接写进循环。

## 模块边界

| 目录/文件 | 职责 |
|---|---|
| `main.py` | CLI输入输出，不负责推理策略 |
| `agent.py` | 主模型—工具循环与本轮状态 |
| `tools/` | 工具Schema、处理函数、联网熔断 |
| `mcp/` | 外部MCP连接、工具发现、命名隔离和权限元数据 |
| `mcp_servers/` | 可独立运行的本地MCP测试服务 |
| `hooks/` | 权限、安全检查和工具生命周期日志 |
| `subagents/` | 隔离上下文的研究/工作区子Agent |
| `context/` | L1–L4上下文压缩，不负责长期记忆 |
| `memory/` | 跨会话记忆选择、读取、提取和存储 |
| `prompt/` | 根据可用能力构建系统提示词 |
| `rag/` | 本地知识库构建与检索，不负责最终回答 |
| `skills/` | 可按需加载的任务规范和资源 |
| `telemetry.py` | 结构化运行日志和Token统计 |

外围能力只应通过三个位置影响执行：

- 模型调用前：提示词、相关记忆和上下文压缩；
- 工具执行时：权限、熔断、缓存和日志；
- 回合结束后：状态汇总和长期记忆提取。

## 模型错误恢复

主 Agent 的模型调用已接入 S11 恢复层：429、529、超时、连接错误和常见
临时服务错误会执行有限的指数退避；连续 529 可切换到
`FALLBACK_MODEL_ID`。上下文超限继续复用本项目的 L1–L4 压缩；输出达到
上限时先提高 `max_tokens` 重试原请求，仍然截断才进入无工具有限续写。

恢复层只包围模型请求，不包围工具执行，因此不会因为 API 重试而重复写
文件或重复调用有副作用的工具。相关阈值记录在 `.env.example`；未配置备用
模型时，529 只会在原模型上有限重试。

主 Agent 另有异常循环保护：模型在同一用户回合持续要求调用工具时，达到
`AGENT_MAX_TOOL_ROUNDS`（默认 60）后停止继续执行工具并尝试给出最终说明。
正常结束仍由模型决定，此上限不替代日常的模型—工具循环。

## MCP外部工具

小a保留全部内置工具，并在启动时把已配置MCP服务器发现到的工具加入
工具池。MCP工具统一命名为`mcp__服务器__工具`；与内置工具同名的常见
能力默认隐藏，除非服务器配置显式允许。只有MCP服务器明确标注为只读且
非破坏性的工具才自动放行，其余调用都会请求确认。

MCP默认关闭。将`mcp/servers.example.json`复制为`mcp/servers.json`，再在
`.env`中设置`MCP_ENABLED=true`即可启用本地连接测试。示例服务只验证协议
连接，不提供实时旅游数据；接入地图、日历等真实服务时，应把令牌放在
`.env`中，并在配置里通过`token_env`引用变量名。

MCP工具失败不会触发S11重试，尤其不会自动重复写入、预订或付款操作。

### 高德地图MCP

公开副本的`mcp/servers.example.json`包含高德配置，默认关闭。复制为本机配置并启用后，
按[高德官方说明](https://lbs.amap.com/api/mcp-server/create-project-and-key)
创建“Web服务”Key，然后仅在本机`.env`中填写：

```env
MCP_ENABLED=true
AMAP_MAPS_API_KEY=你的高德Web服务Key
```

启动后，小a会自动发现高德的地点搜索、地理编码、周边搜索、详情、距离、
步行/骑行/驾车/公交路线工具。高德天气工具默认隐藏，继续使用小a原有天气
工具；IP定位也默认隐藏。当前高德npm包没有携带只读标注，因此配置中以
经过审核的`read_only_tools`名单放行这些查询能力，名单以外的能力仍需确认。

### 更多旅游 MCP

公开模板还包括默认关闭的逐小时天气（Open-Meteo）和 12306 官方查询入口。
天气可按城市及日期查询未来短期逐小时预报；12306 工具只提供官方查询页面，
不会返回实时余票或票价。航班和酒店可使用 FlyAI；日历可使用钉钉组织
内部应用的主日历，Duffel 和 Google 日历实现保留供切换使用。
这些服务都需要在本机配置中显式启用，并满足对应的安装和凭证要求。
酒店和航班仅开放查询，创建、修改或删除日历事件会在调用前询问用户。
具体申请、授权和验证方法见
[`mcp/TRAVEL_MCP_SETUP.md`](mcp/TRAVEL_MCP_SETUP.md)。

## 测试

无需真实模型的标准库测试：

```powershell
python -m unittest discover -s aa_my_agent/tests -v
```

安装开发依赖后运行其余 pytest 测试：

```powershell
python -m pip install -r aa_my_agent/requirements-dev.txt
python -m pytest aa_my_agent/test aa_my_agent/tests -q
```

当前能力、已验证项目和已知失败记录在 `BASELINE.md`。修改功能前先运行
测试，修改后使用同一命令回归。

## 开发原则

1. 一次只修改一个能力层，再运行由简到难的测试。
2. 不用提示词掩盖本应由代码保证的安全、权限和数据校验。
3. 不因为一次旅游输出异常就在主循环中增加城市、景点或模型专用分支。
4. 模型输出、网络结果和记忆都可能出错；失败必须可见，但非关键外围能力
   失败不应让主回答崩溃。
5. `storage/`、`.env` 和 `rag/data/knowledge/` 是本地运行数据，不进入Git。
