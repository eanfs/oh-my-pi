# 设计文档 / PRD：RPC 内核 + AI Gateway + microVM 执行层

> 范围：把 oh-my-pi 内核产品化为企业级多租户 AI Agent 平台的**核心三层**落地设计。
> 配套：调研依据见 [`enterprise-agent-platform-research.md`](./enterprise-agent-platform-research.md)；架构图见 [`agent-platform-architecture-20260609.html`](./agent-platform-architecture-20260609.html)。
> 状态：Draft v0.1 · 2026-06-09

---

## 1. 背景与目标

### 1.1 问题陈述
oh-my-pi 当前是**单机 CLI / 嵌入式 agent 内核**：通过 CLI、TUI、Print、RPC 四种模式驱动，会话落本地 NDJSON 盘，凭据 per-session 存储，工具（`bash`/`eval`/文件操作）直接在宿主进程内运行。要对外做**多租户云服务**，缺三样东西：

1. **对外接口层**——RPC 是 stdin/stdout 进程协议，不能直接面向公网客户端（无鉴权、无限流、无多租户身份、无计费）。
2. **安全执行边界**——`bash`/`eval` 跑 LLM 生成的不可信代码，`pi-iso` 只隔离文件系统，**共享宿主内核**，多租户下是逃逸风险。
3. **状态可扩展性**——NDJSON 落本地盘，实例不可水平迁移/恢复。

### 1.2 目标（本设计覆盖）
- **G1**：定义 **AI Gateway**，把 RPC 内核能力安全暴露为 REST+SSE / WebSocket / MCP。
- **G2**：定义**实例调度模型**——一会话/租户一 RPC 内核实例（Silo），含生命周期与租户路由。
- **G3**：定义 **microVM 执行层**，让不可信代码在内核级隔离中运行，`pi-iso` 降级为 microVM 内的工作区隔离。
- **G4**：定义**状态外置**——NDJSON event log 外置到对象存储，支持实例重建。

### 1.3 非目标（本设计不覆盖，后续单列）
- 计费定价模型、前端控制台 UI、skills 市场/审核流程、私有化离线交付包、durable execution（Temporal）落地细节（仅留扩展点）。

---

## 2. 总体架构

```
Client ──(REST+SSE / WebSocket / MCP)──▶ AI Gateway
                                            │ (gRPC, 双向流)
                                            ▼
                                   Instance Scheduler ──┐
                                            │           │ 管理生命周期
                                   ┌────────┴────────┐  │
                                   ▼                 ▼  ▼
                            RPC Kernel #A      RPC Kernel #B   ...   (一会话/租户一实例, K8s Pod)
                            (agent-loop +      每实例 = 一个
                             skills + Task)    AgentSession 进程
                                   │ exec 不可信代码
                                   ▼
                            microVM 执行层 (Kata/Firecracker)
                            └ pi-iso worktree + bash + eval + per-tenant egress
                                   │ 状态
                                   ▼
                            NDJSON(event) → Object Store / Redis 热缓存 / 元数据 DB
```

**控制面（Gateway + Scheduler + 元数据 DB）** 与 **数据面（RPC 内核实例 + microVM）** 分离；凭据经 **Secrets Broker（Vault）** JIT 下发；遥测经 **OTel Collector** 打 `tenant_id` 维度。

---

## 3. 组件设计

### 3.1 AI Gateway（新建服务）

**职责**：对外协议终止 + 横切关注点。**有意做薄**——业务逻辑全在内核，网关只做 auth / 路由 / 限流 / 计量 / 审计。

| 能力 | 设计 |
|---|---|
| 鉴权 & 租户身份 | API Key / OAuth2 / JWT → 解析出 `tenant_id` + `principal`，注入下游所有调用的 context |
| 协议适配 | 对外 REST+SSE（补全）、WebSocket（交互/steer）、MCP（工具联邦）；对内 gRPC 双向流 ↔ 内核 |
| 限流 & 配额 | 按 `tenant_id` 做 token-bucket（QPS）+ 月度 token 配额（读元数据 DB）|
| 多模型路由/回退 | 复用内核 `modelRoles` + `fallbackChains`，网关层加租户级模型白名单 |
| 用量计量 & 计费 | 订阅内核 OTel GenAI 指标（input/output tokens）→ 计费事件流 |
| 审计 | 每请求落审计日志（who/when/tenant/action），默认**不记 prompt/response 正文**（合规） |
| 安全 | prompt 注入防护（可选接入 MCP 网关安全工具）、出参脱敏 |

**对外 API 形态（建议）**：

```
POST /v1/sessions                     # 创建会话 → 触发 Scheduler 分配实例，返回 session_id
POST /v1/sessions/{id}/messages       # 发送 prompt；Accept: text/event-stream → SSE 流式
WS   /v1/sessions/{id}/stream         # WebSocket：双向，支持 steer / abort / follow_up
POST /v1/sessions/{id}/abort          # 中断当前回合
GET  /v1/sessions/{id}/events         # 重放会话事件（从 NDJSON event log）
GET  /v1/mcp                          # MCP server endpoint（暴露 skills/tools）
```

**映射到内核 RPC 命令**：网关把 HTTP/WS 请求翻译成内核 RPC 的 `prompt` / `steer` / `abort` / `follow_up` / `compact` 等 50+ 命令，内核回传的 `AgentSessionEvent` 流再翻译回 SSE/WS 帧。**RPC 协议是天然的内部契约，网关 = 协议翻译 + 横切层。**

### 3.2 Instance Scheduler（新建服务）

**职责**：RPC 内核实例的生命周期 + 租户路由。

- **分配策略（Silo）**：默认**一会话一实例**（最强隔离）；可配置为**一租户一实例池**（按客单价/SLA 分层，对应调研里的 silo→bridge 演进）。
- **路由表**：`session_id → instance endpoint`（存 Redis），网关查表转发 gRPC。
- **生命周期**：
  - 创建：拉起 K8s Pod（内核镜像）→ 等 `{"type":"ready"}` → 写路由表。
  - 空闲回收：N 分钟无活动 → flush NDJSON 到对象存储 → 销毁 Pod → 删路由。
  - 恢复：会话再次活跃 → 新 Pod + 从对象存储 `setSessionFile()` 重建上下文（复用内核 `switchSession` 能力）。
- **配额/隔离**：每 Pod 套 `ResourceQuota` + `LimitRange`；高价值租户用**专用节点池**（taint/toleration）抑制 noisy-neighbor。
- **水平扩展**：HPA 按活跃会话数 / CPU 扩；Scheduler 本身无状态（路由表在 Redis）。

### 3.3 RPC 内核实例（复用 oh-my-pi，最小改造）

- **运行形态**：`omp --mode rpc` 作为容器主进程，gRPC sidecar 把 stdin/stdout JSONL 桥成 gRPC 双向流（或网关直连 stdio over gRPC streaming）。
- **复用**：`agent-loop`、`skills` 引擎、`Task` 子代理、`compaction`、`telemetry`（OTel）全部原样复用——**这是“本项目负责编排 + skills 核心业务”的落点**。
- **改造点**：
  1. 工具执行从“宿主进程内”改为“委托 microVM 执行层”（见 3.4），通过内核已有的工具抽象注入一个 **remote exec backend**。
  2. NDJSON writer 从本地盘改为**可插拔 SessionStorage**（内核已有 `SessionStorage` 接口 + `FileSessionStorage`/`MemorySessionStorage`）——新增 `ObjectStoreSessionStorage`。
  3. `injectable fetch` 注入为 **per-tenant egress 代理**（按 `tenant_id` 路由出站，审计 LLM/web 调用）。
  4. 凭据从 `AuthStorage` 本地文件改为**从 Secrets Broker 拉取**的 JIT 短时凭据。

### 3.4 microVM 执行层（新建，最关键的安全层）

**职责**：在内核级隔离里运行所有不可信代码（`bash`、`eval` Python/JS、文件工具）。

- **隔离技术**（择一，见 §6 决策）：
  - **Kata Containers**：K8s 原生（RuntimeClass），改动最小，Pod 即 microVM。
  - **Firecracker**：~125ms 启动，最轻，但需自建编排（参考 E2B）。
  - **gVisor**：用户态内核，启动快但兼容性/性能折中。
  - **买**：E2B（Firecracker，~80–200ms，支持 BYOC 进客户 AWS/GCP）作为托管执行层。
- **pi-iso 的新定位**：**降级为 microVM 内的工作区隔离**——microVM 提供内核隔离，pi-iso 在其内提供 worktree/overlayfs 做多任务/多子代理的文件系统快照与 diff。二者叠加而非二选一。
- **接口**：内核通过 remote exec backend 把 `bash`/`eval`/file op 发到 microVM；microVM 回传 stdout/stderr/exit + 文件 delta（复用 pi-iso 的 `captureDeltaPatch`）。
- **egress**：microVM 出站强制走 per-tenant egress 代理（默认 deny-all + 白名单 LLM/工具域名），杜绝数据外泄与横向移动。

### 3.5 状态层

| 存储 | 内容 | 说明 |
|---|---|---|
| **Object Store（S3）** | NDJSON event log（真相源） | 内核 NDJSON 本就是 event-sourced，外置后实例可重建 |
| **Redis** | 路由表 + 热会话缓存 | Scheduler 路由、活跃会话快照 |
| **元数据 DB（PG）** | 租户、会话索引、配额、计费账 | 控制面查询 |
| **（可选）Temporal** | 编排工作流持久化 | 留扩展点；注意其能力是**持久化+重放，非“保证完成”** |

---

## 4. 关键数据流

### 4.1 创建会话并流式补全
```
1. Client → Gateway: POST /v1/sessions (Authorization)
2. Gateway: 验签 → tenant_id；调用 Scheduler.allocate(tenant_id)
3. Scheduler: 拉起 RPC Kernel Pod → 等 ready → 写 Redis 路由
4. Gateway → Client: 201 {session_id}
5. Client → Gateway: POST /messages (SSE) {prompt}
6. Gateway → Kernel(gRPC): RPC `prompt`
7. Kernel: agent-loop 跑回合；遇 bash/eval → 委托 microVM 执行 → 收 delta
8. Kernel → Gateway: AgentSessionEvent 流（assistant token / tool card / done）
9. Gateway → Client: SSE 帧（含计量埋点 → OTel）
10. Kernel: 追加 NDJSON event → 异步刷 Object Store
```

### 4.2 实例回收与恢复
```
空闲超时 → Kernel flush NDJSON → Object Store → Scheduler 销毁 Pod → 删路由
再次活跃 → Scheduler 新 Pod → Kernel setSessionFile(从 S3) → 重建上下文 → 继续
```

---

## 5. 多租户隔离矩阵

| 层 | 隔离手段 | 强度 |
|---|---|---|
| 网络 | NetworkPolicy + per-tenant egress 代理（deny-all 白名单） | 高 |
| 计算 | 一会话/租户一 Pod + ResourceQuota + 专用节点池 | 高 |
| 内核（代码执行） | **Kata/Firecracker microVM**（不共享宿主内核） | 最高 |
| 文件系统 | microVM 内 pi-iso worktree/overlayfs | 高 |
| 数据 | 会话 NDJSON 按 tenant 前缀分桶 + DB 行级租户键 | 高 |
| 凭据 | Secrets Broker JIT 短时凭据 + per-session 隔离 | 高 |
| 身份 | Gateway 解析 tenant_id 注入全链路 | 高 |

---

## 6. 关键决策点（需评审拍板）

1. **microVM 技术选型**：Kata（K8s 原生、改动小）vs Firecracker 自建（最轻、运维重）vs 买 E2B（最快上线、BYOC 但仅 AWS/GCP）。**初版建议 Kata**（RuntimeClass 接入成本最低）。
2. **隔离粒度**：一会话一实例（最强、成本高）vs 一租户一池（成本优、需 pool 内再隔离）。**建议按租户分层（bridge）**。
3. **状态后端**：纯 S3+自建重放 vs 引入 Temporal。**初版建议 S3+Redis，Temporal 留扩展点**（避免过早引入 + 其能力被营销夸大）。
4. **网关选型**：Kong / APISIX（原生 MCP）/ 自建薄网关。**建议 APISIX 或自建**（需 MCP + SSE/WS 流式 + 自定义计量）。
5. **执行委托协议**：内核↔microVM 用 gRPC vs 共享 RPC 协议扩展。

---

## 7. 内核改造清单（落到 oh-my-pi 仓库）

| 改造 | 涉及模块（基于代码分析） | 类型 |
|---|---|---|
| ObjectStore SessionStorage | `session/session-storage.ts`（已有 `SessionStorage` 接口） | 新增实现 |
| Remote exec backend | `tools/index.ts` 工具工厂 + `exec/bash-executor.ts` + `eval/` | 注入抽象 |
| per-tenant egress fetch | 已有 `injectable fetch`（`ai/src/stream.ts`、`tools/fetch.ts`） | 注入配置 |
| Secrets Broker 凭据源 | `session/auth-storage.ts` + `config/api-key-resolver.ts` | 替换后端 |
| OTel tenant_id 维度 | `agent/src/telemetry.ts`（已集成 OTel） | 加 attribute |
| gRPC 桥接 RPC 模式 | `modes/rpc/rpc-mode.ts`（stdin/stdout JSONL） | sidecar/适配 |

> 注：上述模块路径来自对本仓库的探查（`packages/agent`、`packages/coding-agent`），实施前请以最新代码为准复核。

---

## 8. 分阶段交付

- **P0（MVP，验证隔离直觉）**：Gateway 薄壳（auth+路由+SSE）+ Scheduler 一会话一 Pod + 内核 RPC 直跑（工具暂留 Pod 内，**仅信任租户**）。NDJSON 落本地盘。
- **P1（安全门槛）**：接入 **microVM 执行层**（Kata）+ per-tenant egress + Secrets Broker JIT 凭据。**这是对外开放不可信租户的硬门槛。**
- **P2（可扩展）**：NDJSON 外置 S3 + Redis 路由 + 实例回收/恢复 + HPA。
- **P3（规模化 + 计量）**：OTel tenant 维度 → 计费/配额闭环；silo→bridge 分层；MCP 工具联邦。

---

## 9. 风险与证伪提醒

- ⚠ **不要在 P1 前对外开放不可信租户**——namespace 隔离不足以防代码逃逸（调研 3-0 证实）。
- ⚠ **Temporal 不“保证执行完成”**（调研 0-3 证伪），如引入须按“持久化+重放”心智设计补偿逻辑。
- ⚠ 勿照抄被证伪的 “Burn-After-Use”“SMTA 92%”“Cloudflare 每沙箱独立 VM” 等说法。
- ⚠ OTel GenAI semconv 仍处 Development 阶段，字段可能变动。
