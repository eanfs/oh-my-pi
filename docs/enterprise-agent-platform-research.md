# 基于 oh-my-pi 内核构建企业级多租户 AI Agent 平台 · 调研报告

> 研究方法：deep-research 工作流 — 5 个搜索角度并行 · 24 个来源 · 120 条声明 · 3 票对抗式验证（21 条确认 / 4 条证伪）
> 内核基线：基于对本仓库 `oh-my-pi`（Pi fork，TypeScript/Bun 单体仓库）的代码分析
> 生成日期：2026-06-09

---

## 0. 结论先行（TL;DR）

你设想的「**多实例部署 = 租户隔离**」本质是 SaaS 隔离模型里的 **Silo（筒仓）模型**。研究证实它**确实能消除 noisy-neighbor、把爆炸半径限定在单租户**，但有两个硬约束必须正视：

1. **Silo 在 ~1000 租户后成本与运维急剧劣化**（高置信）——纯 silo 不可持续，需要走 **分层隔离（silo / pool / bridge 混合）**。
2. **K8s namespace + 多实例 ≠ 安全隔离**。Agent 要跑 LLM 生成的不可信代码（内核里的 `bash`、`eval` Python/JS、`pi-iso` worktree），而 **Docker/容器共享宿主内核**，必须叠加**内核级隔离**（Firecracker microVM / gVisor / Kata Containers）。**这是本报告最关键的安全结论，也是 oh-my-pi 当前最大的缺口**——`pi-iso` 只做了文件系统隔离，没做内核隔离。

**推荐落地形态**：K8s 云原生 + 分层租户隔离 + Kata/microVM 跑不可信代码 + AI Gateway 做对外接口 + 会话状态外置 + OTel GenAI 语义约定做可观测与成本归因。

---

## 1. 多租户隔离架构（K8s vs Serverless vs 私有化）

### 1.1 三种隔离模型（已验证，置信度高）

| 模型 | 机制 | noisy-neighbor | 爆炸半径 | 成本 | 规模上限 |
|---|---|---|---|---|---|
| **Silo（你的方案）** | 每租户一套完整隔离栈 | ✅ 消除 | ✅ 限定单租户 | ❌ 高 | ⚠️ ~1000 租户后劣化 |
| **Pool** | 租户共享底层计算 | ❌ 引入 | ❌ 跨租户 | ✅ 最优 | ✅ 高，但无法用网络/IAM 边界隔离 |
| **Bridge（混合）** | 按租户分层（高价值 silo / 长尾 pool） | 可控 | 可控 | 平衡 | ✅ 推荐 |

> 来源：AWS SaaS Tenant Isolation Strategies 白皮书（primary，但被标记为 historical/较早）、Azure AKS 多租户架构文档（primary, 2026-05-12，3-0 验证）。佐证：kubernetes.io、vcluster、Northflank。

**关键被证实的事实**：
- **「Kubernetes 不保证完美安全的隔离」（3-0）**——namespace/RBAC/NetworkPolicy/ResourceQuota 只是软隔离基线。
- **AKS 区分 soft multitenancy（信任租户，如内部团队）vs hard multitenancy（不信任租户，如外部客户）**（3-0）——对外提供服务属于后者，必须硬隔离。
- **AKS 支持垂直分区/混合租户模型 + 基于 Kata Containers 的 Pod 沙箱**（3-0）。

### 1.2 三种部署形态对比

| 维度 | K8s 云原生 | 公有云 Serverless/容器实例 | 私有化 |
|---|---|---|---|
| 隔离粒度 | namespace→Pod→Kata/microVM 可分层 | 平台托管沙箱（如 E2B/Cloudflare） | 强隔离 + 离线 |
| noisy-neighbor 控制 | ResourceQuota + LimitRange + 专用节点池 | 平台保证 | 自控 |
| 冷启动 | Pod 秒级；microVM ~125ms | Firecracker ~80–200ms | — |
| 运维负担 | 高（自管） | 低 | 最高 |
| 合规 | 自证 | 平台带 PCI/ISO/HIPAA | 数据不出域 |
| 适配 oh-my-pi | ⭐ 最契合「多实例 = 一实例一会话/租户」 | 适合托管沙箱执行层 | 适合金融/政企交付 |

**建议**：控制面用 K8s；**不可信代码执行层用 microVM 沙箱**（自建 Kata/Firecracker，或买 E2B/类似 sandbox-as-a-service）。

---

## 2. Agent 编排引擎的可扩展性与可靠性

### 2.1 Durable Execution —— 但要去掉营销话术

> ⚠️ **被对抗验证“击杀”的声明，必须警惕**：
> - ❌ **Temporal「保证所有执行在故障后都能跑到完成」= 0-3 证伪**（过度宣称）。Temporal 提供的是**持久化状态 + 确定性重放（replay）**，不是“魔法保证完成”。
> - ✅ 真实可信：**「Temporal 工作流自动持久化并保持状态」（3-0）**、**「编排长任务 + human-in-the-loop」（3-0）**。

> 佐证：Diagrid 文章明确指出 **「checkpoints ≠ durable execution」**，LangGraph/CrewAI/Google ADK 等靠 checkpoint 的方案在生产 agent 工作流中“不够用”。Render、Temporal 官方亦有论述。

### 2.2 有状态会话与水平扩展

- 长任务/有状态会话需要**会话状态外置**：Redis vs StatefulSet vs 外部数据库各有取舍（dev.to 专文，blog 级）。
- **关键张力（开放问题）**：oh-my-pi 的 **NDJSON 追加日志树** 本身已经是**事件溯源（event-sourced）日志**——这与 durable execution 的 replay 模型天然同构。是否引入 Temporal、还是把 NDJSON 外置到对象存储 + 自建轻量恢复，是核心架构决策点（见 §6 / §8）。

---

## 3. 对外接口与 API 网关设计

### 3.1 传输层选型（GenAI 场景）

> Google Cloud《Engineering Transport Layers for GenAI》系统对比 REST / WebSocket / gRPC（blog）。

| 协议 | 适用 | 在 oh-my-pi 的落点 |
|---|---|---|
| **REST + SSE** | 单向流式输出（聊天补全） | 包装 Print 模式 / RPC 的流式事件 |
| **WebSocket** | 双向、可中断、steering | 最贴合 RPC 模式的 `steer`/`abort`/`follow_up` 50+ 命令 |
| **gRPC** | 内部服务间、低延迟 | 网关↔内核实例 |
| **MCP** | 工具/技能联邦化 | skills & tools 对外暴露/接入 |

### 3.2 AI Gateway（区别于传统 API Gateway）

> Kong、Apache APISIX（含 MCP-over-gateway）、Jimmy Song 深度文（均 blog 级）。

AI Gateway 在传统网关之上增加：**多模型路由/失败回退、token 级限流与配额、语义缓存、prompt 防护、按租户用量计量与计费、审计**。

- **计费**：已有「token billing system for AI agent」实践参考（dev.to, blog）。oh-my-pi 的 OTel 遥测可直接喂给计费。

---

## 4. 安全合规与可观测性

### 4.1 凭据与沙箱（高置信，且有证伪项）

✅ **已证实**：
- **「不可信代码必须内核级隔离，标准 Docker 不安全」（3-0）**。
- **「凭据/密钥须 per-task JIT 下发、短时有效」（3-0）**——这正是 oh-my-pi per-session 凭据 + key 轮换可强化的方向。
- **Firecracker ~125ms / gVisor / Kata AKS 沙箱可满足 PCI DSS / ISO 27001 / HIPAA**（3-0）。

> ⚠️ **三条被证伪、不要照抄的“最佳实践”**：
> - ❌ arXiv 2601.06627 的 **SMTA「92% 防御成功率」= 1-2 证伪**（数字不可靠，但其“按部门隔离 LLM 实例 + 上下文所有权边界”的定性思路 3-0 成立）。
> - ❌ 同文 **「Burn-After-Use 临时上下文用后即焚」= 0-3 证伪**（机制不成立，别当真）。
> - ❌ **「Cloudflare Sandbox SDK 每沙箱独立 VM（VM 而非容器）」= 0-3 证伪**——实际是 **Workers + Durable Objects + Containers** 组合（3-0），用容器不是 VM。

### 4.2 可观测性与成本归因（OTel GenAI，primary 来源）

- ✅ **OTel GenAI 语义约定标准化了 GenAI 遥测**（3-0）。⚠️ 注意该 semconv 仍处 **Development 阶段**（caveat）。
- ✅ **默认不采集 prompt/response 内容**（3-0）——隐私友好，合规默认安全。
- ✅ **OTel GenAI 指标可做 per-tenant 成本估算**（3-0）——直接支撑多租户成本归因 + 计费。

> 来源：opentelemetry.io GenAI Observability（primary）、Datadog LLM OTel semconv（blog）。

---

## 5. 参考架构：单机 CLI 内核 → 多租户云服务

业界把 CLI/单机 agent 内核产品化的成熟范式（均有来源佐证）：

- **Sandbox-as-a-Service**：**E2B 用 Firecracker 隔离每个 agent 沙箱，~80–200ms 冷启动，支持 BYOC 部署进客户云**（3-0；caveat：BYOC 目前**仅 AWS + GCP**）。
- **Cloudflare 路线**：**沙箱身份与路由由 Durable Objects 支撑**（3-0），SDK = Workers + Durable Objects + Containers。
- **嵌入式参考**：有人专门写了「把 Codex / opencode / Pi 这类 agent 嵌入 SaaS」的文章（codex.danielvaughan.com，blog）——与 oh-my-pi 是 Pi fork 高度相关，建议精读。

---

## 6. 映射回 oh-my-pi 内核：复用 vs 增强

| 内核已有原语 | 复用方式 | 必须增强 |
|---|---|---|
| **RPC 模式**（JSONL, 50+ 命令, 流式 + 扩展 UI 桥接） | ⭐ 直接做对外接口底座：每实例跑一个 RPC 内核，网关用 WebSocket/SSE 包它 | 加 backpressure、超时、断线重连；命令鉴权 |
| **pi-iso**（worktree/overlayfs/ProjFS 文件系统隔离） | 保留做工作区隔离 | ⚠️ **最大缺口：叠加内核级隔离**（Kata/Firecracker/gVisor）才能安全跑不可信 `bash`/`eval` |
| **NDJSON 会话日志树** | ⭐ 本身是 event-sourced 日志，天然支持 replay 恢复 | 外置到对象存储/DB；决策是否上 durable execution（Temporal）还是自建轻量重放（§2.2 开放问题） |
| **injectable fetch transport** | ⭐ 按租户路由网络：强制走 per-tenant egress 代理 | 接入密钥经纪（Vault），出站审计 |
| **per-session 凭据 + key 轮换** | ⭐ 已贴合「per-task JIT 短时凭据」最佳实践 | 改为由 secrets broker 动态签发、短 TTL、用后回收 |
| **OpenTelemetry 遥测** | ⭐ 直接对齐 **OTel GenAI semconv** | 加 tenant_id 维度 → 成本归因 + 计费；默认不记 prompt 内容（合规） |
| **capability/skills 发现** | 对外用 **MCP** 暴露/联邦 skills 与 tools | 多租户 skill 命名空间 + 权限 |
| **Task 子代理（进程内 + 有界并发）** | 复用编排 | 跨实例时改为分布式调度 + 沙箱化执行 |

---

## 7. 分阶段落地路线（建议）

1. **MVP（验证 silo 模型）**：K8s 上「一会话一 Pod」跑 RPC 内核 + 前置 AI Gateway（auth/租户身份/限流/审计），NDJSON 仍落本地盘。验证多实例隔离直觉。
2. **安全加固**：给执行层套 **Kata/microVM**（这是上生产的硬门槛）；injectable fetch 接 egress 代理；per-session 凭据改 Vault JIT 签发。
3. **可扩展**：NDJSON 外置对象存储；引入会话状态外置 + 恢复；评估 durable execution（记住 Temporal 的真实能力是持久化+重放，不是“保证完成”）。
4. **规模化**：从纯 silo 演进到 **bridge 分层隔离**（高价值租户 silo、长尾 pool），突破 ~1000 租户天花板。
5. **可观测/计费**：OTel GenAI semconv 全量打 tenant 维度 → 成本归因 + 用量计费。

---

## 8. 开放问题（需拍板）

1. **silo↔pool 切换阈值**：租户数/客单价临界点在哪？
2. **pi-iso 与 microVM 如何分层**：是 microVM 内再跑 pi-iso worktree，还是 pi-iso 让位给沙箱平台？
3. **NDJSON vs Temporal**：自建事件溯源重放，还是引入工作流引擎？（二者语义有重叠）
4. **网关选型**：Kong / APISIX / 自建？是否需要原生 MCP 支持？

---

## 附录 A：验证质量说明

- **5 角度 / 24 来源 / 120 声明 → 验证 25 条 → 21 确认、4 证伪**。
- **证伪项已在正文用 ❌ 标出**（Temporal 完成保证、SMTA 92%、Burn-After-Use、Cloudflare VM 说法），请勿采信。
- **Caveats**：AWS 白皮书偏旧；OTel GenAI semconv 仍 Development；E2B BYOC 仅 AWS+GCP。

## 附录 B：来源清单

### Primary（一手/官方）
- AWS — SaaS Tenant Isolation Strategies 白皮书：https://d1.awsstatic.com/whitepapers/saas-tenant-isolation-strategies/saas-tenant-isolation-strategies.pdf
- Azure — AKS 多租户架构：https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/service/aks
- Temporal — AI 解决方案：https://temporal.io/solutions/ai
- OpenTelemetry — GenAI Observability：https://opentelemetry.io/blog/2026/genai-observability/
- E2B：https://e2b.dev/
- Cloudflare — Sandbox 架构：https://developers.cloudflare.com/sandbox/concepts/architecture/
- arXiv 2601.06627（SMTA，部分声明证伪）：https://arxiv.org/pdf/2601.06627

### Secondary / Blog
- 多租户 AI agent 基础设施如何真正扩展：https://medium.com/@vamshidhar.pandrapagada/how-to-deploy-multi-tenant-ai-agent-infrastructure-that-actually-scales-433f44515837
- Blaxel — Tenant Isolation：https://blaxel.ai/blog/tenant-isolation
- Blaxel — Multi-tenant Isolation for AI Agents：https://blaxel.ai/blog/multi-tenant-isolation-ai-agents
- Diagrid — Checkpoints ≠ Durable Execution：https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows
- Render — Durable Workflow Platforms for AI Agents：https://render.com/articles/durable-workflow-platforms-ai-agents-llm-workloads
- 长任务 Agent 状态管理：Redis vs StatefulSets vs 外部 DB：https://dev.to/inboryn_99399f96579fcd705/state-management-patterns-for-long-running-ai-agents-redis-vs-statefulsets-vs-external-databases-39c5
- Jimmy Song — AI Gateway 深度解析：https://jimmysong.io/blog/ai-gateway-in-depth/
- Kong — API Gateway vs AI Gateway：https://konghq.com/blog/learning-center/api-gateway-vs--ai-gateway
- Apache APISIX — MCP Protocol AI Gateway：https://apisix.apache.org/learning-center/mcp-protocol-ai-gateway/
- Google Cloud — GenAI 传输层工程（REST/WebSocket/gRPC）：https://medium.com/google-cloud/engineering-transport-layers-for-genai-rest-websockets-grpc-and-beyond-90a866da39c8
- 最佳 MCP 网关与 AI Agent 安全工具：https://www.integrate.io/blog/best-mcp-gateways-and-ai-agent-security-tools/
- AI Agent 的 Token 计费系统实践：https://dev.to/tejakummarikuntla/i-built-a-token-billing-system-for-my-ai-agent-heres-how-it-works-dl2
- Datadog — LLM OTel 语义约定：https://www.datadoghq.com/blog/llm-otel-semantic-convention/
- TrueFoundry — Claude Code Sandboxing：https://www.truefoundry.com/blog/claude-code-sandboxing
- BeyondScale — AI Agent Sandboxing 企业安全指南：https://beyondscale.tech/blog/ai-agent-sandboxing-enterprise-security-guide
- 把 AI Agents（Codex/opencode/Pi）嵌入 SaaS：https://codex.danielvaughan.com/2026/04/07/embedding-ai-agents-saas-codex-opencode-pi/
- Manveer — AI Agent Sandboxing 指南：https://manveerc.substack.com/p/ai-agent-sandboxing-guide
