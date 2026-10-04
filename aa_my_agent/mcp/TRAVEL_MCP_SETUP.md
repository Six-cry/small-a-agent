# 小 A 旅游 MCP 接入说明

公开副本不附带个人配置。先把 `aa_my_agent/mcp/servers.example.json` 复制为
`aa_my_agent/mcp/servers.json`，密钥只放在本机 `aa_my_agent/.env`。模板仅启用
本地 `travel-demo`；下面各服务默认关闭，需要在复制的配置中显式设为
`enabled: true`，然后设置 `MCP_ENABLED=true` 并重启小 A。
启动日志的 `connected` 是已经连上的服务数，
`pending_sources` 列出还缺凭证的可选服务。

| 服务 | 当前能力 | 准备条件 |
| --- | --- | --- |
| `hourly-weather` | 按城市和日期查询最多 7 天逐小时天气、降雨概率、风速 | 无密钥；Open-Meteo 免费接口仅适用于其许可允许的使用场景 |
| `rail-12306` | 给出 12306 官方余票/时刻查询入口及检索条件 | 无密钥；不会读取实时余票、价格或代购 |
| `flyai-travel` | 飞猪航班及国内酒店候选、参考价格与详情链接 | 首次安装项目内 CLI；建议配置 `FLYAI_API_KEY`，无 Key 为受限体验模式 |
| `duffel-travel` | 旧 Duffel 航班查询，当前关闭 | 切回时需 Duffel Token；测试 Token 只返回沙盒数据 |
| `dingtalk-calendar` | 查询、创建、修改、删除指定用户主日历的个人日程 | 钉钉组织内部应用的 Client ID、Client Secret、目标用户 Union ID 和日历读写权限 |

`google-calendar` 的实现保留供以后切回，但当前配置已关闭；不会再出现在
小 A 的可用工具中。

## 飞猪航班和国内酒店（可选）

启用 `flyai-travel` 后，小 A 使用[飞猪 FlyAI 官方 CLI](https://github.com/alibaba-flyai/flyai-skill)
的 `search-flight` 和 `search-hotel`。`duffel-travel` 已在 MCP 配置中关闭；
即使本机 `.env` 留有 Duffel 测试 Token，也不会再把虚拟航司结果当作当前
航班候选。原 Duffel 实现保留供以后手动切回，不会与飞猪重复调用。

首次在仓库根目录安装固定版本的项目内运行环境：

```powershell
npm ci --prefix aa_my_agent/mcp/flyai_cli --no-audit --no-fund
```

Windows PowerShell 如无法执行 `npm`，改用 `npm.cmd`；高德模板中的 `npx`
同样可改成 `npx.cmd`。需要联网安装；不装全局包。安装并启用后重启小 A，启动日志应出现 `flyai-travel`
连接成功。已将 Node 22 固定在该目录，避免这台 Windows 机器上的全局
Node 24 在酒店查询完成后异常退出。可以先不填 `FLYAI_API_KEY`，使用受限
体验模式；如果已有正式 Key，只写入本机 `aa_my_agent/.env` 并重启，
不要把 Key 发到聊天或提交仓库。体验模式可能限制结果或把价格显示成
`¥1xx` 一类模糊值。航班和酒店搜索都只提供有限条候选与飞猪详情链接，
**不保证最终票价、房价、座位、房态或退改政策**；预订前在详情页核实。
航班工具按明确出发地、目的地和出发日期查询，可选返程、直飞和价格上限。
没有下单或付款工具。

可用以下只读请求验证：

> 查一下下周末杭州西湖附近的酒店，只用飞猪酒店工具，给我候选和详情链接，
> 不要下单。

航班验证可问：“只用飞猪查询 2026 年 10 月 15 日北京到上海的直飞航班，
给我航司、起降时间、参考价格和详情链接，不要下单。”

如果安装缺失或服务报错，结果应是明确的查询失败，而不是“没有酒店”。

## 钉钉日历（可选）

这一路径使用钉钉组织的**企业内部应用**，需要有权限创建应用并申请接口权限
的组织管理员或开发者。仅有个人钉钉账号、没有可管理组织时，无法完成此
应用凭证配置。参考[钉钉官方 MCP 日历权限说明](https://github.com/open-dingtalk/dingtalk-mcp)
和[钉钉开放平台](https://open.dingtalk.com/)。

1. 在钉钉开放平台创建企业内部应用。在应用的“凭证与基础信息”里取得
   Client ID（AppKey）和 Client Secret（AppSecret）。
2. 在“权限管理”中申请 `Calendar.Event.Read`、`Calendar.Event.Write`；
   若查询日程视图还要求 `Calendar.EventSchedule.Read`，也在此开通。
   确保应用的可使用范围包含目标用户。
3. 确认你要使用的钉钉用户 **Union ID**。它是接口路径中的用户身份，
   不是邮箱、手机号或 Client ID；可请组织管理员或应用开发者协助查询。
4. 在本机配置中启用 `dingtalk-calendar`，在 `aa_my_agent/.env` 中填写以下项并重启小 A：

   ```env
   MCP_ENABLED=true
   DINGTALK_CLIENT_ID=你的应用Client_ID
   DINGTALK_CLIENT_SECRET=你的应用Client_Secret
   DINGTALK_CALENDAR_UNION_ID=你的钉钉Union_ID
   ```

启动记录中 `pending_sources` 不再包含 `dingtalk-calendar`，且 `connected`
增加，才表示小 A 已发现日历 MCP。先让小 A 查询一个明确的日期范围；
查询成功后再测试创建日程，创建前会显示内容并要求确认。当前桥接只访问
配置用户的 `primary` 主日历。修改和删除必须先查询到准确的日程 ID，
并提供当前标题、开始时间；执行前会再次读取并核对，且弹出人工确认。
为防止误改共享会议或整组重复日程，小 A 拒绝修改/删除重复日程和有其他
参与者的日程；这类日程请在钉钉中手动处理。修改仅支持标题、时间、地点和
备注，不开放邀请参与人或群消息能力。查询不到手动创建的测试日程时，
先核对账号、所属组织和日历，不要直接尝试修改或删除。
应用令牌只在运行进程中缓存；不要将
Client Secret 写入仓库或发送到聊天中。未取得真实应用凭证前，无法完成
钉钉账号端的实测。

## Google 日历（已关闭，保留切回方式）

创建 Google Cloud 项目，启用 Calendar API，建立“桌面应用”类型 OAuth
客户端，将客户端 ID 和 Secret 分别填入本机 `.env`：

```env
GOOGLE_CALENDAR_CLIENT_ID=...
GOOGLE_CALENDAR_CLIENT_SECRET=...
GOOGLE_CALENDAR_REFRESH_TOKEN=
```

然后从仓库根目录运行一次授权助手：

```powershell
python -m aa_my_agent.mcp_servers.google_calendar_auth --authorize
```

授权助手只在你主动运行时打开本地回调并要求浏览器授权。成功后刷新令牌保存在
本机 `.env`；如果日后在 `mcp/servers.json` 中重新启用 `google-calendar`，
重启小 A 后才会发现它的工具。创建事件属于外部写入，
小 A 会在每次执行前询问。项目接的是本地 Google Calendar 桥接服务，不依赖
目前仅开放开发者预览的 Google Workspace 官方远程 MCP。

## 火车

12306 工具只提供[官方余票查询页](https://kyfw.12306.cn/otn/leftTicket/init)
与[时刻查询页](https://kyfw.12306.cn/otn/queryTrainInfo/init)。目前没有找到
可供小 A 正式接入的 12306 开发者 API 文档，因此小 A 不声称已经查到实时
余票，也不会自动登录、抢票或购票。

## 数据来源

- [Open-Meteo 预报接口](https://open-meteo.com/en/docs)及[使用条款](https://open-meteo.com/en/terms)
- [Duffel Flights Offer Requests](https://duffel.com/docs/api/v2/offer-requests)
- [飞猪 FlyAI 酒店搜索参数](https://github.com/alibaba-flyai/flyai-skill/blob/main/skills/flyai/references/search-hotel.md)
- [飞猪 FlyAI 航班搜索参数](https://github.com/alibaba-flyai/flyai-skill/blob/main/skills/flyai/references/search-flight.md)
- [Google Calendar API Events](https://developers.google.com/workspace/calendar/api/v3/reference/events)
