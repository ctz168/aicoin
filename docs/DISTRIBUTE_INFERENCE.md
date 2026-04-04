# AICoin 混合分布式推理系统设计文档

> **版本**: 2.0.0
> **模块**: `core/hybrid_inference.py`
> **更新日期**: 2025

---

## 目录

1. [核心设计原则](#1-核心设计原则)
2. [三大核心问题与解决方案](#2-三大核心问题与解决方案)
3. [分级冗余策略 (HYBRID_CONFIG)](#3-分级冗余策略-hybrid_config)
4. [热备机制](#4-热备机制)
5. [架构图](#5-架构图)
6. [优雅降级流程](#6-优雅降级流程)
7. [核心组件说明](#7-核心组件说明)
8. [模型目录 (MODEL_CATALOG)](#8-模型目录-model_catalog)
9. [使用示例](#9-使用示例)

---

## 1. 核心设计原则

AICoin 的混合分布式推理系统面向去中心化 AI 算力挖矿网络设计，承载着在不可靠节点环境下稳定提供推理服务的关键使命。设计遵循以下三大原则：

### 1.1 适度冗余保障稳定性，不搞到冗余又浪费

去中心化网络中的节点随时可能掉线、宕机或延迟飙升。纯靠"多副本"来保障可靠性会导致大量算力闲置——这正是传统分布式推理系统常犯的错误。

本系统采用 **分级冗余** 策略：根据模型规模决定冗余级别。小模型（≤8B）只需要 1 个活跃节点 + 1 个热备节点；大模型（>120B）才需要 8 个活跃节点 + 2 个热备节点。热备节点预加载权重但不主动推理，在主节点故障时秒级接管，既保障了可用性，又避免了算力浪费。

### 1.2 大模型跨节点分片支持

单节点 VRAM 容量有限（通常 24-80GB），无法加载 70B+ 参数的大模型。本系统实现了三种模型并行策略：

- **张量并行 (TENSOR_PARALLEL)**：将同层权重均匀切分到多个节点，适合 16B-72B 模型
- **流水线并行 (PIPELINE_PARALLEL)**：按 Transformer 层分段到多个节点，适合 73B-120B 模型
- **混合并行 (HYBRID_PARALLEL)**：两者结合，适合 >120B 的超大模型

### 1.3 节点动态波动下的稳定性

去中心化网络中节点的加入和退出是常态。系统通过三层防护确保推理服务在节点波动下持续可用：

1. **熔断器 (CircuitBreaker)**：自动隔离故障节点，防止级联失败
2. **会话亲和性 (SessionAffinity)**：对话级别的路由一致性，节点切换时平滑迁移
3. **集群自动重组 (ClusterManager)**：节点离开时自动评估是否需要重建集群

---

## 2. 三大核心问题与解决方案

### 问题1: 节点动态波动 (Churn)

去中心化网络中，矿工节点可能因网络断连、硬件故障、主动下线等原因随时离开。如果调度器不做处理，会导致请求路由到已离线的节点，造成推理失败。

#### 2.1.1 熔断器 (CircuitBreaker)

熔断器采用经典的三态状态机模型，自动隔离连续失败的节点：

```
CLOSED (正常放行)
  │
  │ 连续失败 ≥ 5 次
  ▼
OPEN (熔断拒绝)
  │
  │ 等待 30 秒恢复期
  ▼
HALF_OPEN (试探性放行, 最多 3 次请求)
  │
  ├─ 探测成功 ≥ 2 次 → CLOSED (恢复正常)
  │
  └─ 探测失败 → OPEN (重新熔断)
```

**关键参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `failure_threshold` | 5 | 连续失败多少次触发熔断 |
| `recovery_timeout` | 30s | 熔断后多久进入半开探测 |
| `half_open_max_calls` | 3 | 半开状态下允许的最大试探请求数 |
| `success_threshold` | 2 | 半开状态下恢复所需的连续成功数 |

**核心代码路径**：
- `CircuitBreaker.is_available(node_id)` — 调度前检查节点是否可用
- `CircuitBreaker.record_success(node_id)` — 记录成功，重置失败计数
- `CircuitBreaker.record_failure(node_id)` — 记录失败，可能触发熔断

#### 2.1.2 会话亲和性 (SessionAffinity)

同一用户的对话请求应路由到相同节点，避免每次切换节点导致上下文重建的开销。会话亲和性管理器支持三种策略：

| 策略 | 说明 | 适用场景 |
|------|------|---------|
| `STRONG` | 仅在节点故障时才切换路由 | 多轮对话、长上下文场景 |
| `WEAK` | 每 10 次请求重新评估是否切换 | 独立请求、一般场景 |
| `NONE` | 每次请求都重新选择节点 | 无状态推理 |

**工作流程**：
1. 请求到达时，通过 `session_key`（如 `user_id:conversation_id`）查询已绑定的节点
2. 如果有绑定且未过期（TTL = 600s），优先路由到绑定节点
3. 绑定节点不可用时（被熔断/离线），自动解绑并迁移到新节点
4. 节点主动离线时，通过 `unbind_by_node()` 批量迁移所有受影响会话

#### 2.1.3 集群自动重组 (ClusterManager.handle_node_leave)

当节点离开时，集群管理器会自动评估影响并执行重组：

```
节点 node_x 离开
  │
  ├─ 遍历 node_x 参与的所有集群
  │   │
  │   ├─ 从集群成员列表中移除 node_x
  │   │
  │   ├─ 检查: 剩余节点数 ≥ min_nodes ?
  │   │   │
  │   │   ├─ YES → 集群继续运行
  │   │   │        └─ 如果 node_x 是 leader，选举新 leader
  │   │   │
  │   │   └─ NO → 标记集群为 inactive
  │   │          └─ 下次请求到来时自动组建新集群
  │   │
  │   └─ 迁移受影响会话的亲和性绑定
  │
  └─ 完成
```

对于不同级别模型的差异化处理：
- **TINY / SMALL 模型**：单节点即可推理，直接迁移到其他可用节点
- **MEDIUM+ 模型**：并行推理不能缺节点，必须重新组建完整集群

---

### 问题2: 算力极度冗余 (Over-provisioning)

在去中心化网络中，可能出现某个模型有 10 个可用节点，但实际只需要 1 个。如果不加控制地组建大集群，会造成严重的算力浪费。

#### 2.2.1 STANDALONE 模式: 小模型只用 1 个活跃节点

对于 TINY（≤8B）和 SMALL（9-15B）模型，采用 STANDALONE 模式：

```python
# 集群组建逻辑（关键代码）
if mode == InferenceMode.STANDALONE:
    selected = sorted_nodes[:1]  # 只选 1 个节点，不浪费
```

这意味着即使有 20 个节点可用，也只会选择综合评分最高的 1 个节点进行推理。额外的节点不会被纳入集群，可以服务于其他模型请求。

#### 2.2.2 集群效率监控 (NodeCluster.calculate_efficiency)

每个集群实时计算效率得分（0-1），综合考虑三个维度：

| 维度 | 权重 | 计算方式 |
|------|------|---------|
| 节点利用率 | 30% | `min_nodes / actual_nodes` |
| 请求成功率 | 40% | `(total - failures) / total` |
| 负载均衡度 | 30% | 基于 VRAM 可用量的标准差 |

**效率过低的信号**：
- 效率 < 0.3 → 集群存在过度配置（比如 5 个节点但只需要 2 个）
- 系统会建议缩容或回收

#### 2.2.3 自适应缩容 (空闲集群回收)

集群管理器在后台运行 GC 线程（每 60 秒检查一次），自动回收满足以下条件的集群：

- `is_active == True`（仍处于活跃状态）
- 空闲时间 > 300 秒（5 分钟无任何请求）
- 无历史请求记录

此外，`HybridScheduler.adaptive_pool_resize()` 方法可以主动诊断某个模型的节点配置是否合理，并给出 `shrink` / `keep` / `expand` 建议。

---

### 问题3: 大模型单节点装不下 (Model Parallelism)

70B 参数的模型在 FP16 精度下需要约 140GB 显存，远超单张消费级显卡的容量。必须将模型分片到多个节点协同推理。

#### 2.3.1 张量并行 (TENSOR_PARALLEL)

**适用模型**: 16B - 72B

**原理**: 将 Transformer 同一层的权重矩阵按列切分，分配到多个节点。每个节点持有权重的一部分，前向传播时通过 AllReduce 同步中间结果。

```
输入 token
  │
  ├─ 节点0: 权重分片 [shard_0] → 部分输出
  ├─ 节点1: 权重分片 [shard_1] → 部分输出
  └─ 节点2: 权重分片 [shard_2] → 部分输出
  │
  ▼ AllReduce (求和)
完整输出
```

**集群组建时的分片分配**：
```python
# 均匀分配权重分片到每个节点
shard_per_node = math.ceil(total_weight / len(selected))
shard_start = rank * shard_per_node
shard_ids = list(range(shard_start, min(shard_start + shard_per_node, total_weight)))
```

**节点选择策略**: 优先选择网络延迟最低的节点对（张量并行对通信延迟敏感）。

#### 2.3.2 流水线并行 (PIPELINE_PARALLEL)

**适用模型**: 73B - 120B

**原理**: 将 Transformer 的不同层分配到不同节点。节点0处理第0-19层，节点1处理第20-39层，以此类推。前一个节点的输出作为后一个节点的输入。

```
输入 token
  │
  ▼
节点0: 层 [0, 20) → 中间隐藏状态
  │
  ▼
节点1: 层 [20, 40) → 中间隐藏状态
  │
  ▼
节点2: 层 [40, 60) → 中间隐藏状态
  │
  ▼
节点3: 层 [60, 80) → 最终输出
```

**集群组建时的层分配**：
```python
# 按层均匀分配到每个节点
layer_total = int(profile.params_b * 10)  # 粗略估计层数
layers_per_node = math.ceil(layer_total / len(selected))
layer_start = rank * layers_per_node
layer_end = min(layer_start + layers_per_node, layer_total)
```

**节点选择策略**: 优先选择算力最强的节点（流水线并行是计算密集型）。

#### 2.3.3 混合并行 (HYBRID_PARALLEL)

**适用模型**: >120B

**原理**: 张量并行 + 流水线并行结合。每个流水线阶段内部使用张量并行，多个流水线阶段串联执行。

```
输入 token
  │
  ▼
阶段0: 节点组 [0,1,2] 张量并行处理层 [0, 25)
  │
  ▼
阶段1: 节点组 [3,4,5] 张量并行处理层 [25, 50)
  │
  ▼
阶段2: 节点组 [6,7]  张量并行处理层 [50, 80)
  │
  ▼
最终输出
```

---

## 3. 分级冗余策略 (HYBRID_CONFIG)

分级冗余是本系统最核心的设计决策。不同规模的模型对冗余的需求完全不同：

| Tier | 模型大小 | 推理模式 | 活跃节点 | 热备节点 | 总需求 | 降级下限 | 说明 |
|------|---------|---------|---------|---------|-------|---------|------|
| **TINY** | ≤8B | STANDALONE | 1 | 1 | **2** | 1 | 1推理 + 1热备 |
| **SMALL** | 9-15B | STANDALONE | 1 | 1 | **2** | 1 | 1推理 + 1热备 |
| **MEDIUM** | 16-40B | TENSOR_PARALLEL | 2 | 1 | **3** | 2 | 2张量并行 + 1热备 |
| **LARGE** | 41-72B | TENSOR_PARALLEL | 4 | 1 | **5** | 3 | 4张量并行 + 1热备 |
| **XLARGE** | 73-120B | PIPELINE_PARALLEL | 4 | 1 | **5** | 3 | 4流水线并行 + 1热备 |
| **MASSIVE** | >120B | HYBRID_PARALLEL | 8 | 2 | **10** | 6 | 8混合并行 + 2热备 |

**设计理由**：

- **TINY / SMALL**: 模型小，单节点即可推理。1个热备节点确保故障时秒级切换。
- **MEDIUM**: 需要2个节点做张量并行，再加1个热备节点。热备节点也预加载完整权重，可在任一活跃节点故障时替补。
- **LARGE**: 需要4路张量并行，通信开销大。1个热备节点覆盖最可能出现的单节点故障。
- **XLARGE**: 流水线并行，4个阶段串联。同样只需1个热备。
- **MASSIVE**: 8个节点的混合并行集群，任意节点故障的概率更高（P(at least 1 failure) ≈ 8x 单节点故障率），因此配备 **2个热备节点**。

---

## 4. 热备机制

### 4.1 热备节点的工作方式

热备节点是本系统实现"适度冗余"的关键创新。与传统的"多副本同时推理"不同：

```
传统冗余 (浪费算力):
  节点A → 推理中 (输出结果1)
  节点B → 推理中 (输出结果2)  ← 重复计算，浪费!
  节点C → 推理中 (输出结果3)  ← 重复计算，浪费!

热备机制 (算力零浪费):
  活跃节点 → 推理中 (输出结果)
  热备节点 → 待命中 (权重已加载, 不参与推理)
                    │
                    └─ 活跃节点故障? → 秒级接管, 无需加载权重
```

**热备节点的特点**：
1. **预加载权重**: 热备节点在集群组建时就加载了完整的模型权重
2. **不主动推理**: 热备节点处于待命状态，不接受正常推理请求
3. **秒级接管**: 主节点故障时无需等待权重加载，直接开始推理
4. **可感知故障转移**: 系统会记录 `standby_activated` 标志，便于监控和告警

### 4.2 故障转移流程

```
活跃节点推理
  │
  ├─ 成功 → 返回结果
  │
  └─ 失败
      │
      ├─ 记录失败到熔断器
      │
      ├─ STANDALONE模式:
      │   └─ 直接切换到热备节点 → 成功? → 返回结果 + standby_activated=True
      │                                       └─ 失败? → 集群故障处理
      │
      └─ 并行模式:
          ├─ 检查热备节点数量是否足够
          │   │
          │   ├─ 够 → 用热备替换失败节点 → 重新执行并行推理
          │   │
          │   └─ 不够 → 集群故障处理
          │
          └─ 集群故障处理:
              ├─ 将失败节点从集群移除
              ├─ 剩余节点 < min_nodes? → 标记集群不活跃
              └─ 下次请求自动组建新集群
```

### 4.3 资源不足时的优雅降级

当可用节点数不满足 `total_required`（活跃 + 热备）时，系统不会直接拒绝请求，而是执行优雅降级：

```
可用节点数 = N
  │
  ├─ N ≥ active + standby → 全量配置
  │
  ├─ active ≤ N < active + standby → 砍热备, 保留全量活跃节点
  │   例: LARGE 级别需要 5 节点 (4+1), 只有 4 节点可用 → 4活跃+0热备
  │
  ├─ degrade_to_active ≤ N < active → 减少活跃节点
  │   例: LARGE 级别 degrade_to_active=3, 只有 3 节点 → 3活跃+0热备
  │
  └─ N < degrade_to_active → 无法运行, 返回错误
```

**降级优先级**: 先砍热备 → 再砍并行度 → 最后才放弃

---

## 5. 架构图

### 5.1 系统整体架构

```
                          ┌─────────────────────────────────────────┐
                          │            请求入口                      │
                          │   (API Gateway / 调度请求)               │
                          └──────────────┬──────────────────────────┘
                                         │
                                         ▼
                          ┌─────────────────────────────────────────┐
                          │     HybridScheduler.schedule()          │
                          │     HybridScheduler.route_hybrid()      │
                          │              混合调度器                  │
                          └──────────────┬──────────────────────────┘
                                         │
                          ┌──────────────┼──────────────────────────┐
                          │              │                          │
                          ▼              ▼                          ▼
                 ┌────────────┐  ┌──────────────┐  ┌──────────────────┐
                 │ ModelProfile│  │SessionAffinity│  │  ClusterManager  │
                 │  模型分级    │  │  会话亲和性    │  │   集群管理器      │
                 │             │  │              │  │                  │
                 │ · 参数量     │  │ · STRONG     │  │ · 集群组建/复用   │
                 │ · Tier分级   │  │ · WEAK       │  │ · 节点变动处理    │
                 │ · 推理模式   │  │ · NONE       │  │ · 空闲集群回收    │
                 │ · 最小节点   │  │ · TTL过期    │  │ · 效率监控        │
                 │ · VRAM需求   │  │ · 批量迁移    │  │ · 熔断保护        │
                 └────────────┘  └──────────────┘  └────────┬─────────┘
                                                              │
                                                    ┌─────────┴─────────┐
                                                    │                   │
                                                    ▼                   ▼
                                           ┌──────────────┐   ┌──────────────┐
                                           │CircuitBreaker │   │ NodeCluster   │
                                           │   熔断器      │   │   推理集群     │
                                           │              │   │              │
                                           │ · CLOSED     │   │ · Leader节点   │
                                           │ · OPEN       │   │ · Worker节点   │
                                           │ · HALF_OPEN  │   │ · 张量分片     │
                                           │              │   │ · 层分配       │
                                           │ · 失败阈值=5 │   │ · 效率计算     │
                                           │ · 恢复超时30s│   │ · 健康检查     │
                                           └──────────────┘   └──────────────┘
```

### 5.2 请求调度流程

```
请求入口
  │
  ▼
HybridScheduler.route_hybrid()
  │
  ├─ Step1: ModelProfile.from_catalog() → 解析模型分级
  │   └─ 查询 MODEL_CATALOG 或自动估算参数量 → 确定 tier/mode/min_nodes
  │
  ├─ Step2: HYBRID_CONFIG.get_policy(tier) → 获取冗余策略
  │   └─ 确定 active_nodes + standby_count
  │
  ├─ Step3: SessionAffinity.get_affinity() → 会话亲和性检查
  │   └─ 已绑定? → preferred_node_id = 绑定的节点
  │
  ├─ Step4: ClusterManager.find_or_create_cluster() → 集群组建/复用
  │   │
  │   ├─ 过滤熔断节点
  │   ├─ 尝试复用已有健康集群
  │   └─ 不满足? → 组建新集群
  │       │
  │       ├─ STANDALONE → 选最优单节点
  │       ├─ TENSOR_PARALLEL → 选延迟最低的 N 节点
  │       ├─ PIPELINE_PARALLEL → 选算力最强的 N 节点
  │       └─ HYBRID_PARALLEL → 综合 N 节点
  │
  ├─ Step5: _prepare_node_list() → 构建节点列表 (标记 active/standby)
  │
  ├─ Step6: _execute_on_cluster() → 执行推理
  │   │
  │   ├─ 在活跃节点上执行
  │   │   │
  │   │   ├─ 成功 → 返回结果
  │   │   │
  │   │   └─ 失败 → 检查熔断器 → 尝试热备切换
  │   │       │
  │   │       ├─ STANDALONE → 热备节点直接接管
  │   │       └─ 并行模式 → 热备替换失败节点 → 重试
  │   │
  │   └─ 全部失败 → 集群标记不活跃 → 等待下次重建
  │
  └─ Step7: 记录结果
      ├─ 更新熔断器 (success/failure)
      ├─ 更新会话亲和性绑定
      └─ 更新统计计数器
```

### 5.3 熔断器状态机

```
                    ┌─────────────┐
         ┌─────────│   CLOSED    │◄──────────┐
         │         │  (正常放行)  │           │
         │         └──────┬──────┘           │
         │                │                  │
         │    连续失败 ≥ 5次                  │ 半开探测成功 ≥ 2次
         │                │                  │
         │                ▼                  │
         │         ┌─────────────┐           │
         │    ┌────│    OPEN     │           │
         │    │    │  (熔断拒绝)  │           │
         │    │    └──────┬──────┘           │
         │    │           │                  │
         │    │    等待 30秒                   │
         │    │           │                  │
         │    │           ▼                  │
         │    │    ┌─────────────┐           │
         │    └────│  HALF_OPEN  │───────────┘
         │         │ (试探放行)   │
         │         │ 最多3次请求   │
         │         └──────┬──────┘
         │                │
         │     探测失败     │
         └────────────────┘
```

---

## 6. 优雅降级流程

`TierRedundancyPolicy.graceful_degrade(available)` 是优雅降级的核心逻辑。当可用节点数不足时，系统按以下优先级逐步降级：

### 6.1 降级决策树

```
输入: available = 当前可用节点数
配置: active_nodes, standby_count, degrade_to_active

┌─ available ≥ active + standby ?
│   YES → return (active_nodes, standby_count)     ← 全量配置
│   NO  ▼
│
├─ available ≥ active_nodes ?
│   YES → return (active_nodes, 0)                  ← 砍掉热备，保留全量活跃
│   NO  ▼
│
├─ available ≥ degrade_to_active ?
│   YES → return (available, 0)                     ← 减少并行度，砍掉热备
│   NO  ▼
│
└─ return (0, 0)                                    ← 资源严重不足，无法运行
```

### 6.2 降级示例

以 **LARGE** 级别（如 `aicoin-llama-70b`）为例，配置为 `active=4, standby=1, degrade_to=3`：

| 可用节点数 | 决策 | 实际配置 | 说明 |
|-----------|------|---------|------|
| 5 | 全量 | 4活跃 + 1热备 | 理想状态 |
| 4 | 砍热备 | 4活跃 + 0热备 | 无冗余，但推理能力完整 |
| 3 | 降级并行 | 3活跃 + 0热备 | 并行度下降，推理速度变慢 |
| 2 | 降级并行 | 2活跃 + 0热备 | 低于 degrade_to，由具体实现决定 |
| 1 | 无法运行 | 0活跃 + 0热备 | 节点严重不足，返回错误 |

### 6.3 降级对推理的影响

```
全量配置 (5节点):
  [活跃0][活跃1][活跃2][活跃3] + [热备]
  → 4路张量并行，推理速度 1x，有冗余

砍热备 (4节点):
  [活跃0][活跃1][活跃2][活跃3]
  → 4路张量并行，推理速度 1x，无冗余 (风险略高)

降级并行 (3节点):
  [活跃0][活跃1][活跃2]
  → 3路张量并行，推理速度 ~0.75x，无冗余

无法运行 (1-2节点):
  → 返回错误，等待更多节点上线
```

---

## 7. 核心组件说明

### 7.1 ModelProfile — 模型画像

**职责**: 从 `MODEL_CATALOG` 查找或自动估算模型特征，为调度决策提供数据支撑。

**关键字段**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | str | 模型名称 |
| `params_b` | float | 参数量（十亿） |
| `tier` | ModelTier | 分级 (TINY/SMALL/MEDIUM/LARGE/XLARGE/MASSIVE) |
| `preferred_mode` | InferenceMode | 推荐推理模式 |
| `min_nodes` | int | 最小活跃节点数 |
| `min_vram_gb` | float | 单节点最小 VRAM 需求 |
| `weight_gb` | float | 模型权重大小 (FP16) |
| `estimated_latency_factor` | float | 相对延迟系数 (7B=1.0) |

**自动估算**: 对于未在 `MODEL_CATALOG` 中注册的模型，从模型名称中提取参数量（如 `llama-70b` → 70B），自动分级。

### 7.2 CircuitBreaker — 熔断器

**职责**: 自动隔离故障节点，防止级联失败。

**状态机**: `CLOSED` → `OPEN` → `HALF_OPEN` → `CLOSED`（详见第5.3节）

**线程安全**: 使用 `threading.RLock` 保护所有内部状态。

**关键方法**:
- `is_available(node_id)` → 调度前检查
- `record_success(node_id)` → 记录成功
- `record_failure(node_id)` → 记录失败
- `get_state(node_id)` → 查询状态
- `reset(node_id)` → 手动重置

### 7.3 SessionAffinity — 会话亲和性管理器

**职责**: 确保同一会话的请求路由到相同节点，支持节点故障时的平滑迁移。

**策略枚举**: `STRONG` / `WEAK` / `NONE`

**关键参数**:
- `max_session_ttl`: 会话绑定有效期（默认 600s）
- `weak_recheck_interval`: 弱亲和性重新评估间隔（默认 10 次请求）
- `max_sessions`: 最大会话数（默认 100,000，超出后淘汰最旧会话）

**关键方法**:
- `get_affinity(session_key, model_name)` → 查询绑定
- `bind(session_key, node_id, model_name)` → 创建绑定
- `unbind_by_node(node_id)` → 节点下线时批量迁移

### 7.4 NodeCluster / NodeSlot — 推理集群

**NodeSlot** — 集群中单个节点的槽位描述：

| 字段 | 说明 |
|------|------|
| `node_id` | 节点 ID |
| `rank` | 在集群中的序号（0 = leader） |
| `role` | "leader" 或 "worker" |
| `assigned_shards` | 张量并行时分配的权重分片 ID |
| `assigned_layers` | 流水线并行时分配的层范围 (start, end) |
| `vram_available_gb` | 节点可用 VRAM |

**NodeCluster** — 多个 NodeSlot 组成的推理协作单元：

| 能力 | 方法 |
|------|------|
| 健康检查 | `is_healthy()` — 节点数 ≥ min_nodes |
| 空闲检测 | `is_idle()` — 无请求超过 300s |
| 效率计算 | `calculate_efficiency()` — 综合利用率/成功率/均衡度 |
| 结果记录 | `record_request(success)` |
| 序列化 | `to_dict()` |

### 7.5 ClusterManager — 集群管理器

**职责**: 自动组建、维护和回收推理集群，是整个系统的集群生命周期管理中心。

**核心能力**:

| 能力 | 方法 | 说明 |
|------|------|------|
| 集群查找/创建 | `find_or_create_cluster()` | 复用已有集群或组建新集群 |
| 节点离开处理 | `handle_node_leave()` | 评估影响，自动重组 |
| 节点加入处理 | `handle_node_join()` | 检查是否可重建不活跃集群 |
| 空闲回收 | `_reclaim_idle_clusters()` | 后台线程定期清理 |
| 统计查询 | `get_stats()` | 返回活跃集群数/按模式统计等 |

**集群组建策略**（`_form_cluster`）：
1. 综合评分排序节点：VRAM(40%) + 负载(30%) + 延迟(30%)
2. 如果有亲和性首选节点，排到最前
3. 按推理模式选择不同数量的节点

### 7.6 HybridScheduler — 混合调度器

**职责**: 统一调度入口，串联所有组件完成一次推理请求的完整生命周期。

**两种调度接口**:

| 方法 | 说明 |
|------|------|
| `schedule()` | 基础调度，侧重集群组建和基本故障转移 |
| `route_hybrid()` | 完整调度，增加分级冗余策略和热备切换 |

**完整调度流程**（`route_hybrid`）:
1. 解析模型 → `ModelProfile`
2. 查询冗余策略 → `TierRedundancyPolicy`
3. 检查会话亲和性 → `SessionAffinity`
4. 组建/复用集群 → `ClusterManager`
5. 构建节点列表（含热备标记）→ `_prepare_node_list`
6. 执行推理（含故障转移）→ `_execute_on_cluster`
7. 记录结果 → 更新熔断器 + 亲和性 + 统计

**自适应优化**: `adaptive_pool_resize()` 诊断模型节点配置，给出缩容/扩容建议。

**诊断接口**: `diagnose()` 生成详细的系统健康状态报告，包括问题列表和优化建议。

### 7.7 HYBRID_CONFIG / TierRedundancyPolicy — 全局配置

**HYBRID_CONFIG** — 所有可调参数的单一入口：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `tier_policies` | 6级策略 | 每个Tier的冗余配置 |
| `standby_warmup_timeout` | 60s | 热备预热超时 |
| `failover_timeout` | 5s | 故障转移超时 |
| `cluster_idle_timeout` | 300s | 集群空闲回收超时 |
| `gc_check_interval` | 60s | GC检查间隔 |
| `cb_failure_threshold` | 5 | 熔断失败阈值 |
| `cb_recovery_timeout` | 30s | 熔断恢复时间 |
| `affinity_strategy` | "weak" | 亲和性策略 |
| `affinity_ttl` | 600s | 亲和性TTL |

**TierRedundancyPolicy** — 单个Tier的冗余策略：

| 字段 | 说明 |
|------|------|
| `active_nodes` | 正常推理所需节点 |
| `standby_count` | 热备节点数 |
| `degrade_to_active` | 降级下限 |
| `total_required` | `active + standby` (计算属性) |
| `graceful_degrade(n)` | 优雅降级计算方法 |

---

## 8. 模型目录 (MODEL_CATALOG)

系统内置了以下模型的配置，无需手动配置即可直接使用：

### TINY 级 (≤8B) — STANDALONE 模式

| 模型名称 | 参数量 | 最小VRAM | 权重大小 | 最小节点 |
|---------|--------|---------|---------|---------|
| `qwen2.5-0.5b` | 0.5B | 2 GB | 1 GB | 1 |
| `qwen2.5-1.5b` | 1.5B | 3 GB | 3 GB | 1 |
| `qwen2.5-3b` | 3B | 5 GB | 6 GB | 1 |
| `aicoin-llama-7b` | 7B | 8 GB | 14 GB | 1 |
| `aicoin-mistral-7b` | 7B | 8 GB | 14 GB | 1 |
| `qwen2.5-7b` | 7B | 8 GB | 14 GB | 1 |
| `llama-3-8b` | 8B | 10 GB | 16 GB | 1 |

### SMALL 级 (9-15B) — STANDALONE 模式

| 模型名称 | 参数量 | 最小VRAM | 权重大小 | 最小节点 |
|---------|--------|---------|---------|---------|
| `aicoin-llama-13b` | 13B | 16 GB | 26 GB | 1 |
| `qwen2.5-14b` | 14B | 16 GB | 28 GB | 1 |

### MEDIUM 级 (16-40B) — TENSOR_PARALLEL 模式

| 模型名称 | 参数量 | 最小VRAM | 权重大小 | 最小节点 |
|---------|--------|---------|---------|---------|
| `qwen2.5-32b` | 32B | 24 GB | 64 GB | 2 |
| `aicoin-coder-34b` | 34B | 24 GB | 68 GB | 2 |

### LARGE 级 (41-72B) — TENSOR_PARALLEL 模式

| 模型名称 | 参数量 | 最小VRAM | 权重大小 | 最小节点 |
|---------|--------|---------|---------|---------|
| `aicoin-llama-70b` | 70B | 24 GB | 140 GB | 4 |
| `aicoin-qwen-72b` | 72B | 24 GB | 144 GB | 4 |
| `llama-3-70b` | 70B | 24 GB | 140 GB | 4 |

### 自动估算

对于未在目录中注册的模型（如 `deepseek-67b`），系统会从名称中自动提取参数量并分级：

```
"deepseek-67b" → 提取 67B → ModelTier.LARGE → TENSOR_PARALLEL → min_nodes=4
```

---

## 9. 使用示例

### 9.1 基础调度

```python
from core.hybrid_inference import HybridScheduler

# 初始化调度器（使用默认配置）
scheduler = HybridScheduler()

# 模拟可用节点列表
available_nodes = [
    {"id": "node-001", "vram_gb": 24, "load": 0.3, "latency_ms": 15},
    {"id": "node-002", "vram_gb": 24, "load": 0.5, "latency_ms": 20},
    {"id": "node-003", "vram_gb": 48, "load": 0.1, "latency_ms": 10},
    {"id": "node-004", "vram_gb": 24, "load": 0.2, "latency_ms": 25},
    {"id": "node-005", "vram_gb": 24, "load": 0.4, "latency_ms": 18},
]

# 执行推理调度
result = scheduler.schedule(
    model_name="aicoin-llama-70b",       # 70B 模型 → LARGE → TENSOR_PARALLEL
    available_nodes=available_nodes,
    session_key="user_abc:conv_123",     # 会话标识（启用亲和性）
)

print(f"成功: {result['success']}")
print(f"集群: {result['cluster_id']}")
print(f"模式: {result['mode']}")                    # tensor_parallel
print(f"张量并行度: {result['tensor_parallel_size']}")  # 4
print(f"执行节点: {result['node_id']}")             # node-003
print(f"延迟: {result['total_latency_ms']}ms")
```

### 9.2 带热备的混合路由

```python
from core.hybrid_inference import HybridScheduler

scheduler = HybridScheduler()

# 自定义推理回调
def my_inference(node_id, cluster, request_data):
    """
    实际推理执行逻辑

    Args:
        node_id: 目标节点 ID
        cluster: 推理集群 (NodeCluster)
        request_data: 请求数据

    Returns:
        {"success": bool, "response": Any, "error": str|None}
    """
    try:
        # 在 node_id 上执行实际推理
        response = do_inference_on_node(node_id, request_data)
        return {"success": True, "response": response}
    except Exception as e:
        return {"success": False, "error": str(e)}

# 使用带热备的完整路由
result = scheduler.route_hybrid(
    model_name="aicoin-qwen-72b",
    request_data={"prompt": "解释量子计算的基本原理", "max_tokens": 512},
    session_key="user_xyz:conv_456",
    priority="normal",
    available_nodes=available_nodes,
    execute_callback=my_inference,
)

print(f"成功: {result['success']}")
print(f"Tier: {result['tier']}")                          # large
print(f"热备是否接管: {result['standby_activated']}")       # False (正常情况)
print(f"冗余策略: {result['redundancy_policy']}")
# {'active': 4, 'standby': 1, 'total_required': 5, 'degrade_to_active': 3}
```

### 9.3 节点变动处理

```python
from core.hybrid_inference import HybridScheduler

scheduler = HybridScheduler()

# 节点主动离开
migrated_sessions = scheduler.notify_node_leave("node-001")
print(f"迁移会话数: {len(migrated_sessions)}")
# → 熔断器: node-001 → OPEN
# → 亲和性: 解绑所有 node-001 的会话
# → 集群管理: 受影响集群自动重组

# 新节点加入
scheduler.notify_node_join("node-006", {
    "vram_gb": 48,
    "load": 0.0,
    "latency_ms": 12,
    "models": ["aicoin-llama-70b", "aicoin-qwen-72b"],
})
# → 熔断器: node-006 → CLOSED (可用)
# → 集群管理: 检查是否可重建不活跃集群
```

### 9.4 自定义配置

```python
from core.hybrid_inference import (
    HybridScheduler, HYBRID_CONFIG, TierRedundancyPolicy, ModelTier
)

# 自定义全局配置
config = HYBRID_CONFIG(
    cb_failure_threshold=3,         # 更激进的熔断: 3次失败即熔断
    cb_recovery_timeout=20.0,       # 更快恢复: 20秒后探测
    affinity_strategy="strong",     # 强亲和性: 多轮对话场景
    affinity_ttl=1200.0,            # 更长会话: 20分钟
    cluster_idle_timeout=180.0,     # 更快回收: 3分钟无请求即回收
)

scheduler = HybridScheduler(config=config)
```

### 9.5 监控与诊断

```python
# 获取调度统计
stats = scheduler.get_stats()
print(f"总调度: {stats['total_scheduled']}")
print(f"成功率: {stats['success_rate']}%")
print(f"热备接管: {stats['total_standby_activations']} 次")
print(f"熔断器: {stats['circuit_breaker']}")

# 获取集群详情
for cid, cluster in scheduler._cluster_manager.get_all_clusters().items():
    info = cluster.to_dict()
    print(f"集群 {cid}: 模型={info['model_name']}, "
          f"模式={info['mode']}, 节点={info['node_count']}, "
          f"效率={info['efficiency']}")

# 运行诊断
diagnosis = scheduler.diagnose()
print(f"系统健康: {diagnosis['healthy']}")
print(f"问题列表: {diagnosis['issues']}")
print(f"优化建议: {diagnosis['recommendations']}")
```

### 9.6 自适应节点池优化

```python
# 诊断某个模型的节点配置是否合理
resize_result = scheduler.adaptive_pool_resize(
    model_name="qwen2.5-7b",
    available_nodes=available_nodes,
)

if resize_result["action"] == "shrink":
    print(f"建议缩容: {resize_result['reason']}")
    # "模型 qwen2.5-7b 为 STANDALONE 模式，当前平均 3.0 个节点/集群，建议缩容至 1 个"
elif resize_result["action"] == "keep":
    print(f"配置合理: {resize_result['reason']}")
```

### 9.7 优雅停止

```python
# 停止调度器（释放所有资源）
scheduler.stop()
# → 停止后台GC线程
# → 解散所有活跃集群
# → 清除会话亲和性
```

---

## 附录: 枚举值速查

### ModelTier (模型分级)

| 枚举值 | 字符串值 | 参数量范围 |
|--------|---------|-----------|
| `ModelTier.TINY` | `"tiny"` | ≤ 8B |
| `ModelTier.SMALL` | `"small"` | 9B - 15B |
| `ModelTier.MEDIUM` | `"medium"` | 16B - 40B |
| `ModelTier.LARGE` | `"large"` | 41B - 72B |
| `ModelTier.XLARGE` | `"xlarge"` | 73B - 120B |
| `ModelTier.MASSIVE` | `"massive"` | > 120B |

### InferenceMode (推理模式)

| 枚举值 | 字符串值 | 说明 |
|--------|---------|------|
| `InferenceMode.STANDALONE` | `"standalone"` | 单节点独立推理 |
| `InferenceMode.TENSOR_PARALLEL` | `"tensor_parallel"` | 多节点张量并行 |
| `InferenceMode.PIPELINE_PARALLEL` | `"pipeline_parallel"` | 多节点流水线并行 |
| `InferenceMode.HYBRID_PARALLEL` | `"hybrid_parallel"` | 张量 + 流水线混合 |

### CircuitState (熔断器状态)

| 枚举值 | 字符串值 | 说明 |
|--------|---------|------|
| `CircuitState.CLOSED` | `"closed"` | 正常放行 |
| `CircuitState.OPEN` | `"open"` | 熔断拒绝 |
| `CircuitState.HALF_OPEN` | `"half_open"` | 试探放行 |
