# 海马体（Hippocampus）— 项目需求文档（PRD）

> 版本：v2.0 | 日期：2026-04-18 | 作者：小虾 | 状态：评审中

---

## 一、项目目的

为AI Agent提供**本地优先、隐私第一**的持久化记忆引擎。让任何Agent框架通过标准协议（MCP/REST/CLI）即插即用地获得跨会话记忆能力，数据永远不离开用户的机器。

### 核心命题

> 现有AI Agent每次对话都是"失忆"的，记忆要么不存在，要么存在云端别人的服务器上。海马体让Agent像人一样记住重要的事，同时保证记忆完全属于你。

---

## 二、解决痛点

| # | 痛点 | 现状 | 海马体方案 |
|---|------|------|-----------|
| 1 | **Agent失忆** | 每次对话从零开始，用户反复重复偏好/上下文 | 自动提取、持久化、跨会话注入记忆 |
| 2 | **隐私泄露** | Mem0等方案数据上云，企业/个人敏感信息外泄 | 纯本地存储，零外部API调用（默认） |
| 3 | **部署复杂** | 竞品需3+容器、Neo4j、外部embedding API | `pip install hippocampus` 一行搞定 |
| 4 | **记忆割裂** | 多Agent各自为政，无法共享上下文 | GitHub-style记忆仓库，权限隔离+受控共享 |
| 5 | **记忆膨胀** | 只存不管，记忆越来越多越来越慢 | 热冷分层+自动遗忘+整合，模拟人脑记忆机制 |
| 6 | **集成成本高** | 每个框架对接方式不同 | MCP + REST + CLI三协议，5分钟集成 |

---

## 三、目标用户群体

| 画像 | 规模 | 使用场景 | 付费意愿 |
|------|------|---------|---------|
| **独立开发者/Hacker** | 数十万 | 个人AI助手、Side Project | 低（用免费版） |
| **AI Agent框架作者** | 数千 | 为框架集成记忆能力 | 中（愿意赞助/Pro） |
| **企业AI团队** | 数千家 | 内网Agent部署，数据合规要求 | 高（Enterprise） |
| **多Agent玩家** | 数万 | Claude Code/Cursor/Hermes等多Agent协作 | 中 |
| 知识工作者 | — | Obsidian/笔记系统+AI联动 | 低 |

---

## 四、开源形态

| 项目 | 说明 |
|------|------|
| **License** | Apache 2.0 |
| **仓库** | `github.com/hippocampus-ai/hippocampus`（待注册） |
| **模式** | Open-Core：核心引擎完全开源，高级功能付费 |
| **语言** | Python 3.10+ |
| **包管理** | PyPI (`pip install hippocampus`) + Docker |

| 开源（Community） | 付费（Pro/Enterprise） |
|-------------------|----------------------|
| 完整记忆引擎 | 跨设备E2E加密同步 |
| MCP + REST + CLI | Web Dashboard |
| 热冷分层 + FTS5 + 向量搜索 | 记忆分析报告 |
| 单机多Agent隔离共享 | 团队共享（RBAC）/ SSO / 审计 |

---

## 五、部署形式

```bash
# 方式1: pip
pip install hippocampus && hippocampus serve

# 方式2: Docker
docker run -d -p 8200:8200 -v ~/.hippocampus:/data hippocampus/hippocampus:latest

# 方式3: 嵌入式
from hippocampus import MemoryEngine
engine = MemoryEngine(db_path="~/.hippocampus/memory.db")

# 方式4: MCP接入
# config.yaml → mcp_servers.hippocampus.command: hippocampus mcp
```

---

## 六、功能清单

> **统一格式**：每个功能项包含 ① 需求描述 ② 解决的问题 ③ 操作步骤 ④ 输入/输出参数 ⑤ 技术方案 ⑥ 验收标准 ⑦ 验证手段

---

### 6.1 核心记忆操作

#### F1: 记忆写入（Add/Update）

**需求描述**
将结构化或自然语言记忆写入存储，支持自动去重和合并。单条写入和批量写入（最多100条/次）。

**解决的问题**
Agent无法持久化对话中获得的知识，每次对话从零开始。

**操作步骤**
1. Agent调用 `POST /v1/memories`，传入content和可选元数据
2. 引擎计算content的embedding向量
3. 与已有记忆做余弦相似度比对，>0.92则合并而非新建
4. 写入SQLite memories表 + FTS5索引 + 向量索引
5. 返回记忆ID和写入状态（created/merged）

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| content | str | ✅ | 记忆内容 |
| agent_id | str | ✅ | 写入Agent标识 |
| scope | enum | ❌ | user/agent/session/shared，默认agent |
| tags | list[str] | ❌ | 标签列表 |
| metadata | dict | ❌ | 自定义元数据 |
| ttl | int | ❌ | 过期秒数，0=永不过期 |
| batch | list | ❌ | 批量写入时传数组，每项含content+可选字段 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| id | str | 记忆ID |
| status | str | created / merged / updated |
| merged_with | str? | 如果合并，原记忆ID |

**技术方案**
- 存储：SQLite `memories` 表，含embedding BLOB字段
- 去重：sqlite-vec计算余弦相似度，阈值0.92
- 索引：同步写入FTS5全文索引 + vec0向量索引
- 批量：SQLite事务包裹，失败回滚

**验收标准**
1. 写入后立即可通过search检索到
2. 重复内容（余弦>0.92）自动合并，不产生冗余
3. 写入延迟<50ms（不含embedding计算）
4. 批量100条写入<2秒
5. 写入自动触发PII检测（F20）

**验证手段**
- 单元测试：写入→检索round-trip
- 性能测试：批量写入100条计时
- 去重测试：写入近义句验证合并行为
- 集成测试：通过MCP/REST/CLI三种协议写入后交叉检索


### 6.2 协议与接入

#### F6: REST API

**需求描述**
标准RESTful HTTP API，FastAPI实现，OpenAPI 3.0自动文档。所有功能的统一HTTP入口。

**解决的问题**
Agent框架需要通过HTTP调用记忆服务，需要标准化、有文档、易集成的API。

**操作步骤**
1. `hippocampus serve` 启动FastAPI服务（默认localhost:8200）
2. Agent通过Bearer Token认证（本地模式可关闭）
3. 调用 `/v1/*` 端点操作记忆
4. 访问 `/docs` 查看Swagger UI交互文档

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| host | str | ❌ | 监听地址，默认127.0.0.1 |
| port | int | ❌ | 端口，默认8200 |
| auth | bool | ❌ | 是否启用认证，默认true |
| cors_origins | list[str] | ❌ | CORS白名单 |

**输出参数**
所有API统一响应格式：`{data: T, error: str?, meta: {request_id, timestamp}}`

**技术方案**
- 框架：FastAPI + Uvicorn（ASGI）
- 认证：Bearer Token → SHA-256哈希比对agents表
- 序列化：Pydantic v2 模型
- 文档：自动生成OpenAPI 3.0 JSON

**验收标准**
1. 所有F1-F27功能通过REST可调用
2. 响应格式统一 `{data, error, meta}`
3. 错误码遵循HTTP标准（400/401/403/404/500）
4. Swagger UI可用且参数完整
5. 支持CORS配置

**验证手段**
- curl测试：每个端点手动curl验证
- OpenAPI校验：导出spec用swagger-cli validate
- 错误码测试：无Token→401，无权限→403，不存在→404
- 压测：wrk 100并发 search 端点，验证P99<500ms

---

#### F7: MCP协议

**需求描述**
Model Context Protocol Server，供Claude Code/Hermes/Cursor等MCP客户端直接调用。

**解决的问题**
MCP是AI Agent生态标准协议，不支持MCP就无法被主流Agent框架即插即用。

**操作步骤**
1. 用户在Agent配置文件中添加 `hippocampus mcp` 作为MCP server
2. Agent框架自动发现tools列表
3. Agent通过tool_call调用记忆操作
4. 海马体通过stdio/SSE返回结果

**输入参数**
MCP Tools列表：

| Tool名 | 对应API | 说明 |
|--------|---------|------|
| memory_add | POST /v1/memories | 写入记忆 |
| memory_search | POST /v1/memories/search | 检索记忆 |
| memory_delete | DELETE /v1/memories/{id} | 删除记忆 |
| memory_stats | GET /v1/memories/stats | 统计信息 |
| memory_consolidate | POST /v1/consolidate | 触发整合 |
| memory_inject | POST /v1/inject | 上下文注入 |

**输出参数**
MCP标准content block：`{type: "text", text: JSON.stringify(result)}`

**技术方案**
- SDK：`mcp` Python SDK
- 传输：stdio（默认）/ SSE（可配置）
- Tool参数：与REST API完全一致，复用Pydantic模型

**验收标准**
1. Claude Desktop配置后可直接调用所有tools
2. Hermes Agent配置后可直接调用
3. Tool参数与REST API一致
4. 返回格式符合MCP标准

**验证手段**
- Claude Desktop集成测试：配置→调用memory_add→memory_search验证
- Hermes集成测试：配置config.yaml→验证tool发现+调用
- 协议测试：用MCP Inspector验证消息格式

---

#### F8: CLI工具

**需求描述**
命令行界面，用于服务管理、记忆操作和调试。支持所有核心功能的命令行操作。

**解决的问题**
开发者需要快速调试和管理记忆，不想每次都写代码或curl。

**操作步骤**
1. `hippocampus init` — 初始化数据目录和配置
2. `hippocampus serve` — 启动HTTP+MCP服务
3. `hippocampus add "记忆内容"` — 写入记忆
4. `hippocampus search "查询词"` — 检索记忆
5. `hippocampus stats` — 查看统计
6. 所有命令支持 `--help` 和 `--json` 输出

**输入参数**

| 命令 | 说明 |
|------|------|
| init | 初始化 ~/.hippocampus/ |
| serve | 启动服务 (--host, --port, --no-auth) |
| add | 写入记忆 (--agent, --scope, --tags) |
| search | 检索 (--agent, --limit, --mode) |
| forget | 遗忘 (--strategy, --dry-run) |
| stats | 统计信息 |
| consolidate | 手动整合 (--dry-run) |
| backup/restore | 备份恢复 |
| import/export | 导入导出 |

**输出参数**
默认人可读格式，`--json` 输出JSON（便于管道处理）

**技术方案**
- 框架：Typer (Click底层)
- 输出：Rich 美化表格/进度条
- 调用：直接调用MemoryEngine（非HTTP），零网络开销

**验收标准**
1. 所有命令支持 `--help`
2. `--json` 输出可被 jq 解析
3. 退出码遵循Unix惯例（0成功，1错误，2参数错误）
4. Tab补全支持（通过Typer自动生成）
5. 无服务运行时CLI仍可用（直接操作本地DB）

**验证手段**
- 每个子命令 `--help` 输出验证
- 管道测试：`hippocampus search "x" --json | jq '.results[0].content'`
- 离线测试：不启动serve，直接CLI操作验证
- 退出码测试：错误输入验证退出码=2

---

### 6.3 记忆生命周期管理

#### F9: 自动温度调控

**需求描述**
基于访问模式自动调整记忆温度，无需人工干预。高频访问自动升Hot，长期不用自动降Cold。

**解决的问题**
手动管理记忆温度不现实，需要像操作系统内存管理一样自动化。

**操作步骤**
1. 定时任务每小时扫描一次所有记忆
2. 升温：Warm→Hot 当 `access_count≥5 AND last_accessed在7天内`
3. 降温：Hot→Warm 当 `last_accessed>7天`；Warm→Cold 当 `last_accessed>30天 AND access_count<3`
4. 遗忘评分：`score = 0.4×recency + 0.35×relevance + 0.25×frequency`，score<0.1且非pinned→标记待清除
5. 手动触发：`POST /v1/maintenance/sweep`
6. 所有变更写入consolidation_log

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| dry_run | bool | ❌ | 预览不执行 |
| threshold | float | ❌ | 遗忘阈值，默认0.1 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| promoted | int | 升温条数 |
| demoted | int | 降温条数 |
| marked_forget | int | 标记待清除条数 |
| skipped_pinned | int | 跳过的pinned条数 |

**技术方案**
- 定时：APScheduler IntervalTrigger(hours=1)
- 评分公式：`recency(t) = exp(-0.05×天数)`，`frequency(n) = min(n/20, 1.0)`
- 配置化：α/β/γ权重和时间窗口可在config.yaml自定义

**验收标准**
1. 定时任务每小时自动执行
2. 升降温有完整日志记录
3. pinned记忆永不降温/遗忘
4. 用户可自定义评分参数
5. sweep单次执行<10秒（1万条规模）

**验证手段**
- 模拟测试：插入不同access_count/last_accessed的记忆，验证升降温结果
- pinned测试：pinned记忆在任何条件下不变
- 配置测试：修改α/β/γ后验证评分变化
- 性能测试：1万条规模sweep计时

---

#### F10: 记忆版本与冲突处理

**需求描述**
记忆更新时保留历史版本，矛盾记忆自动检测并标记。支持版本回滚。

**解决的问题**
记忆被整合/编辑/自动合并后可能出错，没有版本历史就无法回退。多Agent写入可能产生矛盾。

**操作步骤**
1. 每次UPDATE操作自动将旧版本写入memory_versions表
2. 矛盾检测：同scope+同主题的记忆，语义相似但意图相反→标记conflict
3. 查看历史：`GET /v1/memories/{id}/history`
4. 回滚：`POST /v1/memories/{id}/rollback` 指定版本号
5. 冲突列表：`POST /v1/memories/conflicts`

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | str | ✅ | 记忆ID |
| version | int | ✅(回滚) | 目标版本号 |
| limit | int | ❌ | 历史条数，默认10 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| versions | list | [{version, content, changed_by, change_type, created_at, diff_summary}] |
| conflicts | list | [{memory_a_id, memory_b_id, type, resolved}] |

**技术方案**
- 版本表：`memory_versions(id, memory_id, version, content, changed_by, change_type, created_at)`
- 矛盾检测：关键词对立模式匹配 + 余弦相似度>0.8但情感相反
- 回滚：读取目标版本content→UPDATE memories→创建新版本记录（change_type=rollback）
- 上限：默认保留50版本/条，超出清理最早的

**验收标准**
1. 每次内容变更自动记录版本
2. 版本保留上限可配置
3. 回滚后版本号递增（不覆盖历史）
4. conflicts接口返回未解决冲突列表
5. diff_summary为人可读的变更摘要

**验证手段**
- 版本测试：更新记忆3次→查history验证3个版本
- 回滚测试：回滚到v1→验证content恢复+版本号递增
- 冲突测试：写入矛盾记忆对→验证conflict被检测
- 上限测试：写入超出上限的版本→验证最早版本被清理


### 6.4 隔离与共享

#### F11: 三级隔离架构

**需求描述**
Tenant→Agent→Session三级隔离，借鉴GitHub的Organization→User→Branch模型。物理隔离（独立DB）+ 逻辑隔离（Agent/Session维度）。

**解决的问题**
多Agent共用记忆空间导致数据污染、权限混乱、隐私泄露。

**操作步骤**
1. Tenant级：每个海马体实例=一个租户，独立DB文件（物理隔离）
2. Agent级：每个Agent注册后获得唯一ID+Token，拥有私有记忆空间
3. Session级：单次对话创建临时记忆空间，结束时决定保留/丢弃
4. 注册Agent：`POST /v1/agents`
5. 创建记忆库：`POST /v1/repos`（private/public）

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | str | ✅ | Agent名称 |
| repo_name | str | ✅ | 记忆库名称 |
| visibility | enum | ❌ | public/private，默认private |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| agent_id | str | Agent唯一标识 |
| token | str | PAT令牌（仅创建时返回明文） |
| repo_id | str | 记忆库ID |

**技术方案**
- Agent表：`agents(id, name, token_hash, created_at)`
- Repo表：`repos(id, name, owner_agent_id, visibility, created_at)`
- Token格式：`hpc_xxxxxxxxxxxx`，SHA-256哈希存储
- 多Token支持：一个Agent可生成多个Token（只读Token、全权Token）

**验收标准**
1. 不同Agent的private记忆互不可见
2. 无Token请求返回401
3. public记忆库同租户Agent可读
4. 支持100+个Agent同时注册
5. Token吊销后立即生效（<1秒）

**验证手段**
- 隔离测试：Agent-A写入→Agent-B搜索→验证不可见
- 认证测试：无Token/错误Token→401
- public测试：创建public repo→其他Agent可读
- 并发测试：注册100个Agent验证性能

---

#### F12: 记忆库权限体系

**需求描述**
精细权限控制：Token Scope限制操作类型，记忆库级别授权控制访问范围。

**解决的问题**
粗粒度权限（全有或全无）无法满足安全需求，需要最小权限原则。

**操作步骤**
1. 创建Agent时生成默认全权Token
2. 可额外生成限定scope的Token：`POST /v1/agents/{id}/tokens`
3. 授权Agent访问记忆库：`POST /v1/repos/{id}/grant`
4. 撤销授权：`DELETE /v1/repos/{id}/grant/{agent_id}`
5. 查看授权列表：`GET /v1/repos/{id}/grants`

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| agent_id | str | ✅ | 目标Agent |
| permission | enum | ✅ | read/write/admin |
| scope | list[str] | ❌ | Token scope: memory:read, memory:write, repo:admin, consolidate |
| tags | list[str] | ❌ | tag级共享过滤（只共享指定tag的记忆） |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| grant_id | str | 授权记录ID |
| token | str | 新Token（仅生成时返回） |
| grants | list | [{agent_id, permission, tags, created_at}] |

**技术方案**
- 授权表：`repo_grants(repo_id, agent_id, permission, tags)`
- 权限检查链：Token验证→Agent识别→Repo权限查询→Scope过滤
- 无权限访问private repo返回404（不暴露存在性）

**验收标准**
1. Token scope不足返回403
2. 无权限访问private repo返回404
3. public repo未授权写入返回403
4. tag级共享过滤准确率100%
5. 授权后立即生效（<1秒）

**验证手段**
- scope测试：只读Token写入→403
- 404测试：无权限访问private repo→404（非403）
- tag测试：tag级授权→只能检索到指定tag的记忆
- 即时性测试：授权→立即搜索→验证可见

---

#### F13: Session级记忆隔离

**需求描述**
会话级临时记忆空间，会话内可见，结束时由Agent决定保留/丢弃。

**解决的问题**
对话中的临时推理结果不应污染持久记忆，但对话内需要可见。

**操作步骤**
1. 开始会话：`POST /v1/sessions`
2. 会话内写入的记忆自动标记session scope
3. 检索时session记忆对其他会话不可见
4. 结束会话：`DELETE /v1/sessions/{id}`
5. 选择提升策略：all（全保留）/none（全丢弃）/auto（模型判断）

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| agent_id | str | ✅ | Agent标识 |
| session_id | str | ❌ | 自定义session ID，默认自动生成 |
| promote_strategy | enum | ❌ | all/none/auto，默认none |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| session_id | str | 会话ID |
| promoted_count | int | 提升为持久记忆的条数 |
| discarded_count | int | 丢弃的条数 |

**技术方案**
- 关联表：`session_memories(session_id, memory_id)`
- 检索过滤：自动加WHERE排除其他session的记忆
- auto策略：规则层判断——access_count>0或content长度>50字符→保留
- 清理：会话结束24h后自动清除临时记忆

**验收标准**
1. 会话记忆对其他会话不可见
2. promote=auto时保留率在30-70%之间
3. 会话结束后临时记忆24h内自动清除
4. session_id可自定义（便于追踪）

**验证手段**
- 隔离测试：Session-A写入→Session-B搜索→不可见
- promote测试：插入多条→promote=auto→验证保留比例
- 清理测试：结束会话→24h后验证记忆已删除
- 自定义ID测试：传入自定义session_id→验证可用

---

#### F14: 共享记忆库（Shared Repos）

**需求描述**
Agent创建共享记忆库，邀请其他Agent加入，实现跨Agent知识传递。支持repo级和tag级共享粒度。

**解决的问题**
多Agent协作时无法共享上下文，每个Agent是信息孤岛。

**操作步骤**
1. 主Agent创建共享repo：`POST /v1/repos`（visibility=public或授权private）
2. 邀请Agent：`POST /v1/repos/{id}/grant`
3. 被授权Agent检索时自动搜索共享repo
4. 查看共享动态：`GET /v1/repos/{id}/feed`

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| repo_id | str | ✅ | 记忆库ID |
| agent_id | str | ✅ | 被邀请Agent |
| permission | enum | ✅ | read/write/admin |
| tags | list[str] | ❌ | tag级共享过滤 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| feed | list | [{memory_id, content, agent_id, action, timestamp}] |

**技术方案**
- 检索扩展：search时自动UNION查询agent私有repo + 所有被授权repo
- 写入冲突：last-write-wins + 自动版本记录（F10），不做分布式锁
- Feed：按时间倒序返回共享repo的记忆变更流

**验收标准**
1. 授权后Agent立即可检索共享记忆（<1秒）
2. tag级共享过滤准确率100%
3. 共享记忆修改对所有被授权Agent实时可见
4. feed接口支持分页

**验证手段**
- 共享测试：Agent-A写入shared repo→Agent-B搜索→命中
- tag测试：tag过滤后只返回指定tag记忆
- 实时性测试：写入→立即搜索→验证可见
- feed测试：多次变更后验证feed完整性和排序

---

#### F15: 记忆广播（Memory Broadcast）

**需求描述**
主Agent向子Agent单向推送关键上下文。urgent广播自动注入搜索结果，normal广播需Agent主动拉取。

**解决的问题**
共享库是"拉"模式，广播是"推"模式。用户偏好变更等关键信息需要主动推送而非等子Agent碰巧检索到。

**操作步骤**
1. 主Agent调用 `POST /v1/broadcast`
2. 内容写入各目标Agent的inbox队列表
3. urgent广播：子Agent下次search时自动出现在结果最前
4. normal广播：子Agent调用 `GET /v1/inbox` 获取

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| content | str | ✅ | 广播内容 |
| from_agent | str | ✅ | 发送Agent |
| to_agents | list[str] | ❌ | 目标Agent列表，空=全部 |
| priority | enum | ❌ | normal/urgent，默认normal |
| ttl | int | ❌ | 过期秒数 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| broadcast_id | str | 广播ID |
| delivered_to | int | 投递Agent数 |

**技术方案**
- 收件表：`inbox(id, agent_id, content, from_agent, priority, read, ttl, created_at)`
- urgent注入：search时自动JOIN inbox WHERE priority=urgent AND read=false
- 已读追踪：search注入后标记read=true

**验收标准**
1. urgent广播在下次search时自动出现在结果最前
2. 广播有已读状态追踪
3. TTL到期自动清除
4. 单次广播支持最多1000个目标Agent

**验证手段**
- urgent测试：广播urgent→子Agent search→验证出现在结果首位
- 已读测试：search后再search→验证不重复出现
- TTL测试：设置短TTL→过期后验证已清除
- 批量测试：向100个Agent广播验证性能

---

#### F16: 共享审计日志

**需求描述**
记录所有共享相关操作，便于安全审计和问题追踪。

**解决的问题**
共享操作无追踪，出了安全问题无法溯源。

**操作步骤**
1. 系统自动记录每次grant/revoke/broadcast操作
2. 可选记录跨Agent读取操作
3. 查询审计日志：`GET /v1/audit/sharing`

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| agent_id | str | ❌ | 按Agent过滤 |
| repo_id | str | ❌ | 按记忆库过滤 |
| action | enum | ❌ | grant/revoke/read/write/broadcast |
| since | datetime | ❌ | 起始时间 |
| limit | int | ❌ | 返回条数，默认50 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| entries | list | [{timestamp, actor_agent_id, target_agent_id, repo_id, action, detail}] |
| total_count | int | 总条数 |

**技术方案**
- 审计表：`sharing_audit(id, timestamp, actor_agent_id, target_agent_id, repo_id, action, detail)`
- 写入：所有共享操作的API handler中自动插入审计记录
- 保留：默认90天（可配置），定时清理过期日志

**验收标准**
1. 每次grant/revoke/broadcast自动记录
2. 日志保留90天（可配置）
3. 支持按Agent/Repo/Action维度过滤
4. 审计不影响主操作性能（异步写入）

**验证手段**
- 自动记录测试：执行grant→查audit→验证记录存在
- 过滤测试：按agent_id/repo_id过滤→验证准确
- 保留测试：超过90天的日志被清理
- 性能测试：审计写入不增加主操作延迟


### 6.5 安全与智能

#### F17: 敏感信息识别（PII Detection）

**需求描述**
写入记忆时自动检测敏感信息（API Key、邮箱、手机号、身份证号等），标记或脱敏，防止跨Agent共享时泄露。

**解决的问题**
Agent可能无意中将用户敏感信息写入共享记忆库，造成隐私泄露。

**操作步骤**
1. 记忆写入时自动触发PII检测管道
2. 正则引擎匹配已知PII模式
3. 检测到PII后按策略处理：仅标记/脱敏/拒绝写入
4. 手动扫描存量：`POST /v1/memories/scan`
5. 存量扫描生成报告

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| content | str | ✅ | 待检测文本（自动触发时为记忆content） |
| action | enum | ❌ | tag_only/redact/reject，默认tag_only |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| has_pii | bool | 是否含敏感信息 |
| detections | list | [{type, value, position, pattern}] |
| redacted_content | str? | 脱敏后的内容（action=redact时） |

**技术方案**
- 纯规则引擎，零模型依赖
- 内置模式：邮箱、手机号（中国/国际）、身份证、信用卡、API Key（aws_/sk-/hpc_等）、密码字段
- 关键词黑名单：password、secret、token、private_key
- 配置化：`pii_patterns` 列表可扩展自定义正则

**验收标准**
1. 标准格式PII识别率≥95%
2. 误报率<5%
3. 检测延迟<5ms
4. 共享接口自动拦截含PII的private记忆
5. scan接口可扫描存量并生成报告

**验证手段**
- 覆盖测试：每种PII类型各5个样例→验证检测率
- 误报测试：正常文本100条→统计误报率
- 延迟测试：1000条文本检测计时
- 共享拦截测试：含PII的记忆尝试共享→验证被拦截

---

#### F18: 自动记忆提取（Auto Memory Extraction）

**需求描述**
从Agent对话流中自动提取值得记住的内容，无需Agent显式调用add。核心UX——Agent不需要"知道"自己在存记忆。

**解决的问题**
依赖Agent主动调用add太被动，大量有价值信息在对话中流失。

**操作步骤**
1. Agent将对话片段发送到 `POST /v1/extract`
2. 规则层：模式匹配提取用户偏好/事实/环境信息
3. 模型层（可选）：调用外部LLM做摘要提取
4. 返回提取结果+置信度
5. 高置信度（>0.8）可配置自动写入

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| messages | list | ✅ | [{role, content}] 对话片段 |
| agent_id | str | ✅ | Agent标识 |
| mode | enum | ❌ | rules/model/hybrid，默认rules |
| auto_commit | bool | ❌ | 高置信度自动写入，默认false |
| threshold | float | ❌ | 自动写入阈值，默认0.8 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| extracted | list | [{content, confidence, source_turn, suggested_scope, suggested_tags}] |
| committed | int | 自动写入的条数 |

**技术方案**
- 规则层模式：
  - 用户纠正："我喜欢X不喜欢Y"/"不要做X要做Y"
  - 用户自述："我是/我在/我的/我叫"
  - 环境信息："OS is/Python version/running on"
  - 重复实体：同一实体出现3+次→提取
- 模型层：外部LLM prompt提取隐含偏好
- 去重：提取结果与已有记忆余弦>0.92→跳过
- 异步：提取不阻塞对话流

**验收标准**
1. 规则层可独立运行（零API费用）
2. 提取准确率≥70%（规则层）/≥90%（模型层）
3. auto_commit配合threshold工作正常
4. 提取不阻塞对话（异步处理<200ms）
5. 与已有记忆自动去重

**验证手段**
- 准确率测试：50组对话样例→人工标注→计算P/R
- 去重测试：已有"用户喜欢简洁"→对话中再提→不重复写入
- 异步测试：extract接口响应时间<200ms
- 模式覆盖测试：每种规则模式3个正例+3个反例

---

#### F19: 上下文注入策略（Context Injection）

**需求描述**
定义记忆如何注入Agent的system prompt——存和搜只是基础设施，注入才是用户体验的最后一公里。

**解决的问题**
Agent框架各自拼装记忆到prompt，格式不统一、token浪费、信息优先级混乱。

**操作步骤**
1. Agent调用 `POST /v1/inject`
2. 引擎按strategy组装记忆内容
3. 按token_budget截断/摘要
4. 返回拼装好的文本块，Agent直接塞进system prompt

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| agent_id | str | ✅ | Agent标识 |
| query | str | ❌ | 当前用户输入（用于相关性召回） |
| token_budget | int | ❌ | token上限，默认2000 |
| strategy | enum | ❌ | full/summary/ranked，默认ranked |
| sections | list | ❌ | 注入板块：user_profile/preferences/facts/recent/relevant |
| template | str | ❌ | 自定义Jinja2模板 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| injected_text | str | 拼装好的注入文本 |
| token_count | int | 实际token数 |
| sources | list[str] | 来源记忆ID列表 |
| truncated | bool | 是否被截断 |

**技术方案**
- 模板引擎：Jinja2，内置默认模板（`## User Profile
...
## Relevant Context
...`）
- Token计算：tiktoken库（cl100k_base编码）
- ranked策略：search相关记忆→按score降序→贪心填充到budget
- 缓存：相同agent_id+query的注入结果缓存60秒

**验收标准**
1. 注入延迟<50ms（不含搜索）
2. token计数误差<5%
3. 输出不超过token_budget
4. 支持自定义Jinja2模板
5. 无记忆时返回空串不报错

**验证手段**
- token测试：注入结果用tiktoken验证不超budget
- 模板测试：自定义模板→验证输出格式
- 空记忆测试：新Agent无记忆→inject返回空串
- 缓存测试：相同参数连续调用→第二次<5ms

---

#### F20: 记忆审查界面（Memory Review UI）

**需求描述**
内置Web界面，用户通过浏览器审查所有记忆。按Agent过滤、时间轴浏览、编辑/删除/标记。

**解决的问题**
用户无法直观知道AI记了什么，缺乏掌控感和透明度。

**操作步骤**
1. 访问 `localhost:8200/ui`
2. 左侧Agent列表选择筛选
3. 主区域时间轴浏览记忆
4. 单条操作：编辑/删除/pin/升降温
5. 批量操作：多选后批量删除/归档
6. 搜索栏全文检索

**输入参数**
API层 `GET /v1/memories/timeline`：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| agent_id | str | ❌ | 按Agent过滤 |
| scope | list[str] | ❌ | scope过滤 |
| temperature | list[str] | ❌ | 温度过滤 |
| tags | list[str] | ❌ | 标签过滤 |
| date_from | datetime | ❌ | 起始时间 |
| date_to | datetime | ❌ | 结束时间 |
| page | int | ❌ | 页码 |
| page_size | int | ❌ | 每页条数，默认50 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| items | list | [{id, content, agent_id, scope, tags, temperature, pii, pinned, created_at, access_count}] |
| total | int | 总条数 |
| pages | int | 总页数 |

**技术方案**
- 前端：内置于FastAPI的静态文件服务，纯HTML+JS+CSS（零框架依赖）
- 样式：轻量级CSS（<10KB），响应式布局
- 交互：fetch API调用后端REST接口
- 部署：随hippocampus serve自动启动

**验收标准**
1. 1万条记忆加载<2秒（分页）
2. Agent过滤即时响应
3. 编辑/删除后实时更新
4. 移动端可用（响应式布局）
5. 支持导出选中记忆为JSON/Markdown

**验证手段**
- 加载测试：1万条记忆→页面加载计时
- 功能测试：创建→UI查看→编辑→删除→验证
- 移动端测试：Chrome DevTools模拟手机屏幕
- 导出测试：选中记忆→导出→验证内容完整


### 6.6 运维与集成

#### F21: Webhook/事件推送

**需求描述**
记忆变更时主动推送通知到外部系统，HMAC签名保证安全，指数退避重试保证可靠。

**解决的问题**
Agent间协调只能轮询，无法实时感知记忆变更。

**操作步骤**
1. 注册webhook：`POST /v1/webhooks`
2. 记忆变更时匹配事件类型→POST到注册URL
3. 请求带HMAC-SHA256签名
4. 失败指数退避重试（1s/5s/30s），连续10次失败自动禁用
5. 管理：`GET /v1/webhooks`，`DELETE /v1/webhooks/{id}`

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| url | str | ✅ | 回调地址 |
| events | list[str] | ✅ | 事件类型：memory.created/updated/deleted/promoted/archived, consolidation.completed, pii.detected |
| agent_filter | str | ❌ | 只监听指定Agent事件 |
| secret | str | ❌ | HMAC签名密钥 |

**输出参数**
推送payload：

| 参数 | 类型 | 说明 |
|------|------|------|
| event | str | 事件类型 |
| timestamp | float | 时间戳 |
| agent_id | str | 触发Agent |
| memory_id | str | 相关记忆ID |
| data | dict | 事件详情 |
| signature | str | HMAC-SHA256签名 |

**技术方案**
- 存储：`webhooks` 表（id, url, events, agent_filter, secret, enabled, failure_count）
- 推送：asyncio + httpx异步POST
- 签名：`HMAC-SHA256(secret, json.dumps(payload))`
- 重试：1s→5s→30s指数退避，failure_count≥10自动disable

**验收标准**
1. 事件推送延迟<500ms
2. HMAC签名可验证
3. 重试机制正常工作
4. 连续失败自动禁用webhook
5. 禁用后可手动重新启用

**验证手段**
- 推送测试：写入记忆→验证webhook收到通知
- 签名测试：收到推送→用secret验证HMAC
- 重试测试：回调返回500→验证重试3次
- 禁用测试：回调持续失败→验证自动禁用

---

#### F22: 备份与恢复（Backup & Restore）

**需求描述**
定时自动备份+手动备份+一键恢复。数据全在SQLite单文件里，备份策略是数据安全基线。

**解决的问题**
SQLite文件损坏、误操作删除记忆、服务器迁移等场景下的数据恢复需求。

**操作步骤**
1. 手动备份：`hippocampus backup` 或 `POST /v1/backup`
2. 自动备份：配置cron表达式（默认每天凌晨3点）
3. 备份内容打包为.hcb文件（tar.gz格式）
4. 恢复：`hippocampus restore <file>` → 停服→替换→重启→验证
5. 查看备份列表：`hippocampus backup --list`

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| path | str | ❌ | 备份存储路径，默认~/.hippocampus/backups/ |
| retention | int | ❌ | 保留份数，默认7 |
| schedule | str | ❌ | cron表达式，默认"0 3 * * *" |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| backup_path | str | 备份文件路径 |
| size_bytes | int | 备份大小 |
| checksum | str | SHA256校验和 |
| memories_count | int | 包含的记忆条数 |

**技术方案**
- 备份：SQLite在线备份API（`sqlite3.backup()`），不中断服务
- 打包：memory.db + config.yaml + hot/MEMORY.md → tar.gz → .hcb
- 校验：SHA256写入.hcb.sha256文件
- 恢复：解包→校验→替换→重启MemoryEngine
- 自动清理：保留最近N份，APScheduler CronTrigger

**验收标准**
1. 备份不中断服务（在线备份）
2. 恢复后所有记忆可检索
3. 备份含SHA256校验和
4. 损坏的备份恢复时明确报错
5. 自动备份按配置执行，超出retention自动清理

**验证手段**
- 备份恢复round-trip：备份→清空→恢复→搜索验证
- 在线测试：备份过程中写入记忆→两者均成功
- 校验测试：篡改.hcb→恢复时报校验错误
- 自动清理测试：设retention=2→备份3次→验证只保留2份

---

#### F23: 记忆导入/迁移（Import & Migration）

**需求描述**
从外部系统导入记忆，降低切换成本。支持Hermes MD、Mem0 JSON、通用JSON、CSV、Markdown目录5种格式。

**解决的问题**
用户已有记忆数据（Hermes、Mem0、笔记系统），无法无痛迁入海马体。

**操作步骤**
1. CLI：`hippocampus import --format hermes --source ~/memory-mcp/MEMORY.md`
2. API：`POST /v1/import` 上传文件
3. 解析源格式→标准化→去重检查→写入
4. dry_run预览导入数量和内容预览
5. 导入过程有进度输出

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| source | str | ✅ | 文件路径或URL |
| format | enum | ✅ | hermes/mem0/json/csv/markdown |
| agent_id | str | ✅ | 导入到的Agent |
| scope | str | ❌ | 默认agent |
| dry_run | bool | ❌ | 预览不执行 |
| dedup | bool | ❌ | 导入时去重，默认true |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| imported | int | 导入条数 |
| skipped_dup | int | 去重跳过条数 |
| errors | list | [{line, reason}] 解析错误 |
| preview | list? | dry_run时返回前10条预览 |

**技术方案**
- Hermes MD：按§分隔符拆分，每段→一条记忆
- Mem0 JSON：字段映射（text→content, tags→tags）
- CSV：pandas读取，content列必填
- Markdown目录：遍历*.md，按heading拆分
- 事务：批量INSERT包裹在事务中，失败回滚
- 去重：逐条计算余弦相似度>0.92→跳过

**验收标准**
1. 每种格式有示例文件和文档
2. dry_run返回预览不执行
3. 导入失败不影响已有数据（事务保护）
4. 重复记忆自动跳过
5. 10K条导入<60秒

**验证手段**
- 格式测试：每种格式准备测试文件→导入→验证
- round-trip测试：export→import→对比数据完整性
- dry_run测试：dry_run→验证数据未变→正式导入→验证写入
- 大批量测试：10K条CSV导入计时
- 错误测试：故意损坏文件→验证错误报告+已有数据无损

---

#### F24: Obsidian/文件系统整合

**需求描述**
将Obsidian vault或指定目录下的Markdown文件索引为可检索的知识库。支持watch模式自动重索引。

**解决的问题**
用户已有大量Markdown笔记，希望AI Agent能检索这些知识而不需要手动导入。

**操作步骤**
1. 配置索引路径：`POST /v1/knowledge/index`
2. 引擎遍历目录，解析Markdown内容和frontmatter
3. 计算embedding + 写入knowledge表 + FTS5索引
4. 可选watch模式：文件变更自动重索引
5. 搜索：`POST /v1/knowledge/search`（可跨memories+knowledge联合查询）

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| path | str | ✅ | 目录路径 |
| glob | str | ❌ | 文件匹配模式，默认"*.md" |
| recursive | bool | ❌ | 递归子目录，默认true |
| watch | bool | ❌ | 文件变更监听，默认false |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| indexed | int | 索引文件数 |
| total_chunks | int | 拆分后的知识块数 |
| errors | list | [{file, reason}] |

**技术方案**
- 解析：python-markdown解析内容，pyyaml解析frontmatter
- 分块：按heading拆分为chunks，每块独立索引
- 去重：file_hash（MD5）变更才重索引
- Watch：watchdog库监听文件变更事件
- 存储：独立 `knowledge` 表，结构类似memories但增加source_path和file_hash

**验收标准**
1. 1000个MD文件索引<30秒
2. watch模式文件变更后5秒内自动重索引
3. 搜索可跨memories+knowledge联合查询
4. frontmatter解析为metadata
5. 已索引文件未变更时不重复索引

**验证手段**
- 索引测试：索引100个MD→搜索验证命中
- watch测试：修改文件→等5秒→搜索验证内容更新
- 联合搜索测试：memories+knowledge同时命中的查询
- 性能测试：1000个MD文件索引计时
- 去重测试：重复索引→验证无重复记录

---

#### F25: 健康检查与监控

**需求描述**
服务健康检查端点+基础运行指标，便于运维监控和问题排查。

**解决的问题**
服务运行状态不透明，出问题只能看日志。

**操作步骤**
1. `GET /v1/health` — 基础健康检查
2. `GET /v1/metrics` — 运行指标（可选Prometheus格式）
3. 健康检查含DB连接、磁盘空间、服务状态

**输入参数**
无（GET请求）

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| status | str | healthy/degraded/unhealthy |
| uptime_seconds | float | 运行时长 |
| db_size_bytes | int | 数据库大小 |
| memory_count | int | 记忆总数 |
| hot/warm/cold | dict | 各层数量 |
| disk_free_bytes | int | 磁盘剩余 |
| version | str | 海马体版本号 |
| last_consolidation | datetime? | 上次整合时间 |
| last_backup | datetime? | 上次备份时间 |

**技术方案**
- health：检查SQLite可读写+磁盘空间>100MB
- metrics：内存计数器记录API调用次数/延迟
- 可选：Prometheus格式输出（prometheus_client库）

**验收标准**
1. /health响应<50ms
2. 数据库不可用时返回unhealthy
3. metrics包含API调用计数和延迟百分位
4. 磁盘空间低于阈值时status=degraded

**验证手段**
- 健康测试：正常→healthy，删DB→unhealthy
- 指标测试：调用N次API→验证metrics计数准确
- 降级测试：磁盘空间模拟不足→验证degraded状态

---

### 6.7 Dogfood迁移

#### F26: Hermes记忆迁移

**需求描述**
从现有Hermes内嵌记忆系统（~/memory-mcp/memory_server.py）无感迁移到海马体。这是Dogfood第一步，验证整个系统可用。

**解决的问题**
海马体的第一个用户就是Hermes Agent自己。迁移验证是发布前的必经之路。

**操作步骤**
1. 解析现有memory_server.py的4个MCP tools接口签名
2. 导入MEMORY.md和USER.md到海马体（F23 Hermes格式）
3. 迁移state.db中的冷记忆到海马体cold层
4. 修改Hermes config.yaml，MCP server从memory_server.py改为hippocampus mcp
5. 验证：Hermes记忆读写行为无变化

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| hermes_memory_path | str | ✅ | ~/memory-mcp/ 路径 |
| hermes_config_path | str | ✅ | Hermes config.yaml路径 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| migrated_hot | int | 迁移的热记忆条数 |
| migrated_cold | int | 迁移的冷记忆条数 |
| config_updated | bool | Hermes配置是否更新成功 |
| validation_passed | bool | 读写验证是否通过 |

**技术方案**
- MEMORY.md解析：§分隔符拆分→每段为一条hot记忆
- USER.md解析：§分隔符拆分→scope=user的hot记忆
- state.db解析：SQLite读取→mapping到hippocampus schema
- 配置迁移：修改Hermes config.yaml的mcp_servers section

**验收标准**
1. 迁移后Hermes的memory add/search/archive/promote四个操作正常
2. 原有MEMORY.md/USER.md内容可通过search检索
3. 迁移过程可回滚（保留原文件备份）
4. 迁移脚本幂等（重复运行不产生重复记忆）

**验证手段**
- 端到端测试：迁移→Hermes对话→验证记忆读写
- 完整性测试：对比迁移前后记忆条数和内容
- 回滚测试：迁移→回滚→验证原系统恢复
- 幂等测试：迁移两次→验证记忆数量不翻倍


---

## 七、操作流程

### 7.1 首次安装

```
pip install hippocampus → hippocampus init → hippocampus serve → 配置Agent MCP/REST → 开始使用
```

### 7.2 记忆生命周期

```
Agent对话 → 自动提取(F18)/手动add(F1) → 写入Warm层 → 生成embedding
         → 被检索时score++ → 高频→升Hot(F9) → 长期未用→降Cold(F9)
         → 整合(F5)去重/合并 → TTL到期/遗忘策略→删除(F3)
```

### 7.3 多Agent共享

```
Agent-A创建shared repo(F14) → 写入项目上下文 → 授权Agent-B(F12)
→ Agent-B search自动搜索shared repo → 获得上下文
→ 关键变更通过broadcast(F15)主动推送
```

---

## 八、架构图

```
┌─────────────────────────────────────────────────────────────┐
│                        客户端层                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │Claude Code│  │  Hermes  │  │  Cursor  │  │ 自定义Agent│  │
│  └─────┬────┘  └─────┬────┘  └─────┬────┘  └─────┬─────┘  │
│        │MCP          │REST         │MCP          │REST     │
└────────┼─────────────┼─────────────┼─────────────┼─────────┘
         ▼             ▼             ▼             ▼
┌─────────────────────────────────────────────────────────────┐
│  协议层: MCP Server(stdio/SSE) | REST API(FastAPI) | CLI    │
├─────────────────────────────────────────────────────────────┤
│  认证层: Agent Token → Repo权限 → Scope过滤                  │
├─────────────────────────────────────────────────────────────┤
│  核心引擎: 写入(去重) | 混合检索(FTS+Vec) | 遗忘 | 整合(Dream)│
├─────────────────────────────────────────────────────────────┤
│  存储层: Hot(LRU+MD) | Warm(SQLite+FTS5) | Cold(归档+压缩)   │
├─────────────────────────────────────────────────────────────┤
│  智能层: 规则引擎(MVP,零模型) | 可选外部LLM(整合/摘要/提取)    │
└─────────────────────────────────────────────────────────────┘

数据文件: ~/.hippocampus/
├── memory.db          # SQLite主库
├── config.yaml        # 配置
├── hot/MEMORY.md      # Hot记忆镜像
├── backups/           # 自动备份
└── logs/              # 运行日志
```

---

## 九、环境要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| OS | Linux/macOS/Windows(WSL2) | Linux |
| Python | 3.10+ | 3.11+ |
| RAM | 2GB（规则引擎） | 8GB+ |
| 磁盘 | 500MB | SSD 10GB+ |
| 网络 | 安装时需要 | 运行时完全离线 |

核心依赖：SQLite 3.35+, sqlite-vec, FastAPI, Uvicorn
可选依赖：外部LLM API(OpenAI/Anthropic), jieba(中文分词), tiktoken(token计算)

---

## 十、里程碑规划

### M0: Dogfood迁移（第1-2周）

**目标**：从Hermes现有记忆系统无感迁移到海马体原型

**交付物**
- [ ] hippocampus核心MemoryEngine类（add/search/delete/stats）
- [ ] MEMORY.md/USER.md → SQLite迁移脚本
- [ ] MCP Server（兼容现有4个tool签名）
- [ ] Hermes config.yaml改接hippocampus

**验收准则**
1. ✅ Hermes对话中 memory add → 写入hippocampus DB → search可检索
2. ✅ 原MEMORY.md中所有记忆可通过search命中
3. ✅ 迁移脚本幂等：运行两次记忆数不翻倍
4. ✅ 切换过程Hermes无感知（MCP tool签名不变）
5. ✅ 回滚方案：30秒内切回原memory_server.py

**涉及功能**：F1(写入), F2(检索), F3(删除), F7(MCP), F26(迁移)

---

### M1: 核心引擎（第3-4周）

**目标**：完整的记忆CRUD + 热冷分层 + 混合检索

**交付物**
- [ ] 完整REST API（F6）
- [ ] FTS5 + sqlite-vec混合检索
- [ ] 热冷三级温度管理（F4）
- [ ] CLI工具基础命令（F8）
- [ ] 单元测试覆盖>80%

**验收准则**
1. ✅ `POST /v1/memories` 写入延迟<50ms
2. ✅ `POST /v1/memories/search` 混合检索P@5≥0.8（50组测试集）
3. ✅ 1万条记忆规模search<100ms
4. ✅ Hot记忆注入延迟<5ms
5. ✅ `hippocampus add/search/stats` CLI可用
6. ✅ Swagger UI (`/docs`) 可交互
7. ✅ 所有API返回统一 `{data, error, meta}` 格式

**涉及功能**：F1-F5, F6, F8, F9

---

### M2: 多Agent隔离共享（第5-6周）

**目标**：Agent注册/认证 + 记忆库权限 + 共享机制

**交付物**
- [ ] Agent注册+Token认证体系（F11）
- [ ] 记忆库权限（F12）+ Session隔离（F13）
- [ ] 共享记忆库（F14）+ 广播（F15）
- [ ] 审计日志（F16）

**验收准则**
1. ✅ 无Token请求→401，scope不足→403，private不存在→404
2. ✅ Agent-A private记忆对Agent-B不可见
3. ✅ shared repo授权后Agent立即可检索（<1秒）
4. ✅ urgent广播在子Agent search时自动出现在结果首位
5. ✅ 审计日志记录完整，按维度可过滤
6. ✅ Session结束后临时记忆24h内清除

**涉及功能**：F11-F16

---

### M3: 智能引擎（第7-8周）

**目标**：智能整合 + 自动提取 + PII检测 + 上下文注入

**交付物**
- [ ] 整合引擎（F5规则层+可选模型层）
- [ ] 自动记忆提取（F18规则层）
- [ ] PII检测管道（F17）
- [ ] 上下文注入接口（F19）
- [ ] Auto-Dream定时任务

**验收准则**
1. ✅ 整合后有重复时记忆数减少≥20%
2. ✅ 自动提取准确率≥70%（规则层，50组测试）
3. ✅ PII标准格式识别率≥95%，误报<5%
4. ✅ inject输出不超过token_budget，误差<5%
5. ✅ Auto-Dream每天凌晨自动运行，有完整日志
6. ✅ 规则层零API费用可独立运行

**涉及功能**：F5, F17, F18, F19

---

### M4: 运维与集成（第9-10周）

**目标**：备份恢复 + Webhook + 知识库 + Review UI + 监控

**交付物**
- [ ] 备份恢复（F22）+ 自动备份定时任务
- [ ] Webhook事件推送（F21）
- [ ] Obsidian知识库索引（F24）
- [ ] 记忆审查Web UI（F20）
- [ ] 健康检查+监控（F25）
- [ ] 导入迁移（F23）5种格式

**验收准则**
1. ✅ 备份→恢复round-trip零数据丢失
2. ✅ Webhook推送延迟<500ms，HMAC可验证
3. ✅ 1000个MD文件索引<30秒，watch模式5秒内重索引
4. ✅ Review UI 1万条加载<2秒，移动端可用
5. ✅ /health响应<50ms，DB不可用时返回unhealthy
6. ✅ 5种导入格式各通过round-trip测试

**涉及功能**：F20-F25, F10(版本)

---

### M5: 发布（第11-12周）

**目标**：打包发布 + 文档 + 开源

**交付物**
- [ ] PyPI包 (`pip install hippocampus`)
- [ ] Docker镜像 (`hippocampus/hippocampus:latest`)
- [ ] GitHub README（功能介绍+快速开始+架构图）
- [ ] API文档（自动生成+手写Guide）
- [ ] 集成指南（Hermes/Claude Code/Cursor）
- [ ] CHANGELOG + CONTRIBUTING

**验收准则**
1. ✅ `pip install hippocampus && hippocampus serve` 5分钟内可用
2. ✅ Docker一行命令启动
3. ✅ README含Quick Start可照做成功
4. ✅ 至少3个Agent框架集成指南
5. ✅ 全部F1-F26功能通过集成测试
6. ✅ 无已知Critical/High级别bug

**涉及功能**：全部

---

## 附录A: API接口汇总

| 方法 | 路径 | 功能 | 里程碑 |
|------|------|------|--------|
| POST | `/v1/memories` | 写入记忆(F1) | M0 |
| POST | `/v1/memories/search` | 检索记忆(F2) | M0 |
| DELETE | `/v1/memories/{id}` | 删除记忆(F3) | M0 |
| GET | `/v1/memories/{id}` | 获取单条 | M1 |
| PUT | `/v1/memories/{id}` | 更新记忆 | M1 |
| POST | `/v1/memories/batch` | 批量写入(F1) | M1 |
| POST | `/v1/memories/forget` | 批量遗忘(F3) | M1 |
| POST | `/v1/memories/{id}/promote` | 升温(F4) | M1 |
| POST | `/v1/memories/{id}/archive` | 降温(F4) | M1 |
| GET | `/v1/memories/stats` | 统计(F4) | M1 |
| POST | `/v1/maintenance/sweep` | 温度调控(F9) | M1 |
| POST | `/v1/consolidate` | 触发整合(F5) | M3 |
| POST | `/v1/agents` | 注册Agent(F11) | M2 |
| POST | `/v1/agents/{id}/tokens` | 生成Token(F12) | M2 |
| POST | `/v1/repos` | 创建记忆库(F11) | M2 |
| POST | `/v1/repos/{id}/grant` | 授权(F12) | M2 |
| DELETE | `/v1/repos/{id}/grant/{agent_id}` | 撤销授权(F12) | M2 |
| GET | `/v1/repos/{id}/grants` | 授权列表(F12) | M2 |
| POST | `/v1/sessions` | 开始会话(F13) | M2 |
| DELETE | `/v1/sessions/{id}` | 结束会话(F13) | M2 |
| POST | `/v1/broadcast` | 广播(F15) | M2 |
| GET | `/v1/inbox` | 收件箱(F15) | M2 |
| GET | `/v1/audit/sharing` | 审计日志(F16) | M2 |
| POST | `/v1/memories/scan` | PII扫描(F17) | M3 |
| POST | `/v1/extract` | 自动提取(F18) | M3 |
| POST | `/v1/inject` | 上下文注入(F19) | M3 |
| GET | `/v1/memories/timeline` | 时间轴(F20) | M4 |
| POST | `/v1/webhooks` | 注册webhook(F21) | M4 |
| GET | `/v1/webhooks` | 列表webhook(F21) | M4 |
| DELETE | `/v1/webhooks/{id}` | 删除webhook(F21) | M4 |
| POST | `/v1/backup` | 备份(F22) | M4 |
| POST | `/v1/restore` | 恢复(F22) | M4 |
| POST | `/v1/import` | 导入(F23) | M4 |
| POST | `/v1/knowledge/index` | 索引知识库(F24) | M4 |
| POST | `/v1/knowledge/search` | 搜索知识库(F24) | M4 |
| GET | `/v1/health` | 健康检查(F25) | M0 |
| GET | `/v1/metrics` | 监控指标(F25) | M4 |
| GET | `/v1/memories/{id}/history` | 版本历史(F10) | M4 |
| POST | `/v1/memories/{id}/rollback` | 回滚(F10) | M4 |
| POST | `/v1/memories/conflicts` | 冲突列表(F10) | M4 |

---

## 附录B: 数据库Schema

```sql
-- 记忆主表
CREATE TABLE memories (
    id TEXT PRIMARY KEY DEFAULT (hex(randomblob(8))),
    content TEXT NOT NULL,
    embedding BLOB,
    agent_id TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    scope TEXT CHECK(scope IN ('user','agent','session','shared')) DEFAULT 'agent',
    tags TEXT DEFAULT '[]',
    metadata TEXT DEFAULT '{}',
    temperature TEXT CHECK(temperature IN ('hot','warm','cold')) DEFAULT 'warm',
    access_count INTEGER DEFAULT 0,
    ttl INTEGER DEFAULT 0,
    pinned BOOLEAN DEFAULT FALSE,
    created_at REAL DEFAULT (unixepoch('subsec')),
    updated_at REAL DEFAULT (unixepoch('subsec')),
    last_accessed REAL DEFAULT (unixepoch('subsec'))
);

-- FTS5全文索引
CREATE VIRTUAL TABLE memories_fts USING fts5(content, tags, tokenize='unicode61');

-- 向量索引
CREATE VIRTUAL TABLE memories_vec USING vec0(embedding float[768]);

-- Agent表
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at REAL DEFAULT (unixepoch('subsec'))
);

-- Agent Token表
CREATE TABLE agent_tokens (
    id TEXT PRIMARY KEY DEFAULT (hex(randomblob(8))),
    agent_id TEXT REFERENCES agents(id),
    token_hash TEXT NOT NULL,
    scopes TEXT DEFAULT '[]',
    created_at REAL DEFAULT (unixepoch('subsec')),
    revoked_at REAL
);

-- 记忆库表
CREATE TABLE repos (
    id TEXT PRIMARY KEY DEFAULT (hex(randomblob(8))),
    name TEXT NOT NULL,
    owner_agent_id TEXT REFERENCES agents(id),
    visibility TEXT CHECK(visibility IN ('public','private')) DEFAULT 'private',
    created_at REAL DEFAULT (unixepoch('subsec'))
);

-- 授权表
CREATE TABLE repo_grants (
    repo_id TEXT REFERENCES repos(id),
    agent_id TEXT REFERENCES agents(id),
    permission TEXT CHECK(permission IN ('read','write','admin')),
    tags TEXT,
    PRIMARY KEY (repo_id, agent_id)
);

-- Session表
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT REFERENCES agents(id),
    created_at REAL DEFAULT (unixepoch('subsec')),
    ended_at REAL
);

-- Session记忆关联
CREATE TABLE session_memories (
    session_id TEXT REFERENCES sessions(id),
    memory_id TEXT REFERENCES memories(id),
    PRIMARY KEY (session_id, memory_id)
);

-- 收件箱（广播）
CREATE TABLE inbox (
    id TEXT PRIMARY KEY DEFAULT (hex(randomblob(8))),
    agent_id TEXT REFERENCES agents(id),
    content TEXT NOT NULL,
    from_agent TEXT,
    priority TEXT CHECK(priority IN ('normal','urgent')) DEFAULT 'normal',
    read BOOLEAN DEFAULT FALSE,
    ttl INTEGER DEFAULT 0,
    created_at REAL DEFAULT (unixepoch('subsec'))
);

-- 知识库表
CREATE TABLE knowledge (
    id TEXT PRIMARY KEY DEFAULT (hex(randomblob(8))),
    content TEXT NOT NULL,
    embedding BLOB,
    source_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    created_at REAL DEFAULT (unixepoch('subsec')),
    updated_at REAL DEFAULT (unixepoch('subsec'))
);

-- 整合日志
CREATE TABLE consolidation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT,
    affected_ids TEXT,
    detail TEXT,
    created_at REAL DEFAULT (unixepoch('subsec'))
);

-- 记忆版本历史
CREATE TABLE memory_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    changed_by TEXT,
    change_type TEXT CHECK(change_type IN ('manual','consolidation','auto_extract','import','rollback')),
    created_at REAL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX idx_mv_memory ON memory_versions(memory_id, version);

-- Webhook表
CREATE TABLE webhooks (
    id TEXT PRIMARY KEY DEFAULT (hex(randomblob(8))),
    url TEXT NOT NULL,
    events TEXT DEFAULT '[]',
    agent_filter TEXT,
    secret TEXT,
    enabled BOOLEAN DEFAULT TRUE,
    failure_count INTEGER DEFAULT 0,
    created_at REAL DEFAULT (unixepoch('subsec'))
);

-- 共享审计日志
CREATE TABLE sharing_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL DEFAULT (unixepoch('subsec')),
    actor_agent_id TEXT,
    target_agent_id TEXT,
    repo_id TEXT,
    action TEXT,
    detail TEXT
);
CREATE INDEX idx_audit_time ON sharing_audit(timestamp);
CREATE INDEX idx_audit_agent ON sharing_audit(actor_agent_id);
```

---

#### F2: 记忆检索（Search/Recall）

**需求描述**
混合检索引擎：FTS5全文搜索 + 向量语义搜索 + 标签过滤，加权融合返回排序结果。

**解决的问题**
精确关键词搜索漏召回（语义不同但意思相近），纯向量搜索不精确（关键词完全匹配却排名低）。混合方案兼顾。

**操作步骤**
1. Agent调用 `POST /v1/memories/search`，传入query
2. 并行执行FTS5全文搜索 + 向量近邻搜索
3. 按配置权重融合两路结果（默认FTS 0.4 + Vec 0.6）
4. 应用scope/tags/agent_id过滤
5. 按融合分数排序，截取top-N返回
6. 更新命中记忆的access_count和last_accessed

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | str | ✅ | 搜索词/语句 |
| agent_id | str | ❌ | 限定Agent |
| scope | list[str] | ❌ | 限定scope |
| tags | list[str] | ❌ | 标签过滤 |
| limit | int | ❌ | 返回条数，默认10，最大100 |
| threshold | float | ❌ | 相关性阈值，默认0.5 |
| mode | enum | ❌ | hybrid/fts/vector，默认hybrid |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| results | list | [{id, content, score, tags, scope, temperature, created_at, access_count}] |
| total_count | int | 命中总数（不受limit限制） |

**技术方案**
- FTS5：`memories_fts` 虚表，unicode61 tokenizer + jieba中文分词（可选）
- 向量：sqlite-vec `memories_vec` 虚表，768维float向量
- 融合：RRF (Reciprocal Rank Fusion) — `score = Σ 1/(k+rank_i)`，k=60
- 权限：自动过滤无权限repo的记忆

**验收标准**
1. 混合检索P@5 ≥ 0.8（人工标注测试集）
2. 1万条记忆规模下响应<100ms
3. FTS5支持中英文混合查询
4. 检索自动更新access_count和last_accessed
5. mode=fts/vector可单独使用

**验证手段**
- 准确率测试：构建50组query+标注，计算P@5
- 性能测试：插入1万条后bench search延迟
- 中文测试：纯中文/中英混合查询验证分词
- 回归测试：每次改动后跑完整测试集

---

#### F3: 记忆删除/遗忘（Delete/Forget）

**需求描述**
手动删除指定记忆，或按策略批量自动遗忘低价值记忆。支持dry_run预览和"永不遗忘"标记。

**解决的问题**
记忆只增不减导致膨胀、检索噪音增大、存储无限增长。

**操作步骤**
1. 手动删除：`DELETE /v1/memories/{id}` → 从memories表+FTS5+vec0删除
2. 批量遗忘：`POST /v1/memories/forget` → 按策略筛选候选 → dry_run预览或执行删除
3. 遗忘评分公式：`score = 0.4×recency + 0.35×relevance + 0.25×frequency`
4. score < threshold 且 pinned=false → 标记待清除
5. 执行删除前自动记录到consolidation_log

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | str | ✅(删除) | 记忆ID |
| strategy | enum | ✅(遗忘) | ttl/cold/unused/score |
| dry_run | bool | ❌ | 预览不执行，默认false |
| before | datetime | ❌ | 只处理此时间之前的记忆 |
| threshold | float | ❌ | 遗忘分数阈值，默认0.1 |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| deleted_count | int | 实际/将删除的条数 |
| deleted_ids | list[str] | 删除的记忆ID列表 |
| skipped_pinned | int | 跳过的pinned记忆数 |

**技术方案**
- 删除：SQLite事务，同步清理memories + memories_fts + memories_vec三表
- 遗忘公式：`recency(t) = exp(-0.05×天数)`，`frequency(n) = min(n/20, 1.0)`
- 日志：每次遗忘写入consolidation_log（action=forget）

**验收标准**
1. 删除后search不再返回该条目
2. dry_run返回候选列表但不执行删除
3. pinned=true的记忆永不被自动遗忘
4. 批量遗忘1000条<5秒
5. 删除操作记录在consolidation_log中可审计

**验证手段**
- 功能测试：删除→搜索验证不存在
- dry_run测试：dry_run后再搜索，验证数据仍在
- pinned测试：pinned记忆在遗忘策略下不被删除
- 日志测试：遗忘后查consolidation_log验证记录完整

---

#### F4: 热冷分层（Temperature Management）

**需求描述**
三级温度体系：Hot(内存LRU+MD文件) → Warm(SQLite主表) → Cold(SQLite归档)。高频记忆常驻内存极速访问，低频记忆降级节省资源。

**解决的问题**
所有记忆平等对待导致：高频记忆访问慢（每次查DB），低频记忆占用热资源。

**操作步骤**
1. 新记忆写入默认Warm（SQLite主表）
2. 手动升温：`POST /v1/memories/{id}/promote` → 加载到LRU缓存+同步MEMORY.md
3. 手动降温：`POST /v1/memories/{id}/archive` → 移至归档表+压缩embedding
4. 自动调控由F11定时任务处理
5. `GET /v1/memories/stats` 查看各层统计

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | str | ✅ | 记忆ID |
| target | enum | ❌ | hot/warm（promote时），默认hot |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| id | str | 记忆ID |
| temperature | str | 变更后的温度 |
| stats | dict | {hot_count, warm_count, cold_count, hot_size_bytes, total_size_bytes} |

**技术方案**
- Hot：Python LRU缓存（默认500条上限）+ MEMORY.md文件实时同步
- Warm：SQLite memories主表，FTS5+vec0索引齐全
- Cold：SQLite `memories_archive` 表，embedding压缩50%存储
- 升降温：UPDATE temperature字段 + 移动/复制数据

**验收标准**
1. Hot记忆注入延迟<5ms（内存直读）
2. Warm检索延迟<100ms
3. stats接口返回各层准确数量和大小
4. MEMORY.md与Hot缓存内容一致
5. Cold记忆被检索命中时自动升温到Warm

**验证手段**
- 延迟测试：Hot vs Warm检索延迟对比bench
- 一致性测试：升温后检查LRU缓存+MEMORY.md内容
- stats测试：写入已知数量后验证stats返回值
- 自动升温测试：搜索命中Cold记忆后验证温度变为Warm

---

#### F5: 记忆智能整合（Consolidation / Auto-Dream）

**需求描述**
定期整合记忆：聚类去重、合并矛盾、提炼摘要，类似人脑REM睡眠。分两层——规则层（零模型依赖，MVP）和模型层（可选外部API）。

**解决的问题**
记忆随时间积累大量重复/矛盾/碎片化内容，降低检索质量和存储效率。

**操作步骤**
1. 手动触发 `POST /v1/consolidate` 或定时任务自动运行（默认每天凌晨）
2. 规则层：按余弦相似度>0.85聚类 → 模板拼接合并
3. 矛盾检测：同scope同主题但情感/意图相反的记忆对，标记conflict
4. 模型层（可选）：调用外部LLM对聚类做摘要提炼
5. 生成整合报告写入consolidation_log
6. 被合并的旧记忆创建版本历史（F27）后删除

**输入参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| scope | str | ❌ | 限定整合范围 |
| agent_id | str | ❌ | 限定Agent |
| dry_run | bool | ❌ | 预览不执行 |
| use_model | bool | ❌ | 是否启用模型层，默认false |

**输出参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| merged | int | 合并的记忆组数 |
| deduplicated | int | 去重删除条数 |
| conflicts_found | int | 发现的矛盾对数 |
| report | str | 人可读的整合报告 |

**技术方案**
- 聚类：sqlite-vec批量计算余弦矩阵，>0.85归为一组
- 合并（规则层）：取最新content为主体，附加其余条目的差异信息
- 合并（模型层）：prompt = "合并以下记忆为一条摘要：{cluster}"
- 矛盾检测（规则层）：关键词对立模式匹配（喜欢/不喜欢，是/不是）
- 定时：APScheduler CronTrigger，默认 `0 3 * * *`

**验收标准**
1. 整合后有重复时记忆数量减少≥20%
2. 矛盾检测准确率≥80%（规则层）
3. 整合不丢失关键信息（人工抽检10组）
4. dry_run返回报告但不修改数据
5. 规则层零API费用可独立运行
6. 整合日志完整记录每个操作

**验证手段**
- 去重测试：插入20条近义记忆，整合后验证数量和内容
- 矛盾测试：插入"用户喜欢A"和"用户不喜欢A"，验证检测到conflict
- 幂等测试：连续运行两次整合，第二次应无变更
- 日志测试：整合后查consolidation_log验证记录完整
- 无损测试：人工抽检整合前后关键信息完整性
