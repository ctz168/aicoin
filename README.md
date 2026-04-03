# AICoin - 去中心化AI算力挖矿网络

<p align="center">
  <strong>Decentralized AI Compute Mining Network</strong>
</p>

<p align="center">
  <a href="#项目简介">项目简介</a> •
  <a href="#核心特性">核心特性</a> •
  <a href="#架构设计">架构设计</a> •
  <a href="#快速开始">快速开始</a> •
  <a href="#智能合约">智能合约</a> •
  <a href="#代币经济学">代币经济学</a>
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
- **提案类型**：运行模型选择、网络参数修改、紧急操作
- **投票规则**：1 AIC = 1 票，51% 通过阈值，10% 法定人数
- 支持投票委托机制

### 3. 去中心化API网关

- **OpenAI兼容API接口** — 可直接替换 OpenAI SDK 的 `base_url`
- 代币燃烧机制，分三档计费：
  - `basic` — 标准优先级
  - `premium` — 高优先级
  - `priority` — 最高优先级
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
│   ├── blockchain.py       # 区块链交互层
│   ├── mining_engine.py    # 挖矿引擎
│   ├── router.py           # 最优路由引擎
│   ├── governance.py       # 治理管理模块
│   ├── api_gateway.py      # OpenAI兼容API网关
│   ├── config.py           # 配置管理系统
│   └── node.py             # 节点主程序入口
├── tests/                  # 测试套件
├── ui/                     # Web 管理仪表盘
│   └── index.html          # 单页面仪表盘
├── config.json             # 默认配置模板
├── requirements.txt        # Python 依赖
├── .gitignore              # Git 忽略规则
└── README.md               # 本文件
```

---

## 快速开始

### 环境要求

| 依赖 | 最低版本 | 说明 |
|------|---------|------|
| Python | 3.10+ | 推荐 3.11 或 3.12 |
| pip | 最新版 | `pip install --upgrade pip` |
| CUDA GPU | 可选 | 用于加速模型推理 |

### 安装

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

### 配置

编辑 `config.json`，根据需要修改以下字段：

```json
{
  "node": {
    "node_id": "your-unique-node-id",
    "node_name": "my-aicoin-node",
    "wallet_address": "0xYourWalletAddress"
  }
}
```

> 默认配置为**模拟模式**（`blockchain.mode = "simulation"`），无需真实区块链节点即可运行和测试。

### 启动节点

```bash
python -m core.node --config config.json
```

节点启动后将监听：
- **API服务**: `http://localhost:8080`
- **P2P服务**: `localhost:5000`

### API 调用示例

#### 使用 requests

```python
import requests

# 查询余额
balance = requests.get(
    "http://localhost:8080/v1/balance/your_address"
).json()
print(balance)

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
print(response)
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
| 投票周期 | 7 天 |
| 法定人数 | 10% |
| 通过阈值 | 51% |

### API 调用定价

| 档位 | 价格 | 优先级 | 适用场景 |
|------|------|--------|---------|
| **Basic** | 0.01 AIC / 1K tokens | 标准 | 日常开发测试 |
| **Premium** | 0.05 AIC / 1K tokens | 高 | 生产环境应用 |
| **Priority** | 0.10 AIC / 1K tokens | 最高 | 低延迟关键任务 |

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
| `node.node_id` | 节点唯一标识 | 自动生成 |
| `node.wallet_address` | 钱包地址 | 空 |
| `network.api_port` | API服务端口 | 8080 |
| `network.p2p_port` | P2P通信端口 | 5000 |
| `blockchain.mode` | 区块链模式 | `simulation` |
| `mining.auto_mine` | 自动挖矿 | `true` |
| `mining.mining_interval` | 挖矿间隔(秒) | 60 |
| `routing.strategy` | 路由策略 | `BALANCED` |
| `model.name` | 模型名称 | `Qwen/Qwen2.5-0.5B-Instruct` |

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

---

## 致谢

- **Bitcoin** — 减半经济学模型
- **Ethereum** — 智能合约标准 (ERC20)
- **servermodel** — 分布式推理引擎
- **Qwen** — 默认运行模型 (Qwen2.5)
- **HuggingFace Transformers** — 模型加载与推理框架
