# AICoin API 调用完全指南

## 一、概述

AICoin 提供与 OpenAI 完全兼容的 API 接口，用户通过燃烧 AIC 代币来调用去中心化网络中的 AI 算力。你可以直接将项目中 OpenAI SDK 的 `base_url` 替换为 AICoin 网关地址，零代码改动即可接入。

### 核心特点

- **OpenAI SDK 兼容**：`base_url` 一行替换即可迁移
- **代币燃烧计费**：不需要传统付费，使用 AIC 代币支付
- **智能路由**：自动选择延迟最低、成功率最高的计算节点
- **故障转移**：节点失败自动切换备用节点，对调用者透明
- **多优先级**：Basic / Premium / Priority 三档可选
- **签名安全**：支持钱包签名验证，防止重放攻击

---

## 二、API 端点一览

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `GET` | `/stats` | 网关统计数据 |
| `GET` | `/v1/models` | 获取可用模型列表 |
| `GET` | `/v1/pricing` | 获取定价信息 |
| `GET` | `/v1/balance/{address}` | 查询钱包余额 |
| `POST` | `/v1/chat/completions` | 聊天补全（OpenAI 兼容） |
| `POST` | `/v1/completions` | 文本补全（OpenAI 兼容） |

---

## 三、认证方式

### 方式一：Authorization Header（推荐）

```http
Authorization: Bearer <你的AICoin钱包地址>
```

### 方式二：自定义 Header

```http
x-aicoin-address: <你的AICoin钱包地址>
```

### 方式三：签名认证（高安全场景）

```http
x-aicoin-address: <钱包地址>
x-aicoin-signature: <签名>
x-aicoin-timestamp: <当前Unix时间戳>
```

签名消息格式：`aicoin-api-auth:<address>:<timestamp>`，使用钱包私钥签名。

> 签名有效期 300 秒，每个签名只能使用一次（防重放攻击）。

### 指定优先级（可选）

```http
x-aicoin-tier: premium
```

可选值：`basic`（默认）、`premium`、`priority`。

---

## 四、调用流程

当你发送一个 API 请求到 AICoin 网关时，系统会按以下步骤处理：

```
用户请求
   │
   ▼
┌──────────────┐
│ 1. 请求验证   │  检查 model、messages 格式是否合法
└──────┬───────┘
       ▼
┌──────────────┐
│ 2. 身份认证   │  验证钱包地址，可选签名校验
└──────┬───────┘
       ▼
┌──────────────┐
│ 3. 速率检查   │  检查是否超过频率和 token 用量限制
└──────┬───────┘
       ▼
┌──────────────┐
│ 4. 余额检查   │  检查 AIC 余额是否足够支付本次请求
└──────┬───────┘
       ▼
┌──────────────┐
│ 5. 代币燃烧   │  预先扣除 AIC 代币（burn）
└──────┬───────┘
       ▼
┌──────────────┐
│ 6. 智能路由   │  选择延迟最低、成功率最高的节点
└──────┬───────┘
       ▼
┌──────────────┐
│ 7. 转发推理   │  将请求发送到计算节点执行推理
│    (故障转移) │  失败自动切换备用节点
└──────┬───────┘
       ▼
┌──────────────┐
│ 8. 记录计费   │  记录 token 用量，分配收入给节点
└──────┬───────┘
       ▼
   返回结果
```

### 路由算法说明

节点选择基于五维加权评分：

| 维度 | 权重 | 说明 |
|------|------|------|
| **延迟** | 30% | 越低越好（TCP 连接时间测量） |
| **算力** | 25% | 越高越好（节点 GPU 算力评分） |
| **费用** | 20% | 越低越好（节点成本因子） |
| **可用性** | 15% | 越高越好（历史请求成功率） |
| **负载** | 10% | 越低越好（当前并发量/最大并发量） |

Premium 和 Priority 优先级会使用"延迟优先"策略，Basic 使用"均衡"策略。

---

## 五、定价说明

| 模型 | 参数量 | 输入价格 | 输出价格 |
|------|--------|---------|---------|
| aicoin-llama-7b | 7B | 0.001 AIC / 1K | 0.003 AIC / 1K |
| aicoin-mistral-7b | 7B | 0.001 AIC / 1K | 0.003 AIC / 1K |
| aicoin-llama-13b | 13B | 0.002 AIC / 1K | 0.005 AIC / 1K |
| aicoin-coder-34b | 34B | 0.003 AIC / 1K | 0.008 AIC / 1K |
| aicoin-llama-70b | 70B | 0.005 AIC / 1K | 0.012 AIC / 1K |
| aicoin-qwen-72b | 72B | 0.005 AIC / 1K | 0.012 AIC / 1K |

优先级倍率：Basic × 1.0 / Premium × 2.0 / Priority × 3.0

---

## 六、调用示例

### 6.1 curl 示例

#### 查询可用模型

```bash
curl http://localhost:8080/v1/models
```

响应：
```json
{
  "object": "list",
  "data": [
    {
      "id": "aicoin-llama-7b",
      "object": "model",
      "created": 1700000000,
      "owned_by": "AICoin Network",
      "context_window": 4096,
      "max_output_tokens": 2048,
      "pricing": {"input": "0.001 AIC/1K", "output": "0.003 AIC/1K"}
    }
  ]
}
```

#### 查询定价信息

```bash
curl http://localhost:8080/v1/pricing
```

#### 查询余额

```bash
curl http://localhost:8080/v1/balance/0xYourWalletAddressHere
```

响应：
```json
{
  "object": "balance",
  "address": "0xYourWalletAddressHere",
  "balance": 500000000,
  "balance_aic": "5.00000000",
  "tier": "basic",
  "rate_limits": {
    "remaining_requests": 10,
    "remaining_tokens": 50000
  }
}
```

#### 聊天补全（Basic 模式）

```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 0xYourWalletAddressHere" \
  -d '{
    "model": "aicoin-llama-7b",
    "messages": [
      {"role": "system", "content": "你是一个有帮助的AI助手。"},
      {"role": "user", "content": "请解释什么是去中心化AI计算网络？"}
    ],
    "max_tokens": 500,
    "temperature": 0.7
  }'
```

响应：
```json
{
  "id": "aicoin-req-abc123",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "aicoin-llama-7b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "去中心化AI计算网络是一种将全球闲置GPU算力..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 25,
    "completion_tokens": 150,
    "total_tokens": 175
  },
  "aicoin_metadata": {
    "burn_amount": "0.000525",
    "tier": "basic",
    "node_id": "node-abc123"
  }
}
```

#### 使用 Premium 优先级

```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "x-aicoin-address: 0xYourWalletAddressHere" \
  -H "x-aicoin-tier: premium" \
  -d '{
    "model": "aicoin-llama-70b",
    "messages": [
      {"role": "user", "content": "写一首关于区块链的诗"}
    ],
    "max_tokens": 1000
  }'
```

#### 文本补全（非聊天）

```bash
curl -X POST http://localhost:8080/v1/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 0xYourWalletAddressHere" \
  -d '{
    "model": "aicoin-llama-7b",
    "prompt": "人工智能的未来是",
    "max_tokens": 200,
    "temperature": 0.9
  }'
```

### 6.2 Python requests 示例

```python
import requests

API_BASE = "http://localhost:8080"
WALLET = "0xYourWalletAddressHere"

# ===== 1. 查询余额 =====
resp = requests.get(f"{API_BASE}/v1/balance/{WALLET}")
data = resp.json()
print(f"余额: {data['balance_aic']} AIC")
print(f"剩余配额: {data['rate_limits']}")

# ===== 2. 聊天补全 =====
resp = requests.post(
    f"{API_BASE}/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {WALLET}",
        "Content-Type": "application/json",
    },
    json={
        "model": "aicoin-llama-7b",
        "messages": [
            {"role": "system", "content": "你是 AICoin 网络的 AI 助手。"},
            {"role": "user", "content": "什么是算力挖矿？"},
        ],
        "temperature": 0.7,
        "max_tokens": 500,
    },
)

result = resp.json()
if "choices" in result:
    reply = result["choices"][0]["message"]["content"]
    tokens_used = result["usage"]["total_tokens"]
    burn = result.get("aicoin_metadata", {}).get("burn_amount", "N/A")
    print(f"AI: {reply}")
    print(f"消耗: {tokens_used} tokens, 燃烧: {burn} AIC")
else:
    print(f"错误: {result.get('error', {}).get('message', resp.text)}")

# ===== 3. 多轮对话 =====
conversation = [
    {"role": "system", "content": "你是一个编程助手。"},
]

questions = ["Python 怎么读取文件？", "那写入文件呢？", "有异步方式吗？"]
for q in questions:
    conversation.append({"role": "user", "content": q})
    
    resp = requests.post(
        f"{API_BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {WALLET}", "Content-Type": "application/json"},
        json={"model": "aicoin-llama-7b", "messages": conversation, "max_tokens": 300},
    )
    result = resp.json()
    reply = result["choices"][0]["message"]["content"]
    conversation.append({"role": "assistant", "content": reply})
    print(f"Q: {q}\nA: {reply}\n")
```

### 6.3 OpenAI SDK 示例（零改动迁移）

这是最推荐的方式——只需改一行 `base_url`：

```python
from openai import OpenAI

# ===== 方式一：环境变量 =====
# export OPENAI_BASE_URL=http://localhost:8080/v1
# export OPENAI_API_KEY=0xYourWalletAddressHere
# client = OpenAI()

# ===== 方式二：直接传入（推荐） =====
client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="0xYourWalletAddressHere",  # 用钱包地址代替 API Key
)

# 聊天补全 - 用法与 OpenAI 完全一样
response = client.chat.completions.create(
    model="aicoin-llama-7b",
    messages=[
        {"role": "system", "content": "你是一个有帮助的AI助手。"},
        {"role": "user", "content": "你好，介绍一下 AICoin 项目。"},
    ],
    max_tokens=500,
    temperature=0.7,
)

print(response.choices[0].message.content)
print(f"消耗 tokens: {response.usage.total_tokens}")

# 流式输出
stream = client.chat.completions.create(
    model="aicoin-llama-7b",
    messages=[{"role": "user", "content": "讲一个关于区块链的故事"}],
    max_tokens=300,
    stream=True,
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()

# 查看模型列表
models = client.models.list()
for model in models.data:
    print(f"  - {model.id}: {model.get('pricing', {})}")
```

### 6.4 带 AICoin 钱包签名的安全调用

```python
import time
import requests
from core.wallet import AICoinWallet

# 加载钱包
wallet = AICoinWallet("data/wallet.dat")
wallet.load("your_password")

API_BASE = "http://localhost:8080"
address = wallet.get_address()

# 生成签名
timestamp = int(time.time())
message = f"aicoin-api-auth:{address}:{timestamp}"
signature = wallet.sign_message(message)

# 发送带签名的请求
resp = requests.post(
    f"{API_BASE}/v1/chat/completions",
    headers={
        "Content-Type": "application/json",
        "x-aicoin-address": address,
        "x-aicoin-signature": signature,
        "x-aicoin-timestamp": str(timestamp),
        "x-aicoin-tier": "basic",
    },
    json={
        "model": "aicoin-llama-7b",
        "messages": [{"role": "user", "content": "你好"}],
    },
)
print(resp.json())
```

### 6.5 JavaScript / Node.js 示例

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://localhost:8080/v1",
  apiKey: "0xYourWalletAddressHere",
});

async function main() {
  const response = await client.chat.completions.create({
    model: "aicoin-llama-7b",
    messages: [
      { role: "system", content: "你是一个AI助手。" },
      { role: "user", content: "用JavaScript写一个冒泡排序" },
    ],
    max_tokens: 500,
  });

  console.log(response.choices[0].message.content);
}

main();
```

---

## 七、错误处理

### HTTP 状态码说明

| 状态码 | 错误类型 | 说明 |
|--------|---------|------|
| `400` | `invalid_request_error` | 请求格式错误（模型名无效、参数越界等） |
| `401` | `authentication_error` | 认证失败（缺少钱包地址、格式错误） |
| `402` | `insufficient_funds_error` | AIC 余额不足 |
| `402` | `payment_error` | 代币燃烧操作失败 |
| `429` | `rate_limit_error` | 速率限制（请求过频或 token 用量超限） |
| `500` | `server_error` | 服务器内部错误 |
| `503` | `service_unavailable` | 所有计算节点不可用 |

### 错误响应格式

```json
{
  "error": {
    "type": "insufficient_funds_error",
    "message": "AICoin 余额不足: 需要 0.001500 AIC, 当前余额 0.000800 AIC",
    "code": null
  },
  "request_id": "aicoin-req-abc123"
}
```

### Python 错误处理示例

```python
import requests

resp = requests.post(
    "http://localhost:8080/v1/chat/completions",
    headers={"Authorization": "Bearer 0xYourAddress", "Content-Type": "application/json"},
    json={"model": "aicoin-llama-7b", "messages": [{"role": "user", "content": "你好"}]},
)

if resp.status_code == 200:
    result = resp.json()
    print(result["choices"][0]["message"]["content"])
elif resp.status_code == 401:
    print("认证失败：请检查钱包地址")
elif resp.status_code == 402:
    error = resp.json()["error"]
    print(f"余额不足：{error['message']}")
    print("建议：通过挖矿获取 AIC，或从交易所转入")
elif resp.status_code == 429:
    error = resp.json()["error"]
    print(f"请求过频：{error['message']}")
    print("建议：降低请求频率或升级到 Premium")
elif resp.status_code == 503:
    print("所有节点不可用，请稍后重试")
else:
    print(f"未知错误: {resp.status_code} - {resp.text}")
```

---

## 八、最佳实践

### 8.1 控制成本

```python
# 1. 使用 max_tokens 限制输出长度，避免意外高消耗
response = client.chat.completions.create(
    model="aicoin-llama-7b",
    messages=[...],
    max_tokens=200,       # 限制输出不超过 200 tokens
)

# 2. 根据任务选择合适大小的模型
# 简单任务 → 7B 模型（最便宜）
# 复杂推理 → 70B 模型（更贵但更准确）

# 3. 定期检查余额
balance = requests.get(f"{API_BASE}/v1/balance/{WALLET}").json()
print(f"当前余额: {balance['balance_aic']} AIC")

# 4. 使用 streaming 模式，及时获取结果
stream = client.chat.completions.create(
    model="aicoin-llama-7b",
    messages=[...],
    stream=True,          # 流式输出，减少等待时间
)
```

### 8.2 选择合适的优先级

| 场景 | 推荐优先级 | 理由 |
|------|-----------|------|
| 开发调试 | Basic | 成本最低，速率够用 |
| 后台批处理 | Basic | 不需要低延迟 |
| 用户交互应用 | Premium | 需要快速响应 |
| 实时对话 | Priority | 需要最低延迟 |
| 生产环境 API | Premium | 平衡成本和体验 |

### 8.3 监控和调试

```python
# 查看网关统计信息
stats = requests.get("http://localhost:8080/stats").json()
print(f"总请求数: {stats['total_requests']}")
print(f"成功率: {stats['success_rate']}%")
print(f"总燃烧 AIC: {stats['total_aic_burned']}")
print(f"运行时间: {stats['uptime_seconds']} 秒")

# 健康检查
health = requests.get("http://localhost:8080/health").json()
print(f"状态: {health['status']}")
```

---

*文档版本：v1.0 | 更新日期：2026-04-04 | 作者：AICoin Core Team*
