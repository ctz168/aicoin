# AICoin API 使用指南

> AICoin 提供与 OpenAI 完全兼容的 API 接口，用户通过燃烧 AIC 代币来调用去中心化网络中的 AI 推理服务。本文档详细说明 API 的认证、调用方式、定价体系和最佳实践。

## 目录

- [快速开始](#快速开始)
- [认证方式](#认证方式)
- [API 端点](#api-端点)
  - [聊天补全](#聊天补全-post-v1chatcompletions)
  - [文本补全](#文本补全-post-v1completions)
  - [模型列表](#模型列表-get-v1models)
  - [余额查询](#余额查询)
- [定价体系](#定价体系)
  - [模型定价](#模型定价)
  - [优先级层级](#优先级层级)
  - [费用计算示例](#费用计算示例)
  - [与同行对比](#与同行对比)
- [错误处理](#错误处理)
- [速率限制](#速率限制)
- [Python SDK 示例](#python-sdk-示例)
- [Node.js SDK 示例](#nodejs-sdk-示例)
- [常见问题](#常见问题)

---

## 快速开始

```bash
# 最简单的调用方式 (Basic 级别, 使用 aicoin-llama-7b 模型)
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your_wallet_address>" \
  -d '{
    "model": "aicoin-llama-7b",
    "messages": [
      {"role": "user", "content": "你好，请介绍一下 AICoin 网络"}
    ]
  }'
```

### 前置条件

1. **AIC 钱包**：你需要一个 AICoin 钱包地址，可通过 `wallet.py` 创建
2. **AIC 余额**：钱包中需要有足够的 AIC 代币余额用于燃烧
3. **网络连接**：能访问 AICoin API 网关节点

---

## 认证方式

AICoin API 支持两种认证方式，认证信息通过 HTTP 请求头传递：

### 方式一：Authorization 头（推荐）

```http
Authorization: Bearer <your_wallet_address>
```

```bash
curl -H "Authorization: Bearer 0x1234567890abcdef..."
```

### 方式二：x-aicoin-address 头

```http
x-aicoin-address: <your_wallet_address>
```

```bash
curl -H "x-aicoin-address: 0x1234567890abcdef..."
```

### 增强安全：签名验证（可选）

对于需要更高安全性的场景，可以附加签名验证来防止重放攻击：

```http
x-aicoin-signature: <signed_timestamp_base64>
x-aicoin-timestamp: <unix_timestamp>
```

签名消息格式为 `aicoin-api-auth:<address>:<timestamp>`，使用钱包私钥签名。签名有效期为 300 秒，且每个 nonce 只能使用一次。

```python
import hashlib
import time
from eth_account.messages import encode_defunct

# 构造签名消息
timestamp = int(time.time())
message = f"aicoin-api-auth:{wallet_address}:{timestamp}"

# 使用私钥签名 (eth_account 示例)
msg = encode_defunct(text=message)
signed = w3.eth.account.sign_message(msg, private_key=private_key)
signature = signed.signature.hex()
```

---

## API 端点

所有端点兼容 OpenAI API 格式，基础 URL 默认为 `http://localhost:8080`。

### 聊天补全 POST /v1/chat/completions

与 OpenAI Chat Completions API 完全兼容。

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | 是 | 模型名称，参见[模型列表](#模型定价) |
| `messages` | array | 是 | 消息数组，每条包含 `role` 和 `content` |
| `temperature` | float | 否 | 采样温度，范围 [0.0, 2.0]，默认 1.0 |
| `top_p` | float | 否 | 核采样概率，范围 [0.0, 1.0]，默认 1.0 |
| `n` | integer | 否 | 生成候选数量，范围 [1, 10]，默认 1 |
| `max_tokens` | integer | 否 | 最大输出 token 数，范围 [1, 32768] |
| `stream` | boolean | 否 | 是否使用流式输出，默认 false |
| `stop` | string/array | 否 | 停止序列，最多 4 个元素 |
| `presence_penalty` | float | 否 | 存在惩罚，范围 [-2.0, 2.0]，默认 0 |
| `frequency_penalty` | float | 否 | 频率惩罚，范围 [-2.0, 2.0]，默认 0 |

**请求示例：**

```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 0xYourWalletAddress" \
  -H "x-aicoin-tier: premium" \
  -d '{
    "model": "aicoin-qwen-72b",
    "messages": [
      {"role": "system", "content": "你是一个专业的编程助手"},
      {"role": "user", "content": "用 Python 实现一个简单的 LRU 缓存"}
    ],
    "temperature": 0.7,
    "max_tokens": 2048
  }'
```

**成功响应：**

```json
{
  "id": "chatcmpl-aicoin-abc123",
  "object": "chat.completion",
  "created": 1714000000,
  "model": "aicoin-qwen-72b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "以下是一个使用 Python 实现的简单 LRU 缓存..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 25,
    "completion_tokens": 512,
    "total_tokens": 537,
    "aic_burned": "0.0012"
  }
}
```

**响应字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 请求唯一标识符 |
| `object` | string | 固定为 `chat.completion` |
| `created` | integer | Unix 时间戳 |
| `model` | string | 使用的模型名称 |
| `choices` | array | 生成结果数组 |
| `choices[].message.role` | string | 固定为 `assistant` |
| `choices[].message.content` | string | 模型生成的文本内容 |
| `choices[].finish_reason` | string | 结束原因：`stop`(正常) / `length`(达到上限) |
| `usage` | object | Token 使用统计 |
| `usage.aic_burned` | string | 本次请求燃烧的 AIC 数量 |

### 文本补全 POST /v1/completions

与 OpenAI Completions API 兼容（非聊天模式）。

```bash
curl -X POST http://localhost:8080/v1/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 0xYourWalletAddress" \
  -d '{
    "model": "aicoin-coder-34b",
    "prompt": "def fibonacci(n):",
    "max_tokens": 256,
    "temperature": 0.3
  }'
```

### 模型列表 GET /v1/models

查询当前网络支持的模型列表及其定价信息。

```bash
curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer 0xYourWalletAddress"
```

**响应示例：**

```json
{
  "object": "list",
  "data": [
    {
      "id": "aicoin-llama-7b",
      "object": "model",
      "owned_by": "AICoin Network",
      "context_window": 4096,
      "max_output_tokens": 2048,
      "pricing": {
        "input": "0.0004 AIC/1K tokens",
        "output": "0.0012 AIC/1K tokens"
      }
    }
  ]
}
```

### 余额查询

查询钱包 AIC 余额（通过节点管理接口）：

```bash
# 查询余额
curl http://localhost:8080/balance/0xYourWalletAddress
```

---

## 定价体系

### 模型定价

AICoin 的定价基于模型参数量分级，输入和输出 token 分别计费。以下是 v2 上线初期优惠价格（已包含闲置算力 9 折优惠）：

| 模型 | 参数量 | 级别 | 输入价格 | 输出价格 | 上下文窗口 | 最大输出 |
|------|--------|------|----------|----------|------------|----------|
| `aicoin-llama-7b` | 7B | Tiny | 0.0004 AIC/1K | 0.0012 AIC/1K | 4,096 | 2,048 |
| `aicoin-mistral-7b` | 7B | Tiny | 0.0004 AIC/1K | 0.0012 AIC/1K | 8,192 | 4,096 |
| `aicoin-llama-13b` | 13B | Small | 0.0007 AIC/1K | 0.0018 AIC/1K | 4,096 | 4,096 |
| `aicoin-coder-34b` | 34B | Medium | 0.0012 AIC/1K | 0.0032 AIC/1K | 16,384 | 4,096 |
| `aicoin-llama-70b` | 70B | Large | 0.0018 AIC/1K | 0.0045 AIC/1K | 8,192 | 4,096 |
| `aicoin-qwen-72b` | 72B | Large | 0.0018 AIC/1K | 0.0045 AIC/1K | 32,768 | 8,192 |

> **定价单位**：10^8 最小单位 = 1 AIC。所有价格以 AIC 计算，实际 USD 成本取决于 AIC 的市场汇率。

### 优先级层级

通过 `x-aicoin-tier` 请求头选择优先级，不同层级有不同的倍率和速率限制：

| 层级 | 请求头值 | 倍率 | 每分钟请求数 | 每分钟 Token 数 | 适用场景 |
|------|----------|------|-------------|----------------|----------|
| **Basic** | `basic` (默认) | ×1.0 | 10 | 50,000 | 开发测试、低频调用 |
| **Premium** | `premium` | ×2.0 | 60 | 200,000 | 生产环境、常规业务 |
| **Priority** | `priority` | ×3.0 | 120 | 500,000 | 高并发、低延迟场景 |

> **注意**：倍率会乘以模型基础价格。例如 Priority 层级使用 aicoin-llama-70b，输入价格为 0.0018 × 3.0 = 0.0054 AIC/1K。

### 费用计算示例

**示例 1：Basic 级别，7B 模型，短对话**

```text
模型: aicoin-llama-7b
层级: basic (×1.0)
输入: 100 tokens → 向上取整为 1K → 1 × 0.0004 = 0.0004 AIC
输出: 200 tokens → 向上取整为 1K → 1 × 0.0012 = 0.0012 AIC
─────────────────────────────────────────────
总计: 0.0016 AIC
```

**示例 2：Premium 级别，70B 模型，长对话**

```text
模型: aicoin-llama-70b
层级: premium (×2.0)
输入: 2,500 tokens → 向上取整为 3K → 3 × 0.0018 × 2.0 = 0.0108 AIC
输出: 1,200 tokens → 向上取整为 2K → 2 × 0.0045 × 2.0 = 0.018 AIC
─────────────────────────────────────────────
总计: 0.0288 AIC
```

**示例 3：Priority 级别，72B 模型，代码生成**

```text
模型: aicoin-qwen-72b
层级: priority (×3.0)
输入: 500 tokens → 向上取整为 1K → 1 × 0.0018 × 3.0 = 0.0054 AIC
输出: 4,000 tokens → 向上取整为 4K → 4 × 0.0045 × 3.0 = 0.054 AIC
─────────────────────────────────────────────
总计: 0.0594 AIC
```

### 与同行对比

AICoin 利用全球闲置算力网络，相比传统中心化推理服务具有显著成本优势。以下是与主流推理服务商的价格对比（按 USD/1K tokens 计算，假设 AIC = $0.01）：

| 模型规模 | AICoin | DeepInfra | Groq | Together AI | Fireworks |
|----------|--------|-----------|------|-------------|-----------|
| ~8B 输入 | $0.000004 | $0.00002 | $0.00005 | $0.00010 | $0.00020 |
| ~8B 输出 | $0.000012 | $0.00005 | $0.00008 | $0.00010 | $0.00020 |
| ~70B 输入 | $0.000018 | $0.00010 | $0.00059 | $0.00088 | $0.00090 |
| ~70B 输出 | $0.000045 | $0.00032 | $0.00079 | $0.00088 | $0.00090 |

> 即使按 AIC = $0.10 的较高估值，AICoin 的 70B 模型定价（$0.00018/$0.00045）仍低于 Groq 和 Together AI，体现了去中心化闲置算力的成本优势。

---

## 错误处理

API 遵循 OpenAI 错误格式：

```json
{
  "error": {
    "type": "authentication_error",
    "message": "缺少认证信息: 需要提供 Authorization 或 x-aicoin-address 头",
    "code": null
  },
  "request_id": "req-aicoin-xyz789"
}
```

**常见错误类型：**

| HTTP 状态码 | 错误类型 | 说明 | 解决方案 |
|-------------|----------|------|----------|
| 401 | `authentication_error` | 认证失败 | 检查钱包地址格式是否正确 |
| 402 | `insufficient_balance` | 余额不足 | 通过挖矿获取更多 AIC |
| 400 | `invalid_request_error` | 请求格式错误 | 检查 JSON 格式和参数范围 |
| 404 | `model_not_found` | 模型不存在 | 检查模型名称，使用 `/v1/models` 查看可用模型 |
| 429 | `rate_limit_exceeded` | 超出速率限制 | 降低请求频率或升级优先级层级 |
| 500 | `server_error` | 服务端错误 | 稍后重试，或检查网络节点状态 |
| 503 | `no_available_nodes` | 无可用计算节点 | 网络中无节点支持该模型，稍后重试 |

---

## 速率限制

速率限制按钱包地址和优先级层级独立计算，使用滑动窗口算法：

| 层级 | 每分钟请求数 (RPM) | 每分钟 Token 数 (TPM) |
|------|--------------------|-----------------------|
| Basic | 10 | 50,000 |
| Premium | 60 | 200,000 |
| Priority | 120 | 500,000 |

**速率限制响应头：**

```http
X-RateLimit-Limit-Requests: 60
X-RateLimit-Remaining-Requests: 45
X-RateLimit-Limit-Tokens: 200000
X-RateLimit-Remaining-Tokens: 156000
```

**超出限制时的响应：**

```json
{
  "error": {
    "type": "rate_limit_exceeded",
    "message": "速率限制: 每分钟最多 10 个请求 (basic 层级)",
    "code": "rate_limit"
  }
}
```

---

## Python SDK 示例

### 使用 requests 直接调用

```python
import requests

AICOIN_API_BASE = "http://localhost:8080"
WALLET_ADDRESS = "0xYourWalletAddress"

def chat_completion(model: str, messages: list, tier: str = "basic", **kwargs):
    """调用 AICoin 聊天补全 API"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {WALLET_ADDRESS}",
        "x-aicoin-tier": tier,
    }
    payload = {
        "model": model,
        "messages": messages,
        **kwargs,
    }
    response = requests.post(
        f"{AICOIN_API_BASE}/v1/chat/completions",
        headers=headers,
        json=payload,
    )
    response.raise_for_status()
    return response.json()


# 基础调用
result = chat_completion(
    model="aicoin-llama-7b",
    messages=[
        {"role": "system", "content": "你是一个有帮助的助手"},
        {"role": "user", "content": "什么是去中心化 AI 计算？"},
    ],
)
print(result["choices"][0]["message"]["content"])
print(f"消耗 AIC: {result['usage']['aic_burned']}")


# 流式输出
import json

response = requests.post(
    f"{AICOIN_API_BASE}/v1/chat/completions",
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {WALLET_ADDRESS}",
    },
    json={
        "model": "aicoin-mistral-7b",
        "messages": [{"role": "user", "content": "写一首诗"}],
        "stream": True,
    },
    stream=True,
)

for line in response.iter_lines():
    if line:
        line = line.decode("utf-8")
        if line.startswith("data: "):
            data = line[6:]
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            delta = chunk["choices"][0].get("delta", {})
            if "content" in delta:
                print(delta["content"], end="", flush=True)
```

### 使用 OpenAI SDK（兼容模式）

由于 AICoin API 兼容 OpenAI 格式，你可以直接使用 OpenAI 的 Python SDK：

```python
from openai import OpenAI

# 指向 AICoin 网关
client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="0xYourWalletAddress",  # 钱包地址作为 API key
)

# 普通调用
response = client.chat.completions.create(
    model="aicoin-llama-70b",
    messages=[
        {"role": "user", "content": "解释一下区块链共识机制"}
    ],
    temperature=0.7,
    max_tokens=2048,
)

print(response.choices[0].message.content)

# 流式调用
stream = client.chat.completions.create(
    model="aicoin-qwen-72b",
    messages=[{"role": "user", "content": "写一段 Go 代码实现 HTTP 服务器"}],
    stream=True,
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

---

## Node.js SDK 示例

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://localhost:8080/v1",
  apiKey: "0xYourWalletAddress",
});

async function main() {
  // 基础调用
  const response = await client.chat.completions.create({
    model: "aicoin-llama-7b",
    messages: [
      { role: "system", content: "你是一个技术文档助手" },
      { role: "user", content: "解释 Docker 和 Kubernetes 的区别" },
    ],
    max_tokens: 2048,
  });

  console.log(response.choices[0].message.content);

  // 流式调用
  const stream = await client.chat.completions.create({
    model: "aicoin-mistral-7b",
    messages: [{ role: "user", content: "用 TypeScript 写一个二叉搜索树" }],
    stream: true,
  });

  for await (const chunk of stream) {
    const content = chunk.choices[0]?.delta?.content || "";
    process.stdout.write(content);
  }
}

main();
```

---

## 常见问题

### Q: 如何获取 AIC 代币？

通过运行 AICoin 计算节点参与挖矿。节点贡献闲置 GPU 算力给网络，网络根据贡献量分配 AIC 代币奖励。详细挖矿设置参见 [README.md](README.md)。

### Q: Token 数量是如何估算的？

系统使用简化公式：约 4 个字符 = 1 个 token。输入 token 根据消息内容长度计算，输出 token 根据 `max_tokens` 参数确定（未设置时默认 1024）。token 数量向上取整到 1K 的倍数进行计费。

### Q: 燃烧的代币去哪了？

燃烧的 AIC 代币会被永久销毁（不可恢复），其中 80% 的对应价值分配给执行推理的计算节点作为奖励，20% 进入网络国库用于生态发展。

### Q: 如何选择优先级层级？

- **Basic（免费）**：适合开发测试和个人项目，10 RPM 限制
- **Premium（×2.0）**：适合生产环境，60 RPM，适合大多数业务场景
- **Priority（×3.0）**：适合高并发低延迟场景，120 RPM，请求优先路由

通过在请求头中添加 `x-aicoin-tier: premium` 来选择层级，默认为 `basic`。

### Q: API 请求超时怎么办？

默认请求超时为 120 秒。如果遇到超时，可能是计算节点负载过高。API 网关内置了 3 次自动重试机制，会自动故障转移到其他可用节点。你也可以检查节点网络状态或升级到 Priority 层级获得更稳定的体验。

### Q: 支持多模态输入吗？

是的，API 支持文本和图片 URL 作为输入。图片作为多模态内容的一部分，每张图片约估算为 1000 tokens。

```json
{
  "model": "aicoin-qwen-72b",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "描述这张图片"},
        {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
      ]
    }
  ]
}
```

### Q: 每日有消费上限吗？

是的，每个钱包地址每日最多可燃烧 100,000 AIC。此限制可通过治理投票调整。
