# OpenHippo 开发进度报告

> 更新：2026-04-19 | 基线：PRD v2.1

---

## 一、总览

| 指标 | 数据 |
|------|------|
| PRD功能项 | 26个（F1-F26） |
| 已完成 | 10个 ✅ |
| 部分完成 | 3个 🔶 |
| 未开始 | 13个 ⬜ |
| 源码文件 | 10个 Python模块 |
| 测试用例 | 55个（全通过） |
| Git commits | 9个 |

---

## 二、功能逐项比对

### ✅ 已完成（10/26）

| 功能 | PRD要求 | 实现状态 | 验证 |
|------|---------|----------|------|
| **F1: 记忆写入** | CRUD + 去重 + embedding | 精确去重(SHA-256) + 语义去重(L2<0.4) + 自动embedding | 55/55测试通过 |
| **F2: 记忆检索** | FTS5 + 向量搜索 + 混合 | FTS5 + sqlite-vec + RRF hybrid fusion，三种模式 | ✅ |
| **F3: 记忆删除** | 按内容/ID删除 | hot remove + cold DELETE by ID | ✅ |
| **F4: 热冷分层** | 自动eviction | hot(4400/2750 chars) → cold，自动evict到90% | ✅ |
| **F6: REST API** | FastAPI + OpenAPI | 17个端点，Swagger UI，统一{data}响应 | 42+13测试 |
| **F7: MCP协议** | stdio MCP Server | 5个tool(add/search/replace/remove/archive) | ✅ |
| **F8: CLI工具** | 命令行操作 | `openhippo serve/mcp` + Click CLI | ✅ |
| **F25: 健康检查** | /health端点 | GET /health → {status, version} | ✅ |
| **F26: Hermes迁移** | 从Hermes导入数据 | 已完成hot+cold全量迁移 | ✅ |
| **嵌入抽象层** | (S1/S2独立部署需求) | Ollama/SentenceTransformer双后端 + 统一配置 | ✅ |

### 🔶 部分完成（3/26）

| 功能 | PRD要求 | 已做 | 缺少 |
|------|---------|------|------|
| **F9: 温度调控** | access_count衰减 + 自动归档 | access_count追踪 + eviction归档 | 时间衰减函数、显式promote逻辑 |
| **F20: 审查界面** | Web UI审查记忆 | REST CRUD by ID (GET/PUT/DELETE) | 前端Web UI |
| **F22: 备份恢复** | 完整备份+增量备份 | SQLite文件级备份可用 | CLI命令封装、增量备份 |

### ⬜ 未开始（13/26）

| 功能 | 优先级 | 备注 |
|------|--------|------|
| **F5: 记忆整合(Dream)** | P0 | 核心差异化，需实现sleep consolidation |
| **F10: 版本与冲突** | P2 | 多Agent写入冲突解决 |
| **F11: 三级隔离** | P1 | tenant→agent→session |
| **F12: 权限体系** | P1 | RBAC |
| **F13: Session隔离** | P1 | 会话级记忆 |
| **F14: 共享记忆库** | P2 | GitHub-style repos |
| **F15: 记忆广播** | P3 | pub/sub |
| **F16: 共享审计日志** | P2 | audit trail |
| **F17: PII检测** | P1 | 敏感信息过滤 |
| **F18: 自动提取** | P1 | 对话→记忆提取 |
| **F19: 上下文注入** | P1 | 智能召回注入 |
| **F21: Webhook** | P3 | 事件推送 |
| **F23: 导入迁移** | P2 | 通用数据导入 |
| **F24: Obsidian整合** | P2 | 笔记系统联动 |

---

## 三、独立部署路线图（进行中）

| 阶段 | 内容 | 状态 |
|------|------|------|
| S1: Embedding抽象层 | Ollama/ST双后端 | ✅ 完成 |
| S2: 统一配置系统 | YAML + 环境变量 | ✅ 完成 |
| S3: Bearer Token认证 | 中间件 | ⬜ 下一步 |
| S4: Plugin远程连接 | HTTP client配置 | ⬜ |
| S5: Docker化 | Dockerfile + compose | ⬜ |
| S6: 双VM端到端测试 | 跨机器验证 | ⬜ |

---

## 四、技术栈

| 层 | 选型 |
|----|------|
| 存储 | SQLite + FTS5 + sqlite-vec |
| Embedding | nomic-embed-text-v1.5 (Ollama / SentenceTransformer) |
| API | FastAPI + Uvicorn |
| MCP | stdio模式 |
| 配置 | YAML + 环境变量覆盖 |
| 测试 | pytest, 55用例 |

---

## 五、关键指标

| 指标 | 目标 | 实际 |
|------|------|------|
| 单次写入延迟 | <50ms | ~100ms（含embedding） |
| 搜索延迟 | <100ms | <100ms ✅ |
| 向量维度 | 768 | 768 ✅ |
| 测试覆盖 | 全量通过 | 55/55 ✅ |
| 代码量 | — | ~1200 LOC |
