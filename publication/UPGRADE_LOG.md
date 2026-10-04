# 公开发布副本记录

## 记录 01：创建独立 GitHub 发布目录（2026-10-04）

### 问题与原因

工作目录包含个人配置、知识原文、数据库、生产备份和解析缓存；知识目录忽略规则曾指向旧路径。有效原 Git 仓库位于包目录内，依赖、启动命令、许可证与部分测试却位于外层，直接上传无法独立复现。

### 修改

新建独立仓库布局，复制小 a 源码、相关测试、合成夹具与已有升级记录；排除 `.git` 历史、`.env`、个人 MCP 配置、知识原文、运行数据及 Skill 实验工作区。根目录保留原 MIT 许可并说明 Skill Creator 的独立许可。增加安装说明、离线 pytest 配置、跨平台 CI、虚构示例知识资料和发布检查脚本。历史检索报告只保留元数据及判分表，不携带原文摘录与索引。MCP 模板只默认启用本地演示；聊天模板使用非空的官方服务地址，避免空字符串覆盖客户端默认地址。

### 验证方法

核对复制清单及模块位置；检查文件大小、禁止路径、配置示例、依赖引用、Python 语法和本机凭证是否出现在发布文本；在独立包路径上运行已有离线测试。本机原虚拟环境入口失效，测试使用 Python 官网临时便携解释器与已有 site-packages，不修改全局环境。

### 实际结果

- 发布检查通过：未发现个人配置、知识原文、数据库、大文件或本机凭证值混入；校验了 Python 语法、JSON/TOML 和依赖引用。初次检查约 1.6 MiB，最终文件数及大小见 `release_check.json`。对照本机 11 个不同凭证值，仅记录数量，不输出值。
- 初次 pytest 收集暴露缺少测试 Embedding 地址占位值及便携解释器未加载 pywin32 路径；补齐测试配置与临时解释器路径后，203 项通过、7 项失败。失败均源于原测试依赖个人 MCP 配置或本机 npm 安装目录。
- 配置测试改为检查公开模板；5 项模拟 FlyAI 调用测试改用临时 CLI / Node 文件，不安装 npm、不执行提供商查询，也不削弱错误处理断言。最终完整离线套件：**210 passed, 1 warning，26.33 秒**；警告为既有 `langchain-community` 弃用提示。
- Python 3.11.9 便携解释器复用已有 site-packages。没有在干净环境重新安装依赖；Windows/Linux CI 已配置但尚未远端运行。
- 比对包内 Python 文件，115 个文件与原项目完全一致，只有两份 MCP 测试文件为适配公开副本而变化；生产业务实现未修改。
- 真实聊天、Embedding、OCR、视觉服务和个人知识库检索不属于本次验证范围。历史检索通过率没有被当成本次测试结果。

### 当前状态

仅为独立公开副本的打包与验证；原项目源码和正式知识索引未修改，尚未向远端上传。

### 剩余工作

干净环境依赖安装、Linux 运行和真实服务集成待后续 CI / 使用者环境验证。Top K 默认不一致、研究缓存误复用等功能问题继续保留并明确公开；本次完成公开副本打包，没有修复这些功能问题。

## 记录 02：修复 GitHub Actions 遗漏的结构化文档测试依赖（2026-10-04）

### 问题

用户已将公开副本上传到 Six-cry/small-a-agent，首次 Actions 的 Windows 与 Ubuntu 任务均在收集测试时失败。

### 原因

两个任务的发布输入检查与依赖安装均成功，但 tests/test_structured_chunking.py 导入 structured_document_builder 时缺少 docling_core。开发依赖只声明基础运行依赖和 pytest；本机已有完整 Docling，使旧环境的 210 项测试未暴露这个遗漏。

### 修改

在 aa_my_agent/requirements-dev.txt 中显式固定 docling-core==2.96.0，并说明它只提供结构化文档测试需要的数据类型。保留全部测试与跨平台工作流，不跳过失败测试，不把完整 OCR、Docling 推理或精排权重加入基础安装。README 补充开发依赖与真实 PDF 解析依赖的区别。

### 验证方法

通过 GitHub 连接读取运行 37197651712 的两个任务日志，核对失败步骤与异常。创建临时 Python 3.11.9 隔离解释器，确认 pytest、docling_core、anthropic、chromadb 初始均未安装；仅复用 pip 引导程序，安装声明的开发依赖后执行完整离线测试与发布检查。新环境的解释器路径不包含原 site-packages。

### 实际结果

- 首次远端运行 https://github.com/Six-cry/small-a-agent/actions/runs/37197651712 证实两个平台均因 ModuleNotFoundError: No module named 'docling_core' 中断；Windows 退出码 1，Ubuntu 退出码 2。
- 修复后的开发依赖在临时隔离 Python 3.11.9 环境中从 PyPI 安装成功；pip check 返回 No broken requirements found。
- 确认 sys.path 不包含原 PythonEnvs/main311 的 site-packages；完整 Docling、Torch 和 sentence-transformers 均未安装。只安装 docling-core 类型库即可收集并运行结构化文档测试。
- 隔离环境完整回归：**210 passed, 1 warning，16.67 秒**。警告仍是既有 langchain-community 弃用提示。没有跳过失败测试。
- 发布输入检查通过，结果见 release_check.json。真实模型、Embedding、PDF/OCR 内容质量和原正式知识索引未参与本次验证。

### 当前状态

修复已在本地公开副本中完成，并通过隔离依赖安装与完整离线测试；远端首次运行仍是失败状态，新的远端结果必须在修复提交上传后确认。原项目业务代码及知识索引未改动。

### 剩余工作

用户提交上传修复后核对 Windows/Linux 新一轮 Actions。Linux 本轮修复后尚未实测，不能由本地 Windows 通过推断；真实服务联调和 PDF 内容质量仍不属于本次依赖修复验证。

## 记录 03：修复 Ubuntu 上的敏感路径识别（2026-10-04）

### 问题

修复依赖后的提交 cbb051a 已上传，第二轮 Actions 的 Windows 通过，Ubuntu 仍失败。

### 原因

Ubuntu 完整执行了 210 项测试，其中 209 项通过，唯一失败是 `aa_my_agent\.env` 的敏感路径识别。宿主系统的 Path 在 POSIX 上没有把反斜杠当作目录分隔符；这是工具策略的跨平台兼容问题，与上传或依赖安装无关。

### 修改

公开副本的敏感路径判断改为统一分隔符后使用 PurePosixPath；增加三组路径输入回归，并确认敏感 Shell 命令不会调用进程执行器。工具子系统记录详见 aa_my_agent/tools/UPGRADE_LOG.md 记录 03，保留原跨平台工作流和全部既有测试。

### 验证方法

读取 https://github.com/Six-cry/small-a-agent/actions/runs/37198926576 的 Windows/Ubuntu 日志，复现原有 POSIX 词法问题。用已隔离安装开发依赖的 Python 3.11.9 环境执行全部离线测试与发布输入检查。

### 实际结果

第二轮远端 Windows 为 210 passed、1 warning，6.08 秒；Ubuntu 为 209 passed、1 failed、1 warning，6.43 秒。修复前本地 POSIX 词法复现确认四类嵌套敏感路径漏判；修复后敏感路径拒绝、模板仍允许。新测试的导入位置错误已在本地纠正，最终 Windows 完整回归 **213 passed, 1 warning，10.16 秒**。本机没有 WSL Linux 发行版，未安装额外系统；本地结果不冒充远端 Linux 通过。发布输入检查结果见 release_check.json。

### 当前状态

代码已成功公开上传；自动测试用于验证提交，不是上传审核。此次兼容修复在公开副本中完成，待同步到本地 GitHub 仓库后由用户提交上传并核对新 CI。原工作目录的业务代码与正式知识索引未改变。

### 剩余工作

确认新一轮 Windows 和 Ubuntu 均通过；真实服务、PDF/OCR 内容质量和检索回答准确性仍需单独验证。
