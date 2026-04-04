# AICoin - 去中心化AI算力挖矿网络

<p align="center">
  <strong>Decentralized AI Compute Mining Network</strong>
</p>

<p align="center">
  <a href="#项目简介">项目简介</a> •
  <a href="#核心特性">核心特性</a> •
  <a href="#快速部署指南">部署指南</a> •
  <a href="#投票治理与模型选择详解">投票与模型选择</a> •
  <a href="#api-端点">API 端点</a>
</p>

---

## 项目简介

**AICoin (AIC)** 是一个基于区块链的去中心化AI计算网络。网络通过代币激励机制将AI算力提供者、API调用者和治理参与者连接在一起，构建一个可持续的去中心化AI基础设施。

### 三大角色

| 角色 | 说明 |
|------|------|
| **算力节点** | 贡献GPU/CPU算力运行AI模型，通过"算力挖矿"获得AICoin代币奖励 |
| **API调用者** | 通过燃烧AICoin代币来使用网络中的AI算力，获取OpenAI兼容的API服务 |
| **治理参与者** | AIC代币持有者通过投票治理，决定网络运行的AI模型及关键参数 |

---

## 核心特性

### 1. 算力挖矿（类比特币减半机制）

- 节点贡献GPU/CPU算力运行AI大语言模型
- 按算力贡献比例分配区块挖矿奖励
- **初始奖励 50 AIC / 区块**，每 **210,000 区块**减半
- **最大供应量 21,000,000 AIC**
- 80%奖励分配给算力节点，20%进入DAO金库

### 2. 代币治理（DAO）

- AIC代币持有者可以发起治理提案
- **提案类型**：运行模型选择、新增模型、移除模型、网络参数修改、紧急操作、协议升级
- **投票规则**：1 AIC = 1 票，51% 通过阈值，10% 法定人数
- 支持投票委托机制

### 3. 去中心化API网关

- **OpenAI兼容API接口** — 可直接替换 OpenAI SDK 的 `base_url`
- 代币燃烧机制，分三档计费
- **最优路由引擎**：就近选择延迟最低的可用节点
- 收入分配：80% 给算力节点，20% 进入金库

### 4. P2P分布式网络

- 基于 **servermodel** 的分布式推理引擎
- NAT穿透支持，轻松部署在家庭网络环境
- **Raft共识**实现Leader选举与节点协调
- **管线并行推理**，多节点协同处理大模型请求

---

## 架构设计

```
API调用者
   │ 燃烧AIC
   ▼
┌──────────┐    路由请求    ┌─────────────┐
│ API网关   │──────────────→│ 最优路由引擎  │
└──────────┘               └──────┬───────┘
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼              ▼
              ┌──────────┐ ┌──────────┐  ┌──────────┐
              │ 算力节点A │ │ 算力节点B │  │ 算力节点C │
              │ (挖矿)   │ │ (挖矿)   │  │ (挖矿)   │
              └────┬─────┘ └────┬─────┘  └────┬─────┘
                   │            │              │
                   └────────────┼──────────────┘
                                ▼
                    ┌──────────────────────┐
                    │   AICoin区块链        │
                    │ (智能合约管理层)       │
                    │ - AICoinToken 代币    │
                    │ - Mining 挖矿合约     │
                    │ - Governance 治理     │
                    │ - APIAccess API访问   │
                    └──────────────────────┘
```

---

## 项目结构

```
aicoin/
├── contracts/              # Solidity 智能合约
│   ├── AICoinToken.sol     # ERC20 代币合约 (21M最大供应)
│   ├── Mining.sol          # 算力挖矿合约 (减半机制)
│   ├── Governance.sol      # 治理投票合约
│   ├── APIAccess.sol       # API调用燃烧合约
│   └── AICoinDAO.sol       # DAO 聚合合约
├── core/                   # Python 后端核心
│   ├── blockchain.py       # 区块链交互层 (模拟/Web3双模式)
│   ├── mining_engine.py    # 挖矿引擎 (ComputeMeter/MiningEngine/RewardDistributor)
│   ├── router.py           # 最优路由引擎 (NodeRegistry/LatencyProbe/OptimalRouter)
│   ├── governance.py       # 治理管理 (GovernanceManager/ModelRegistry/ProposalExecutor)
│   ├── api_gateway.py      # OpenAI兼容API网关
│   ├── wallet.py           # 内置加密钱包 (BIP39/BIP44)
│   ├── config.py           # 配置管理系统
│   └── node.py             # 节点主程序入口
├── tests/                  # 测试套件 (106个测试用例)
├── ui/                     # Web 管理仪表盘
│   └── index.html          # 单页面仪表盘 (暗色主题)
├── config.json             # 默认配置模板
├── run.py                  # 一键启动脚本 (演示/钱包/状态)
├── requirements.txt        # Python 依赖
├── .gitignore              # Git 忽略规则
└── README.md               # 本文件
```

---

## 快速部署指南

### 1. 环境准备

| 依赖 | 最低版本 | 说明 |
|------|---------|------|
| Python | 3.10+ | 推荐 3.11 或 3.12 |
| pip | 最新版 | `pip install --upgrade pip` |
| CUDA GPU | 可选 | 用于加速模型推理（无GPU可运行模拟模式） |
| Node.js | 可选 | 仅Dashboard需要 |

### 2. 克隆与安装

```bash
# 克隆仓库
git clone https://github.com/ctz168/aicoin.git
cd aicoin

# 创建虚拟环境（推荐）
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -r requirements.txt
```

### 3. 快速体验（零配置）

无需任何配置，直接运行演示模式：

```bash
# 一键启动 → 进入交互菜单
python run.py

# 或直接进入挖矿演示
python run.py --demo
```

演示模式会自动完成以下步骤：
1. 创建/加载钱包（自动生成 BIP39 助记词 + 以太坊地址）
2. 初始化模拟区块链（无需真实链节点）
3. 模拟AI推理任务（贡献算力）
4. 提交算力证明到链上
5. 计算并领取挖矿奖励

### 4. 钱包管理

```bash
# 进入钱包管理界面
python run.py --wallet

# 可用操作:
#   1. 创建新钱包（生成12词助记词）
#   2. 导入钱包（输入助记词恢复）
#   3. 查看地址和余额
#   4. 备份助记词
```

> **重要**：请安全保管助记词！助记词是恢复钱包的唯一凭证。丢失助记词将导致资产无法找回。

### 5. 正式部署（作为算力节点）

#### 5.1 配置节点

编辑 `config.json`：

```json
{
  "node_id": "auto-generated",
  "node_name": "my-mining-node",
  "wallet_address": "0xYourWalletAddress",

  "host": "0.0.0.0",
  "api_port": 8080,
  "p2p_port": 5000,

  "blockchain_mode": "simulation",
  "auto_mine": true,
  "mining_interval": 60,

  "model_name": "Qwen/Qwen2.5-0.5B-Instruct",
  "model_base_path": "./models",

  "governance_enabled": true,
  "api_enabled": true,

  "seeds": ["seed-node-1:5000", "seed-node-2:5000"]
}
```

#### 5.2 连接真实区块链（可选）

默认使用模拟模式（`blockchain_mode = "simulation"`），如需连接真实链：

```json
{
  "blockchain_mode": "web3",
  "web3_rpc_url": "https://mainnet.infura.io/v3/YOUR_KEY",
  "contract_address": "0xDeployedContractAddress",
  "chain_id": 1
}
```

#### 5.3 启动节点

```bash
# 方式一：使用启动脚本
python run.py

# 方式二：直接启动节点
python -m core.node --config config.json
```

节点启动后将监听：
- **API服务**: `http://localhost:8080`
- **P2P服务**: `localhost:5000`

#### 5.4 查看节点状态

```bash
python run.py --status
```

### 6. API 调用

#### 使用 requests

```python
import requests

# 查询余额
balance = requests.get(
    "http://localhost:8080/v1/balance/your_address"
).json()

# 聊天补全（需要燃烧 AIC）
response = requests.post(
    "http://localhost:8080/v1/chat/completions",
    headers={
        "x-aicoin-address": "your_wallet_address",
        "Content-Type": "application/json"
    },
    json={
        "model": "Qwen/Qwen2.5-0.5B-Instruct",
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 100
    }
).json()
```

#### 使用 OpenAI SDK（直接兼容）

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="your_wallet_address"  # 用钱包地址作为 API Key
)

response = client.chat.completions.create(
    model="Qwen/Qwen2.5-0.5B-Instruct",
    messages=[{"role": "user", "content": "你好"}]
)
print(response.choices[0].message.content)
```

### 7. 运行测试

```bash
cd aicoin
python -m pytest tests/test_all.py -v
# 预期: 106 passed
```

---

## 投票治理与模型选择详解

### 概述

AICoin 网络采用 **DAO（去中心化自治组织）** 模式治理。所有 AIC 代币持有者都可以通过提案和投票来决定网络的发展方向，包括选择运行哪些AI模型、调整网络参数等。

**核心原则**：
- **1 AIC = 1 票**（代币加权投票）
- 提案者需要持有至少 **1,000 AIC** 才能发起提案
- 投票需要达到总供应量的 **10%** 参与才算有效（法定人数）
- 赞成票需要达到总票数的 **51%** 才能通过
- 标准提案投票周期：**7 天**
- 紧急提案投票周期：**24 小时**

### 提案类型一览

| 提案类型 | 说明 | 投票周期 | 典型场景 |
|---------|------|---------|---------|
| **RUN_MODEL** | 选择网络运行的AI模型 | 7天 | 切换到更新的模型 |
| **ADD_MODEL** | 注册新AI模型到网络 | 7天 | 添加新发布的优秀模型 |
| **REMOVE_MODEL** | 从网络移除模型 | 7天 | 移除过时或维护不周的模型 |
| **PARAM_CHANGE** | 修改网络参数 | 7天 | 调整API费率、质押量等 |
| **EMERGENCY** | 紧急安全操作 | 24小时 | 漏洞修复、网络暂停 |
| **UPGRADE** | 协议升级 | 7天 | 版本升级、架构调整 |

### 关于模型的管理

**Q: 初始的6个模型是写死的吗？会过时吗？**

**A: 不是写死的，模型完全由社区投票动态管理。**

初始6个模型只是网络启动时的**默认初始集**，它们存放在 `ModelRegistry` 中作为基准。随着时间的推移和AI技术的快速发展，这些模型确实会变得落后——这正是治理投票存在的意义。

社区的完整模型管理流程如下：

#### 步骤一：新增模型（ADD_MODEL 提案）

当社区发现更好的AI模型（例如 DeepSeek-V3、Llama-4、GPT-5 等新模型发布时），任何持有 1,000+ AIC 的用户都可以发起新增模型提案：

```python
from core.governance import GovernanceManager

gm = GovernanceManager(blockchain_manager)

# 发起"新增模型"提案
proposal_id = gm.create_add_model_proposal(
    proposer="0xYourWalletAddress",
    model_name="deepseek/DeepSeek-V3",
    description="DeepSeek-V3 是目前最强的开源中文大语言模型，"
                "671B参数MoE架构，中文能力远超现有模型。"
                "提议将其注册到网络中，为用户提供更高质量的对话服务。",
    model_info={
        "min_memory_gb": 240,
        "min_gpu_memory_gb": 80,
        "recommended_nodes": 4,
        "category": "chat",
        "description": "DeepSeek-V3 671B MoE 大型语言模型"
    }
)
print(f"提案已创建，ID: #{proposal_id}")
```

提案需要提供的信息：
- **model_name**：模型的 HuggingFace 标准名称
- **min_memory_gb**：运行该模型需要的最小内存
- **min_gpu_memory_gb**：需要的最小GPU显存
- **recommended_nodes**：推荐的分布式推理节点数
- **category**：模型类别（chat/image/code/embedding等）

#### 步骤二：社区投票

提案创建后，所有 AIC 持有者可以在 7 天内投票：

```python
# 投赞成票
gm.vote(voter="0xVoter1...", proposal_id=proposal_id, support=True)

# 投反对票
gm.vote(voter="0xVoter2...", proposal_id=proposal_id, support=False)

# 投票委托（不想自己投票可以委托给别人）
gm.delegate_vote(delegator="0xVoter3...", delegate="0xTrustedVoter...")
```

**投票权重计算**：
- 每个地址的投票权重 = 其持有的 AIC 代币数量
- 如果有人把投票权委托给你，你的权重 = 你的代币 + 委托给你的代币
- 每个地址对每个提案只能投一次票

**提案通过条件**：
1. 参与投票的代币总量 ≥ 总供应量的 10%（法定人数）
2. 赞成票 ≥ 总投票数的 51%（通过阈值）
3. 两个条件同时满足，提案才算通过

#### 步骤三：提案执行

投票期结束后（7天），系统自动结算提案：

| 情况 | 结果 | 说明 |
|------|------|------|
| 法定人数未达 | EXPIRED | 参与投票的代币不足总供应量的10% |
| 赞成率 < 51% | REJECTED | 虽然参与人数够了，但反对票更多 |
| 法定人数达标 + 赞成率 ≥ 51% | PASSED → EXECUTED | 提案通过并自动执行 |

ADD_MODEL 提案通过后：
- 新模型自动注册到 ModelRegistry
- 所有节点收到广播通知
- 该模型立即可供 RUN_MODEL 提案选择

#### 步骤四：切换活跃模型（RUN_MODEL 提案）

新模型注册成功后，还需要一个 RUN_MODEL 提案来切换网络当前运行的模型：

```python
# 提议切换到刚注册的新模型
run_proposal_id = gm.create_model_proposal(
    proposer="0xYourWalletAddress",
    model_name="deepseek/DeepSeek-V3",
    description="提议将网络活跃模型从 Qwen2.5-7B 切换到 DeepSeek-V3"
)

# 社区投票...
gm.vote(voter="0x...", proposal_id=run_proposal_id, support=True)
```

提案通过后，所有节点自动下载并切换到新模型。

#### 步骤五：移除旧模型（REMOVE_MODEL 提案）

当某个模型已经过时或不再需要时，可以发起移除提案：

```python
# 注意：活跃模型不能直接移除，必须先切换到其他模型
remove_proposal_id = gm.create_remove_model_proposal(
    proposer="0xYourWalletAddress",
    model_name="Qwen/Qwen2.5-0.5B-Instruct",
    description="该0.5B模型性能已严重落后，网络中已有更好的替代选择，"
                "建议移除以减少维护负担和节点存储开销"
)
```

**移除保护机制**：
- 当前正在运行的活跃模型 **不能被移除**（需先通过 RUN_MODEL 切换）
- 不存在的模型无法发起移除提案
- 移除操作不可逆，请谨慎投票

### 完整的模型生命周期

```
  ┌─────────────┐
  │ 发现新模型    │  例如: DeepSeek-V5 发布
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ ADD_MODEL   │  发起注册提案，提供模型硬件需求信息
  │ 提案投票     │  社区7天内投票 (1 AIC = 1票, 10%法定人数, 51%通过)
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ 模型注册成功  │  加入 ModelRegistry，所有节点可用
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ RUN_MODEL   │  发起运行提案，提议切换到此模型
  │ 提案投票     │  社区投票决定是否切换
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ 模型运行中    │  所有节点加载运行，API可用
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ 模型老化     │  更好的模型出现，性能落后
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ RUN_MODEL   │  先切换到新模型
  │ (切换走)     │  旧模型不再活跃
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ REMOVE_MODEL│  发起移除提案
  │ 提案投票     │  社区投票决定是否移除
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ 模型已移除    │  从注册表删除，释放资源
  └─────────────┘
```

### 投票委托

如果代币持有者不想自己逐一投票，可以委托给信任的地址：

```python
# 委托投票权
gm.delegate_vote(
    delegator="0xMyAddress...",      # 你的地址
    delegate="0xTrustedRepresentative..."  # 受托人地址
)

# 撤销委托
gm.revoke_delegation(delegator="0xMyAddress...")
```

委托规则：
- 不能委托给自己
- 新委托会覆盖旧委托
- 委托后，受托人的投票权重 = 其自身代币 + 所有委托给他的代币
- 委托不影响已完成的投票

### 网络参数修改（PARAM_CHANGE）

除了模型管理，社区还可以通过投票修改网络参数：

```python
# 调整API调用费率
gm.create_param_proposal(
    proposer="0x...",
    title="降低API调用费率",
    description="当前0.01 AIC/1K tokens的费率偏高，建议降至0.005以吸引更多用户",
    parameters={"api_rate_per_1k_tokens": 0.005}
)

# 调整最低提案质押
gm.create_param_proposal(
    proposer="0x...",
    title="降低提案质押门槛",
    description="将最低提案质押从1000 AIC降至500 AIC，让更多社区成员可以参与治理",
    parameters={"min_stake": 500}
)
```

可修改的参数包括：`api_rate_per_1k_tokens`、`min_stake`、`reward_rate`、`max_proposals_per_day`、`block_reward`。

---

## 智能合约

所有智能合约位于 `contracts/` 目录，基于 Solidity 编写，遵循 Ethereum 标准。

| 合约 | 说明 |
|------|------|
| **AICoinToken.sol** | ERC20 代币合约，21,000,000 AIC 最大供应量，支持铸造/燃烧 |
| **Mining.sol** | 算力证明挖矿合约，内置比特币减半奖励机制 |
| **Governance.sol** | DAO治理合约，支持提案创建、投票、执行 |
| **APIAccess.sol** | API访问控制合约，代币燃烧计费逻辑 |
| **AICoinDAO.sol** | DAO聚合合约，统一管理所有子合约交互 |

---

## 代币经济学

### 参数总览

| 参数 | 值 |
|------|-----|
| 代币名称 | AICoin (AIC) |
| 代币标准 | ERC20 |
| 最大供应量 | 21,000,000 AIC |
| 初始区块奖励 | 50 AIC |
| 减半周期 | 210,000 区块（约1年） |
| 节点奖励比例 | 80% |
| DAO金库比例 | 20% |
| 治理提案最低质押 | 1,000 AIC |
| 投票周期 | 7 天（紧急24小时） |
| 法定人数 | 总供应量的 10% |
| 通过阈值 | 赞成票 ≥ 51% |

### API 调用定价

#### 模型分级定价（输入/输出分离计费）

| 模型 | 参数量 | 输入价格 | 输出价格 |
|------|--------|---------|---------|
| **aicoin-llama-7b** | 7B | 0.001 AIC / 1K | 0.003 AIC / 1K |
| **aicoin-mistral-7b** | 7B | 0.001 AIC / 1K | 0.003 AIC / 1K |
| **aicoin-llama-13b** | 13B | 0.002 AIC / 1K | 0.005 AIC / 1K |
| **aicoin-coder-34b** | 34B | 0.003 AIC / 1K | 0.008 AIC / 1K |
| **aicoin-llama-70b** | 70B | 0.005 AIC / 1K | 0.012 AIC / 1K |
| **aicoin-qwen-72b** | 72B | 0.005 AIC / 1K | 0.012 AIC / 1K |

#### 优先级倍率

| 档位 | 倍率 | 速率限制 | 适用场景 |
|------|------|---------|---------|
| **Basic** | × 1.0 | 10 RPM, 50K TPM | 日常开发测试 |
| **Premium** | × 2.0 | 60 RPM, 200K TPM | 生产环境应用 |
| **Priority** | × 3.0 | 120 RPM, 500K TPM | 低延迟关键任务 |

> 💡 定价远低于 OpenAI（GPT-4o: $2.50/$10.00 per 1M）和 Together AI（Llama 70B: $0.35/$0.40 per 1M）。详细竞品对比见 [docs/PRICING_ANALYSIS.md](docs/PRICING_ANALYSIS.md)。

### 减半时间线

| 减半次数 | 区块高度 | 区块奖励 | 累计产出 |
|---------|---------|---------|---------|
| 创世 | 0 | 50 AIC | — |
| 第1次 | 210,000 | 25 AIC | 10,500,000 AIC |
| 第2次 | 420,000 | 12.5 AIC | 15,750,000 AIC |
| 第3次 | 630,000 | 6.25 AIC | 18,375,000 AIC |
| 第4次 | 840,000 | 3.125 AIC | 19,687,500 AIC |

---

## 配置说明

`config.json` 配置项说明：

| 配置路径 | 说明 | 默认值 |
|---------|------|-------|
| `node.node_id` | 节点唯一标识 | 自动生成UUID |
| `node.node_name` | 节点显示名称 | aicoin-{id前8位} |
| `node.wallet_address` | 钱包地址 | 空 |
| `network.host` | 监听地址 | 0.0.0.0 |
| `network.api_port` | API服务端口 | 8080 |
| `network.p2p_port` | P2P通信端口 | 5000 |
| `blockchain.mode` | 区块链模式 | simulation |
| `mining.auto_mine` | 自动挖矿 | true |
| `mining.mining_interval` | 挖矿间隔(秒) | 60 |
| `routing.strategy` | 路由策略 | BALANCED |
| `model.name` | 模型名称 | Qwen/Qwen2.5-0.5B-Instruct |

环境变量覆盖：所有配置均可通过 `AICOIN_` 前缀的环境变量覆盖，例如：
```bash
export AICOIN_API_PORT=9090
export AICOIN_AUTO_MINE=true
export AICOIN_SEEDS=node1:5000,node2:5000
```

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/stats` | 网络统计信息 |
| GET | `/v1/models` | 可用模型列表 |
| GET | `/v1/pricing` | API定价信息 |
| GET | `/v1/balance/:address` | 查询余额 |
| POST | `/v1/chat/completions` | 聊天补全（OpenAI兼容） |
| POST | `/v1/governance/proposals` | 创建治理提案 |
| POST | `/v1/governance/vote` | 投票 |
| GET | `/v1/governance/proposals` | 查询提案列表 |
| GET | `/v1/mining/stats` | 挖矿统计 |
| GET | `/v1/mining/rewards/:address` | 查询挖矿奖励 |

---

## 许可证

MIT License
