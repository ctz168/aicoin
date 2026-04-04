"""
AICoin API 网关模块

提供 OpenAI 兼容的 API 接口，用户需燃烧 AICoin 代币来访问 AI 推理服务。
支持多优先级、智能路由、故障转移和完整的计费系统。

模块包含:
- TokenAuthenticator: 代币认证器
- RequestValidator: 请求验证器
- APIGateway: 主 API 网关
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
from aiohttp import web

# ============================================================
# 日志配置
# ============================================================

logger = logging.getLogger("aicoin.api_gateway")
logger.setLevel(logging.INFO)

# ============================================================
# 常量定义
# ============================================================


class PriorityTier(str, Enum):
    """API 优先级层级"""
    BASIC = "basic"
    PREMIUM = "premium"
    PRIORITY = "priority"


# 优先级倍率 (应用到模型基础价格上)
# Basic × 1.0 | Premium × 2.0 | Priority × 3.0
TIER_MULTIPLIERS: Dict[str, float] = {
    PriorityTier.BASIC.value: 1.0,
    PriorityTier.PREMIUM.value: 2.0,
    PriorityTier.PRIORITY.value: 3.0,
}

# 支持的模型列表 (与 OpenAI 兼容的模型名映射)
# 定价按模型参数量分级，输入/输出分离计费（单位: AIC, 10^8 = 1 AIC）
# input_rate / output_rate 表示每 1K tokens 的最小单位数
SUPPORTED_MODELS: Dict[str, Dict[str, Any]] = {
    "aicoin-llama-7b": {
        "display_name": "AICoin LLaMA 7B",
        "owner": "AICoin Network",
        "context_window": 4096,
        "max_output_tokens": 2048,
        "input_rate": 100_000,       # 0.001 AIC / 1K input tokens
        "output_rate": 300_000,      # 0.003 AIC / 1K output tokens
        "pricing": {"input": "0.001 AIC/1K", "output": "0.003 AIC/1K"},
    },
    "aicoin-mistral-7b": {
        "display_name": "AICoin Mistral 7B",
        "owner": "AICoin Network",
        "context_window": 8192,
        "max_output_tokens": 4096,
        "input_rate": 100_000,
        "output_rate": 300_000,
        "pricing": {"input": "0.001 AIC/1K", "output": "0.003 AIC/1K"},
    },
    "aicoin-llama-13b": {
        "display_name": "AICoin LLaMA 13B",
        "owner": "AICoin Network",
        "context_window": 4096,
        "max_output_tokens": 4096,
        "input_rate": 200_000,       # 0.002 AIC / 1K input tokens
        "output_rate": 500_000,      # 0.005 AIC / 1K output tokens
        "pricing": {"input": "0.002 AIC/1K", "output": "0.005 AIC/1K"},
    },
    "aicoin-coder-34b": {
        "display_name": "AICoin Coder 34B",
        "owner": "AICoin Network",
        "context_window": 16384,
        "max_output_tokens": 4096,
        "input_rate": 300_000,       # 0.003 AIC / 1K input tokens
        "output_rate": 800_000,      # 0.008 AIC / 1K output tokens
        "pricing": {"input": "0.003 AIC/1K", "output": "0.008 AIC/1K"},
    },
    "aicoin-llama-70b": {
        "display_name": "AICoin LLaMA 70B",
        "owner": "AICoin Network",
        "context_window": 8192,
        "max_output_tokens": 4096,
        "input_rate": 500_000,       # 0.005 AIC / 1K input tokens
        "output_rate": 1_200_000,    # 0.012 AIC / 1K output tokens
        "pricing": {"input": "0.005 AIC/1K", "output": "0.012 AIC/1K"},
    },
    "aicoin-qwen-72b": {
        "display_name": "AICoin Qwen 72B",
        "owner": "AICoin Network",
        "context_window": 32768,
        "max_output_tokens": 8192,
        "input_rate": 500_000,
        "output_rate": 1_200_000,
        "pricing": {"input": "0.005 AIC/1K", "output": "0.012 AIC/1K"},
    },
}

# 速率限制配置
DEFAULT_RATE_LIMITS: Dict[str, Dict[str, int]] = {
    PriorityTier.BASIC.value: {
        "requests_per_minute": 10,
        "tokens_per_minute": 50_000,
    },
    PriorityTier.PREMIUM.value: {
        "requests_per_minute": 60,
        "tokens_per_minute": 200_000,
    },
    PriorityTier.PRIORITY.value: {
        "requests_per_minute": 120,
        "tokens_per_minute": 500_000,
    },
}

# 签名有效期 (秒)
SIGNATURE_EXPIRY_SECONDS = 300

# 默认请求超时 (秒)
DEFAULT_REQUEST_TIMEOUT = 120.0

# 最大重试次数
MAX_RETRY_ATTEMPTS = 3

# 重试延迟基数 (秒)
RETRY_BASE_DELAY = 0.5

# 最大消息长度限制
MAX_MESSAGE_LENGTH = 100_000

# 最大请求数量上限
MAX_MESSAGES_PER_REQUEST = 100

# CORS 默认配置
DEFAULT_CORS_ORIGINS = "*"
DEFAULT_CORS_METHODS = "GET, POST, OPTIONS"
DEFAULT_CORS_HEADERS = "Content-Type, Authorization, x-aicoin-address, x-aicoin-signature, x-aicoin-tier"


# ============================================================
# 数据类
# ============================================================


@dataclass
class APIRequestRecord:
    """API 请求记录 - 用于计费和统计"""
    request_id: str
    address: str
    model: str
    tier: str
    estimated_tokens: int
    actual_tokens: int
    burn_amount: int
    node_id: str
    start_time: float
    end_time: float
    status: str  # "success", "failed", "timeout"
    error: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class NodeInfo:
    """节点信息"""
    node_id: str
    address: str
    host: str
    port: int
    models: List[str]
    tier: str
    is_active: bool = True
    last_heartbeat: float = 0.0
    total_requests: int = 0
    success_rate: float = 1.0
    avg_latency_ms: float = 0.0


@dataclass
class RateLimitEntry:
    """速率限制条目"""
    request_timestamps: List[float] = field(default_factory=list)
    token_counts: List[Tuple[float, int]] = field(default_factory=list)


# ============================================================
# 速率限制器
# ============================================================


class RateLimiter:
    """令牌桶速率限制器

    对每个钱包地址独立进行请求频率和 token 用量的限制。
    使用滑动窗口算法实现精确的速率控制。
    """

    def __init__(self, limits: Optional[Dict[str, Dict[str, int]]] = None):
        """
        初始化速率限制器

        Args:
            limits: 各层级的速率限制配置
                    {
                        "basic": {"requests_per_minute": 10, "tokens_per_minute": 50000},
                        "premium": {"requests_per_minute": 60, "tokens_per_minute": 200000},
                        "priority": {"requests_per_minute": 120, "tokens_per_minute": 500000},
                    }
        """
        self._limits = limits or DEFAULT_RATE_LIMITS
        self._entries: Dict[str, Dict[str, RateLimitEntry]] = {}
        self._lock = threading.Lock()

    def _cleanup_entry(self, entry: RateLimitEntry, window_seconds: float = 60.0) -> None:
        """清理过期的速率记录

        Args:
            entry: 速率限制条目
            window_seconds: 滑动窗口大小 (秒)
        """
        cutoff = time.time() - window_seconds
        entry.request_timestamps = [
            ts for ts in entry.request_timestamps if ts > cutoff
        ]
        entry.token_counts = [
            (ts, count) for ts, count in entry.token_counts if ts > cutoff
        ]

    def check_rate_limit(
        self, address: str, tier: str, estimated_tokens: int
    ) -> Tuple[bool, str]:
        """检查是否超过速率限制

        Args:
            address: 钱包地址
            tier: 优先级层级
            estimated_tokens: 预估 token 数量

        Returns:
            (是否允许, 错误信息)
        """
        if tier not in self._limits:
            return False, f"未知优先级层级: {tier}"

        tier_limits = self._limits[tier]
        max_rpm = tier_limits["requests_per_minute"]
        max_tpm = tier_limits["tokens_per_minute"]

        with self._lock:
            if address not in self._entries:
                self._entries[address] = {}
            if tier not in self._entries[address]:
                self._entries[address][tier] = RateLimitEntry()

            entry = self._entries[address][tier]
            self._cleanup_entry(entry)

            # 检查请求频率
            if len(entry.request_timestamps) >= max_rpm:
                return (
                    False,
                    f"速率限制: 每分钟最多 {max_rpm} 个请求 ({tier} 层级)",
                )

            # 检查 token 用量
            current_tpm = sum(count for _, count in entry.token_counts)
            if current_tpm + estimated_tokens > max_tpm:
                remaining = max_tpm - current_tpm
                return (
                    False,
                    f"速率限制: 每分钟最多 {max_tpm} tokens, 当前已用 {current_tpm}, "
                    f"仅剩 {remaining} tokens ({tier} 层级)",
                )

            # 记录本次请求
            entry.request_timestamps.append(time.time())
            entry.token_counts.append((time.time(), estimated_tokens))

        return True, ""

    def get_remaining_quota(self, address: str, tier: str) -> Dict[str, int]:
        """查询剩余配额

        Args:
            address: 钱包地址
            tier: 优先级层级

        Returns:
            剩余配额信息: {"remaining_requests": int, "remaining_tokens": int}
        """
        if tier not in self._limits:
            return {"remaining_requests": 0, "remaining_tokens": 0}

        tier_limits = self._limits[tier]

        with self._lock:
            if address not in self._entries or tier not in self._entries[address]:
                return {
                    "remaining_requests": tier_limits["requests_per_minute"],
                    "remaining_tokens": tier_limits["tokens_per_minute"],
                }

            entry = self._entries[address][tier]
            self._cleanup_entry(entry)

            used_requests = len(entry.request_timestamps)
            used_tokens = sum(count for _, count in entry.token_counts)

        return {
            "remaining_requests": tier_limits["requests_per_minute"] - used_requests,
            "remaining_tokens": tier_limits["tokens_per_minute"] - used_tokens,
        }

    def clear(self, address: Optional[str] = None) -> None:
        """清除速率限制记录

        Args:
            address: 钱包地址, 为 None 时清除所有记录
        """
        with self._lock:
            if address is None:
                self._entries.clear()
            elif address in self._entries:
                self._entries[address].clear()


# ============================================================
# 令牌认证器
# ============================================================


class TokenAuthenticator:
    """代币认证器 - 验证 API 调用者的 AICoin 余额和权限

    支持三种认证方式:
    1. Authorization: Bearer <wallet_address>
    2. x-aicoin-address: <wallet_address>
    3. x-aicoin-signature: <signed_timestamp> (用于安全验证)

    同时支持签名验证来防止重放攻击。
    """

    def __init__(self, blockchain_manager: Any):
        """
        初始化认证器

        Args:
            blockchain_manager: 区块链管理器实例, 需提供:
                - get_balance(address) -> int
                - verify_signature(address, message, signature) -> bool
        """
        self._blockchain = blockchain_manager
        self._seen_nonces: Set[str] = set()
        self._nonce_lock = threading.Lock()
        self._nonce_cleanup_interval = 3600  # 每小时清理一次
        self._last_nonce_cleanup = time.time()

        logger.info("令牌认证器初始化完成")

    def authenticate_request(self, request_headers: dict) -> Dict[str, Any]:
        """验证请求头中的认证信息

        支持两种方式:
        1. Authorization: Bearer <wallet_address>
        2. x-aicoin-address: <wallet_address>
        3. x-aicoin-signature: <signed_timestamp> (可选, 用于增强安全)

        Args:
            request_headers: HTTP 请求头字典

        Returns:
            认证结果字典:
            {
                "authenticated": bool,
                "address": str,
                "error": str,
                "has_signature": bool,
            }
        """
        result: Dict[str, Any] = {
            "authenticated": False,
            "address": "",
            "error": "",
            "has_signature": False,
        }

        # 优先级 1: 从 Authorization 头获取
        address = self._extract_address_from_auth(request_headers.get("Authorization", ""))

        # 优先级 2: 从 x-aicoin-address 头获取
        if not address:
            address = request_headers.get("x-aicoin-address", "").strip()

        if not address:
            result["error"] = "缺少认证信息: 需要提供 Authorization 或 x-aicoin-address 头"
            return result

        # 验证地址格式
        if not self._validate_address_format(address):
            result["error"] = f"无效的钱包地址格式: {address}"
            return result

        # 检查签名 (如果提供了的话)
        signature = request_headers.get("x-aicoin-signature", "").strip()
        if signature:
            result["has_signature"] = True
            timestamp_str = request_headers.get("x-aicoin-timestamp", "").strip()
            if not timestamp_str:
                result["error"] = "提供了签名但缺少 x-aicoin-timestamp 头"
                return result

            try:
                timestamp = int(timestamp_str)
            except ValueError:
                result["error"] = f"无效的时间戳格式: {timestamp_str}"
                return result

            if not self.verify_signature(address, signature, timestamp):
                result["error"] = "签名验证失败"
                return result

        result["authenticated"] = True
        result["address"] = address
        return result

    def verify_signature(self, address: str, signature: str, timestamp: int) -> bool:
        """验证签名防止重放攻击

        使用 nonce + 时间戳双重验证:
        1. 检查时间戳是否在有效期内
        2. 检查 nonce 是否已被使用 (防重放)

        Args:
            address: 钱包地址
            signature: 签名 (base64 编码)
            timestamp: Unix 时间戳

        Returns:
            签名是否有效
        """
        # 检查时间戳是否过期
        current_time = int(time.time())
        if abs(current_time - timestamp) > SIGNATURE_EXPIRY_SECONDS:
            logger.warning(
                "签名已过期: address=%s, timestamp=%d, current=%d",
                address,
                timestamp,
                current_time,
            )
            return False

        # 构造签名验证用的消息
        message = f"aicoin-api-auth:{address}:{timestamp}"
        nonce = hashlib.sha256(message.encode("utf-8")).hexdigest()

        # 检查 nonce 防重放
        with self._nonce_lock:
            # 定期清理过期 nonce
            if time.time() - self._last_nonce_cleanup > self._nonce_cleanup_interval:
                self._seen_nonces.clear()
                self._last_nonce_cleanup = time.time()

            if nonce in self._seen_nonces:
                logger.warning("检测到重放攻击: address=%s, nonce=%s", address, nonce[:16])
                return False

            self._seen_nonces.add(nonce)

        # 调用区块链管理器验证签名
        try:
            is_valid = self._blockchain.verify_signature(address, message, signature)
            if not is_valid:
                logger.warning("签名验证失败: address=%s", address)
            return is_valid
        except Exception as e:
            logger.error("签名验证异常: address=%s, error=%s", address, e)
            return False

    def check_balance(self, address: str, required_amount: int) -> bool:
        """检查余额是否足够

        Args:
            address: 钱包地址
            required_amount: 需要的 AICoin 数量 (最小单位, 10^8 = 1 AIC)

        Returns:
            余额是否足够
        """
        try:
            balance = self._blockchain.get_balance(address)
            is_sufficient = balance >= required_amount
            if not is_sufficient:
                logger.info(
                    "余额不足: address=%s, balance=%d, required=%d",
                    address,
                    balance,
                    required_amount,
                )
            return is_sufficient
        except Exception as e:
            logger.error("查询余额异常: address=%s, error=%s", address, e)
            return False

    def _extract_address_from_auth(self, auth_header: str) -> str:
        """从 Authorization 头中提取钱包地址

        支持格式: Bearer <wallet_address>

        Args:
            auth_header: Authorization 头的值

        Returns:
            钱包地址, 提取失败返回空字符串
        """
        if not auth_header:
            return ""

        parts = auth_header.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return ""

    @staticmethod
    def _validate_address_format(address: str) -> bool:
        """验证钱包地址格式

        支持多种区块链地址格式:
        - 以太坊: 0x 开头, 42 字符的十六进制
        - 通用: 32~64 字符的十六进制字符串

        Args:
            address: 钱包地址

        Returns:
            格式是否有效
        """
        if not address or len(address) < 32:
            return False

        # 以太坊格式
        if address.startswith("0x") and len(address) == 42:
            try:
                int(address[2:], 16)
                return True
            except ValueError:
                return False

        # 通用十六进制格式
        if re.match(r"^[0-9a-fA-F]{32,64}$", address):
            return True

        # Base58 格式 (Bitcoin 风格)
        if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,64}$", address):
            return True

        return False


# ============================================================
# 请求验证器
# ============================================================


class RequestValidator:
    """请求验证器 - 验证 API 请求的合法性

    负责:
    1. 验证请求格式是否正确
    2. 检查模型名称是否有效
    3. 清理输入防止注入攻击
    4. 确保 OpenAI 兼容性
    """

    # 允许的消息角色
    VALID_ROLES: Set[str] = {"system", "user", "assistant", "function", "tool"}

    # 模型名称验证正则
    MODEL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]$")

    def __init__(self, supported_models: Optional[Dict[str, Dict[str, Any]]] = None):
        """
        初始化请求验证器

        Args:
            supported_models: 支持的模型字典, 默认使用全局 SUPPORTED_MODELS
        """
        self._models = supported_models or SUPPORTED_MODELS
        logger.info(
            "请求验证器初始化完成, 支持 %d 个模型", len(self._models)
        )

    def validate_chat_request(self, request: dict) -> Tuple[bool, str, dict]:
        """验证聊天请求

        检查项:
        1. model 字段存在且有效
        2. messages 字段存在且非空
        3. 消息格式正确 (role + content)
        4. 参数在合理范围内

        Args:
            request: 聊天请求体

        Returns:
            (是否有效, 错误信息, 清理后的请求)
        """
        # 基础类型检查
        if not isinstance(request, dict):
            return False, "请求体必须是 JSON 对象", {}

        # 验证 model 字段
        model = request.get("model")
        if not model or not isinstance(model, str):
            return False, "缺少必填字段: model", {}

        if not self.validate_model(model):
            available = ", ".join(sorted(self._models.keys()))
            return (
                False,
                f"不支持的模型: {model}, 可用模型: {available}",
                {},
            )

        # 验证 messages 字段
        messages = request.get("messages")
        if not messages or not isinstance(messages, list):
            return False, "缺少必填字段: messages (必须是非空数组)", {}

        if len(messages) > MAX_MESSAGES_PER_REQUEST:
            return (
                False,
                f"消息数量超出限制: 最多 {MAX_MESSAGES_PER_REQUEST} 条, "
                f"当前 {len(messages)} 条",
                {},
            )

        # 验证每条消息
        cleaned_messages: List[Dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return False, f"第 {i + 1} 条消息必须是 JSON 对象", {}

            role = msg.get("role")
            if not role or role not in self.VALID_ROLES:
                return (
                    False,
                    f"第 {i + 1} 条消息的角色无效: {role}, "
                    f"允许的角色: {', '.join(sorted(self.VALID_ROLES))}",
                    {},
                )

            content = msg.get("content")
            if content is None:
                # name 字段可选
                if "name" not in msg and "tool_calls" not in msg and "tool_call_id" not in msg:
                    return (
                        False,
                        f"第 {i + 1} 条消息缺少 content 或 name/tool_calls/tool_call_id 字段",
                        {},
                    )

            # 清理消息
            cleaned_msg: Dict[str, Any] = {"role": role}
            if content is not None:
                if isinstance(content, str):
                    cleaned_msg["content"] = self.sanitize_text(content)
                elif isinstance(content, list):
                    # 多模态内容 (文本 + 图片等)
                    cleaned_msg["content"] = self._sanitize_content_parts(content)
                else:
                    return False, f"第 {i + 1} 条消息的 content 类型无效: {type(content).__name__}", {}

            # 复制允许的额外字段
            for extra_key in ("name", "tool_calls", "tool_call_id", "function_call"):
                if extra_key in msg:
                    cleaned_msg[extra_key] = msg[extra_key]

            cleaned_messages.append(cleaned_msg)

        # 构建清理后的请求
        cleaned_request: Dict[str, Any] = {
            "model": model,
            "messages": cleaned_messages,
        }

        # 可选参数验证
        optional_params = {
            "temperature": (0.0, 2.0),
            "top_p": (0.0, 1.0),
            "n": (1, 10),
            "max_tokens": (1, 32768),
            "presence_penalty": (-2.0, 2.0),
            "frequency_penalty": (-2.0, 2.0),
        }

        for param_name, (min_val, max_val) in optional_params.items():
            if param_name in request:
                value = request[param_name]
                if not isinstance(value, (int, float)):
                    return (
                        False,
                        f"参数 {param_name} 必须是数字类型",
                        {},
                    )
                if not (min_val <= value <= max_val):
                    return (
                        False,
                        f"参数 {param_name} 的值 {value} 超出范围 [{min_val}, {max_val}]",
                        {},
                    )
                cleaned_request[param_name] = value

        # 布尔类型可选参数
        for bool_param in ("stream", "logprobs"):
            if bool_param in request:
                value = request[bool_param]
                if not isinstance(value, bool):
                    return (
                        False,
                        f"参数 {bool_param} 必须是布尔类型",
                        {},
                    )
                cleaned_request[bool_param] = value

        # top_logprobs 参数 (仅在 logprobs=True 时有效)
        if "top_logprobs" in request:
            value = request["top_logprobs"]
            if not isinstance(value, int) or not (1 <= value <= 20):
                return False, "参数 top_logprobs 必须是 1-20 之间的整数", {}
            cleaned_request["top_logprobs"] = value

        # stop 参数
        if "stop" in request:
            stop = request["stop"]
            if isinstance(stop, str):
                cleaned_request["stop"] = stop
            elif isinstance(stop, list) and len(stop) <= 4:
                cleaned_request["stop"] = [self.sanitize_text(s) for s in stop if isinstance(s, str)]
            else:
                return False, "参数 stop 必须是字符串或最多 4 个元素的数组", {}

        # user 参数
        if "user" in request:
            cleaned_request["user"] = str(request["user"])[:256]

        return True, "", cleaned_request

    def validate_completions_request(self, request: dict) -> Tuple[bool, str, dict]:
        """验证补全请求 (非聊天)

        Args:
            request: 补全请求体

        Returns:
            (是否有效, 错误信息, 清理后的请求)
        """
        if not isinstance(request, dict):
            return False, "请求体必须是 JSON 对象", {}

        # 验证 model 字段
        model = request.get("model")
        if not model or not isinstance(model, str):
            return False, "缺少必填字段: model", {}

        if not self.validate_model(model):
            available = ", ".join(sorted(self._models.keys()))
            return (
                False,
                f"不支持的模型: {model}, 可用模型: {available}",
                {},
            )

        # 验证 prompt 字段
        prompt = request.get("prompt")
        if prompt is None:
            return False, "缺少必填字段: prompt", {}

        if isinstance(prompt, str):
            prompt = self.sanitize_text(prompt)
        elif isinstance(prompt, list):
            prompt = [
                self.sanitize_text(p) if isinstance(p, str) else p
                for p in prompt
            ]
        else:
            return False, "参数 prompt 必须是字符串或字符串数组", {}

        # 构建清理后的请求
        cleaned_request: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
        }

        # 可选参数 (与聊天共享参数范围)
        optional_params = {
            "temperature": (0.0, 2.0),
            "top_p": (0.0, 1.0),
            "n": (1, 10),
            "max_tokens": (1, 32768),
            "presence_penalty": (-2.0, 2.0),
            "frequency_penalty": (-2.0, 2.0),
            "best_of": (1, 20),
        }

        for param_name, (min_val, max_val) in optional_params.items():
            if param_name in request:
                value = request[param_name]
                if not isinstance(value, (int, float)):
                    return (
                        False,
                        f"参数 {param_name} 必须是数字类型",
                        {},
                    )
                if not (min_val <= value <= max_val):
                    return (
                        False,
                        f"参数 {param_name} 的值 {value} 超出范围 [{min_val}, {max_val}]",
                        {},
                    )
                cleaned_request[param_name] = value

        for bool_param in ("stream", "logprobs", "echo"):
            if bool_param in request:
                value = request[bool_param]
                if not isinstance(value, bool):
                    return False, f"参数 {bool_param} 必须是布尔类型", {}
                cleaned_request[bool_param] = value

        if "suffix" in request:
            cleaned_request["suffix"] = self.sanitize_text(str(request["suffix"]))

        if "user" in request:
            cleaned_request["user"] = str(request["user"])[:256]

        return True, "", cleaned_request

    def validate_model(self, model_name: str) -> bool:
        """验证模型名称是否有效

        Args:
            model_name: 模型名称

        Returns:
            模型是否支持
        """
        if not model_name:
            return False
        return model_name in self._models

    def sanitize_input(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """清理消息列表, 防止注入攻击

        对每条消息的文本内容进行 HTML 转义和控制字符过滤。

        Args:
            messages: 原始消息列表

        Returns:
            清理后的消息列表
        """
        if not messages:
            return []

        cleaned: List[Dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue

            cleaned_msg: Dict[str, Any] = {"role": msg.get("role", "user")}
            content = msg.get("content")

            if isinstance(content, str):
                cleaned_msg["content"] = self.sanitize_text(content)
            elif isinstance(content, list):
                cleaned_msg["content"] = self._sanitize_content_parts(content)
            elif content is not None:
                cleaned_msg["content"] = content

            # 复制其他字段
            for key, value in msg.items():
                if key not in cleaned_msg:
                    cleaned_msg[key] = value

            cleaned.append(cleaned_msg)

        return cleaned

    @staticmethod
    def sanitize_text(text: str) -> str:
        """清理文本内容

        1. 移除控制字符 (保留换行符和制表符)
        2. HTML 转义
        3. 截断过长文本

        Args:
            text: 原始文本

        Returns:
            清理后的文本
        """
        if not isinstance(text, str):
            return str(text)

        # 移除控制字符 (保留 \n, \r, \t)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

        # HTML 转义防止 XSS
        text = html.escape(text, quote=True)

        # 截断过长文本
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[:MAX_MESSAGE_LENGTH]
            logger.warning("消息文本被截断: 原始长度 %d -> %d", len(text), MAX_MESSAGE_LENGTH)

        return text

    def _sanitize_content_parts(self, parts: List[Any]) -> List[Dict[str, Any]]:
        """清理多模态内容部分

        Args:
            parts: 内容部分列表

        Returns:
            清理后的内容部分
        """
        cleaned_parts: List[Dict[str, Any]] = []

        for part in parts:
            if not isinstance(part, dict):
                continue

            part_type = part.get("type")
            if part_type == "text":
                text = part.get("text", "")
                cleaned_parts.append({
                    "type": "text",
                    "text": self.sanitize_text(text),
                })
            elif part_type == "image_url":
                image_url = part.get("image_url", {})
                if isinstance(image_url, dict):
                    url = image_url.get("url", "")
                    # 验证 URL 格式
                    if url and (url.startswith("data:") or url.startswith("http://") or url.startswith("https://")):
                        cleaned_parts.append({
                            "type": "image_url",
                            "image_url": {"url": url},
                        })
            else:
                # 保留其他未知类型 (透传)
                cleaned_parts.append(part)

        return cleaned_parts if cleaned_parts else [{"type": "text", "text": ""}]


# ============================================================
# API 网关
# ============================================================


class APIGateway:
    """AICoin API 网关 - 处理 API 请求并路由到计算节点

    功能:
    1. 接收 OpenAI 兼容的 API 请求
    2. 验证代币燃烧 (调用者必须燃烧 AICoin)
    3. 路由到最优节点
    4. 返回推理结果
    5. 记录请求用于计费和分配

    使用示例:
        gateway = APIGateway(blockchain_mgr, router, mining_engine, config)
        gateway.start_server(host="0.0.0.0", port=8080)
    """

    def __init__(
        self,
        blockchain_manager: Any,
        router: Any,
        mining_engine: Any,
        config: Optional[dict] = None,
    ):
        """
        初始化 API 网关

        Args:
            blockchain_manager: 区块链管理器实例, 需提供:
                - get_balance(address) -> int
                - verify_signature(address, message, signature) -> bool
                - burn_for_api_access(address, amount, request_id) -> bool
            router: 节点路由器实例, 需提供:
                - find_best_node(model, tier) -> NodeInfo
                - get_backup_nodes(model, tier, exclude) -> List[NodeInfo]
            mining_engine: 挖矿引擎实例, 需提供:
                - record_request(record) -> None
                - get_network_stats() -> dict
            config: 配置字典, 可包含:
                - cors_origins: CORS 允许的源
                - default_tier: 默认优先级层级
                - request_timeout: 请求超时 (秒)
                - max_retries: 最大重试次数
        """
        self._config = config or {}
        self._blockchain = blockchain_manager
        self._router = router
        self._mining_engine = mining_engine

        # 认证与验证
        self._authenticator = TokenAuthenticator(blockchain_manager)
        self._validator = RequestValidator()
        self._rate_limiter = RateLimiter()

        # HTTP 客户端会话
        self._client_session: Optional[aiohttp.ClientSession] = None
        self._session_lock = threading.Lock()

        # 服务器
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._server_thread: Optional[threading.Thread] = None
        self._is_running = False

        # CORS 配置
        self._cors_origins = self._config.get("cors_origins", DEFAULT_CORS_ORIGINS)
        self._cors_methods = self._config.get("cors_methods", DEFAULT_CORS_METHODS)
        self._cors_headers = self._config.get("cors_headers", DEFAULT_CORS_HEADERS)

        # 默认配置
        self._default_tier = self._config.get("default_tier", PriorityTier.BASIC.value)
        self._request_timeout = self._config.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)
        self._max_retries = self._config.get("max_retries", MAX_RETRY_ATTEMPTS)

        # 请求统计
        self._stats_lock = threading.Lock()
        self._total_requests = 0
        self._total_success = 0
        self._total_failed = 0
        self._total_tokens_processed = 0
        self._total_aic_burned = 0
        self._start_time = time.time()

        # 请求追踪
        self._pending_requests: Dict[str, asyncio.Event] = {}
        self._request_results: Dict[str, Dict[str, Any]] = {}

        logger.info(
            "API 网关初始化完成: default_tier=%s, timeout=%.1fs",
            self._default_tier,
            self._request_timeout,
        )

    # ================================================================
    # 属性
    # ================================================================

    @property
    def is_running(self) -> bool:
        """网关是否正在运行"""
        return self._is_running

    @property
    def stats(self) -> Dict[str, Any]:
        """获取网关统计数据"""
        with self._stats_lock:
            uptime = time.time() - self._start_time
            success_rate = (
                (self._total_success / self._total_requests * 100)
                if self._total_requests > 0
                else 0.0
            )
            return {
                "total_requests": self._total_requests,
                "total_success": self._total_success,
                "total_failed": self._total_failed,
                "success_rate": round(success_rate, 2),
                "total_tokens_processed": self._total_tokens_processed,
                "total_aic_burned": self._total_aic_burned,
                "uptime_seconds": int(uptime),
                "is_running": self._is_running,
            }

    # ================================================================
    # API 端点 (OpenAI 兼容)
    # ================================================================

    async def handle_chat_completions(self, request: dict) -> dict:
        """POST /v1/chat/completions - OpenAI 兼容的聊天补全接口

        处理流程:
        1. 验证请求 (model, messages, etc.)
        2. 检查调用者地址 (从 Authorization 或 x-aicoin-address 头)
        3. 根据预估 token + 层级计算燃烧金额
        4. 通过区块链管理器燃烧代币
        5. 路由到最优节点
        6. 转发请求到节点 API
        7. 通过挖矿引擎记录请求
        8. 返回 OpenAI 兼容的响应

        Args:
            request: 完整的 HTTP 请求数据 (包含 headers 和 body)

        Returns:
            OpenAI 兼容的 JSON 响应
        """
        request_id = self._generate_request_id()
        start_time = time.time()
        record: Optional[APIRequestRecord] = None

        try:
            # 分离 headers 和 body
            headers = request.get("headers", {})
            body = request.get("body", {})

            # Step 1: 验证请求格式
            is_valid, error_msg, cleaned_body = self._validator.validate_chat_request(body)
            if not is_valid:
                return self._make_error_response(
                    error_type="invalid_request_error",
                    message=error_msg,
                    status_code=400,
                    request_id=request_id,
                )

            model = cleaned_body["model"]
            messages = cleaned_body["messages"]

            # Step 2: 认证调用者
            auth_result = self._authenticator.authenticate_request(headers)
            if not auth_result["authenticated"]:
                return self._make_error_response(
                    error_type="authentication_error",
                    message=auth_result["error"],
                    status_code=401,
                    request_id=request_id,
                )

            address = auth_result["address"]

            # Step 3: 确定优先级层级
            tier = headers.get("x-aicoin-tier", self._default_tier).strip().lower()
            if tier not in TIER_MULTIPLIERS:
                tier = self._default_tier

            # Step 4: 估算 token 数量（输入/输出分离）
            input_tokens, output_tokens = self.estimate_tokens(cleaned_body)
            estimated_tokens = input_tokens + output_tokens

            # Step 5: 速率限制检查
            rate_ok, rate_error = self._rate_limiter.check_rate_limit(
                address, tier, estimated_tokens
            )
            if not rate_ok:
                return self._make_error_response(
                    error_type="rate_limit_error",
                    message=rate_error,
                    status_code=429,
                    request_id=request_id,
                )

            # Step 6: 计算燃烧金额（输入/输出分离 + 模型分级 + 优先级倍率）
            burn_amount = self.calculate_burn_amount(model, input_tokens, output_tokens, tier)

            # Step 7: 检查余额
            if not self._authenticator.check_balance(address, burn_amount):
                return self._make_error_response(
                    error_type="insufficient_funds_error",
                    message=(
                        f"AICoin 余额不足: 需要 {burn_amount / 1e8:.6f} AIC, "
                        f"请充值后重试"
                    ),
                    status_code=402,
                    request_id=request_id,
                )

            # Step 8: 燃烧代币
            burn_success = self._blockchain.burn_for_api_access(
                address, burn_amount, request_id
            )
            if not burn_success:
                return self._make_error_response(
                    error_type="payment_error",
                    message="代币燃烧失败, 请稍后重试",
                    status_code=402,
                    request_id=request_id,
                )

            logger.info(
                "代币燃烧成功: request=%s, address=%s, amount=%d, tier=%s, model=%s",
                request_id,
                address[:12],
                burn_amount,
                tier,
                model,
            )

            # Step 9: 路由到最优节点并转发请求 (带故障转移)
            try:
                node_response = await self.forward_with_fallback(
                    cleaned_body, model, tier
                )
            except Exception as e:
                logger.error("请求转发失败: request=%s, error=%s", request_id, e)
                # 退还代币 (在实现中应该有退款机制)
                return self._make_error_response(
                    error_type="api_error",
                    message=f"所有计算节点不可用, 请稍后重试: {str(e)}",
                    status_code=503,
                    request_id=request_id,
                )

            end_time = time.time()

            # Step 10: 构建记录
            actual_tokens = self._extract_token_count(node_response, estimated_tokens)
            node_id = node_response.get("metadata", {}).get("node_id", "unknown")

            record = APIRequestRecord(
                request_id=request_id,
                address=address,
                model=model,
                tier=tier,
                estimated_tokens=estimated_tokens,
                actual_tokens=actual_tokens,
                burn_amount=burn_amount,
                node_id=node_id,
                start_time=start_time,
                end_time=end_time,
                status="success",
                input_tokens=node_response.get("usage", {}).get("prompt_tokens", estimated_tokens // 2),
                output_tokens=node_response.get("usage", {}).get("completion_tokens", estimated_tokens // 2),
            )

            # Step 11: 记录请求
            try:
                self._mining_engine.record_request(record)
            except Exception as e:
                logger.warning("记录请求失败 (不影响响应): request=%s, error=%s", request_id, e)

            # 更新统计
            self._update_stats(success=True, tokens=actual_tokens, burned=burn_amount)

            # Step 12: 确保 OpenAI 兼容的响应格式
            response = self._ensure_openai_format(node_response, request_id, model, cleaned_body)
            return response

        except Exception as e:
            logger.exception("处理聊天请求异常: request=%s", request_id)
            return self._make_error_response(
                error_type="server_error",
                message=f"内部服务器错误: {str(e)}",
                status_code=500,
                request_id=request_id,
            )

    async def handle_completions(self, request: dict) -> dict:
        """POST /v1/completions - OpenAI 兼容的文本补全接口

        Args:
            request: 完整的 HTTP 请求数据

        Returns:
            OpenAI 兼容的 JSON 响应
        """
        request_id = self._generate_request_id()
        start_time = time.time()

        try:
            headers = request.get("headers", {})
            body = request.get("body", {})

            # 验证请求
            is_valid, error_msg, cleaned_body = self._validator.validate_completions_request(body)
            if not is_valid:
                return self._make_error_response(
                    error_type="invalid_request_error",
                    message=error_msg,
                    status_code=400,
                    request_id=request_id,
                )

            model = cleaned_body["model"]

            # 认证
            auth_result = self._authenticator.authenticate_request(headers)
            if not auth_result["authenticated"]:
                return self._make_error_response(
                    error_type="authentication_error",
                    message=auth_result["error"],
                    status_code=401,
                    request_id=request_id,
                )

            address = auth_result["address"]
            tier = headers.get("x-aicoin-tier", self._default_tier).strip().lower()
            if tier not in TIER_MULTIPLIERS:
                tier = self._default_tier

            input_tokens, output_tokens = self.estimate_tokens(cleaned_body)
            estimated_tokens = input_tokens + output_tokens

            # 速率限制
            rate_ok, rate_error = self._rate_limiter.check_rate_limit(address, tier, estimated_tokens)
            if not rate_ok:
                return self._make_error_response(
                    error_type="rate_limit_error",
                    message=rate_error,
                    status_code=429,
                    request_id=request_id,
                )

            burn_amount = self.calculate_burn_amount(model, input_tokens, output_tokens, tier)

            if not self._authenticator.check_balance(address, burn_amount):
                return self._make_error_response(
                    error_type="insufficient_funds_error",
                    message=f"AICoin 余额不足: 需要 {burn_amount / 1e8:.6f} AIC",
                    status_code=402,
                    request_id=request_id,
                )

            if not self._blockchain.burn_for_api_access(address, burn_amount, request_id):
                return self._make_error_response(
                    error_type="payment_error",
                    message="代币燃烧失败",
                    status_code=402,
                    request_id=request_id,
                )

            # 转发请求
            node_response = await self.forward_with_fallback(cleaned_body, model, tier)

            end_time = time.time()
            actual_tokens = self._extract_token_count(node_response, estimated_tokens)
            node_id = node_response.get("metadata", {}).get("node_id", "unknown")

            record = APIRequestRecord(
                request_id=request_id,
                address=address,
                model=model,
                tier=tier,
                estimated_tokens=estimated_tokens,
                actual_tokens=actual_tokens,
                burn_amount=burn_amount,
                node_id=node_id,
                start_time=start_time,
                end_time=end_time,
                status="success",
            )

            try:
                self._mining_engine.record_request(record)
            except Exception as e:
                logger.warning("记录请求失败: request=%s, error=%s", request_id, e)

            self._update_stats(success=True, tokens=actual_tokens, burned=burn_amount)

            return self._ensure_completions_format(node_response, request_id, model, cleaned_body)

        except Exception as e:
            logger.exception("处理补全请求异常: request=%s", request_id)
            return self._make_error_response(
                error_type="server_error",
                message=f"内部服务器错误: {str(e)}",
                status_code=500,
                request_id=request_id,
            )

    async def handle_models(self) -> dict:
        """GET /v1/models - 列出可用模型和价格

        返回 OpenAI 兼容的模型列表格式。

        Returns:
            模型列表响应:
            {
                "object": "list",
                "data": [
                    {
                        "id": "aicoin-llama-7b",
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "AICoin Network",
                        ...
                    },
                    ...
                ]
            }
        """
        models = []
        for model_id, model_info in SUPPORTED_MODELS.items():
            models.append({
                "id": model_id,
                "object": "model",
                "created": 1700000000,
                "owned_by": model_info.get("owner", "AICoin Network"),
                "permission": [],
                "root": model_id,
                "parent": None,
                "context_window": model_info.get("context_window", 4096),
                "max_output_tokens": model_info.get("max_output_tokens", 2048),
                "pricing": model_info.get("pricing", {}),
            })

        return {
            "object": "list",
            "data": models,
        }

    async def handle_balance(self, address: str) -> dict:
        """GET /v1/balance/{address} - 查询 AICoin 余额

        Args:
            address: 钱包地址

        Returns:
            余额信息:
            {
                "address": "0x...",
                "balance": 100000000,  # 最小单位
                "balance_aic": "1.00000000",
                "tier": "basic",
                "rate_limits": {...}
            }
        """
        if not self._authenticator._validate_address_format(address):
            return {
                "error": "invalid_address",
                "message": f"无效的钱包地址: {address}",
            }

        try:
            balance = self._blockchain.get_balance(address)

            # 推断层级 (基于历史用量或余额)
            tier = self._default_tier

            return {
                "object": "balance",
                "address": address,
                "balance": balance,
                "balance_aic": f"{balance / 1e8:.8f}",
                "tier": tier,
                "rate_limits": self._rate_limiter.get_remaining_quota(address, tier),
            }
        except Exception as e:
            logger.error("查询余额异常: address=%s, error=%s", address, e)
            return {
                "error": "query_failed",
                "message": f"查询余额失败: {str(e)}",
            }

    async def handle_pricing(self) -> dict:
        """GET /v1/pricing - 查询各层级价格

        Returns:
            价格信息:
            {
                "object": "pricing",
                "tiers": {
                    "basic": {
                        "rate_per_1k_tokens": 0.01,
                        "rate_per_1k_tokens_raw": 1000000,
                        "rate_limits": {...}
                    },
                    ...
                },
                "models": {...}
            }
        """
        tiers = {}
        for tier_name, multiplier in TIER_MULTIPLIERS.items():
            tiers[tier_name] = {
                "multiplier": multiplier,
                "rate_limits": DEFAULT_RATE_LIMITS.get(tier_name, {}),
                "description": f"模型基础价格 × {multiplier}x 倍率",
            }

        models = {}
        for model_id, model_info in SUPPORTED_MODELS.items():
            models[model_id] = {
                "display_name": model_info.get("display_name", model_id),
                "pricing": model_info.get("pricing", {}),
                "input_rate_raw": model_info.get("input_rate", 0),
                "output_rate_raw": model_info.get("output_rate", 0),
                "context_window": model_info.get("context_window", 4096),
                "max_output_tokens": model_info.get("max_output_tokens", 2048),
            }

        return {
            "object": "pricing",
            "tiers": tiers,
            "models": models,
            "currency": "AIC",
        }

    # ================================================================
    # 请求转发
    # ================================================================

    async def forward_to_node(
        self,
        node_id: str,
        request: dict,
        timeout: float = 30.0,
    ) -> dict:
        """将请求转发到指定节点

        使用 aiohttp 调用节点的 OpenAI 兼容 API。
        包含超时处理和连接管理。

        Args:
            node_id: 目标节点 ID
            request: 要转发的请求数据
            timeout: 请求超时 (秒)

        Returns:
            节点的响应数据

        Raises:
            asyncio.TimeoutError: 请求超时
            aiohttp.ClientError: 网络错误
            RuntimeError: 节点不可用
        """
        # 获取节点信息
        node_info = self._get_node_info(node_id)
        if not node_info:
            raise RuntimeError(f"节点不存在或不可用: {node_id}")

        if not node_info.is_active:
            raise RuntimeError(f"节点未激活: {node_id}")

        url = f"http://{node_info.host}:{node_info.port}/v1/chat/completions"

        session = self._get_client_session()
        if not session:
            raise RuntimeError("HTTP 客户端会话未初始化")

        # 添加转发元数据
        forwarded_request = dict(request)
        if "metadata" not in forwarded_request:
            forwarded_request["metadata"] = {}
        forwarded_request["metadata"]["gateway_request_id"] = request.get(
            "request_id", self._generate_request_id()
        )

        logger.info(
            "转发请求到节点: node=%s, url=%s, timeout=%.1fs",
            node_id,
            url,
            timeout,
        )

        try:
            async with session.post(
                url,
                json=forwarded_request,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    error_body = await resp.text()
                    logger.warning(
                        "节点返回错误: node=%s, status=%d, body=%s",
                        node_id,
                        resp.status,
                        error_body[:500],
                    )
                    raise RuntimeError(
                        f"节点返回 HTTP {resp.status}: {error_body[:200]}"
                    )

                result = await resp.json()
                result.setdefault("metadata", {})["node_id"] = node_id
                result["metadata"]["node_address"] = node_info.address
                result["metadata"]["latency_ms"] = int(
                    (time.time() - forwarded_request["metadata"].get("timestamp", time.time()))
                    * 1000
                )
                return result

        except asyncio.TimeoutError:
            logger.warning("节点请求超时: node=%s, timeout=%.1fs", node_id, timeout)
            raise
        except aiohttp.ClientError as e:
            logger.warning("节点网络错误: node=%s, error=%s", node_id, e)
            raise

    async def forward_with_fallback(
        self,
        request: dict,
        model_name: str,
        priority: str,
    ) -> dict:
        """带故障转移的请求转发

        按顺序尝试: 主节点 → 备用节点1 → 备用节点2 → ... → 错误

        每次失败后会等待一段时间 (指数退避) 再尝试下一个节点。

        Args:
            request: 请求数据
            model_name: 模型名称
            priority: 优先级层级

        Returns:
            第一个成功节点的响应

        Raises:
            RuntimeError: 所有节点都不可用
        """
        # 获取主节点
        primary_node = self._router.find_best_node(model_name, priority)
        if not primary_node:
            raise RuntimeError(f"没有可用的计算节点: model={model_name}, tier={priority}")

        # 构建尝试节点列表
        nodes_to_try = [primary_node]
        exclude_ids = {primary_node.node_id}

        # 获取备用节点
        try:
            backup_nodes = self._router.get_backup_nodes(
                model_name, priority, exclude_ids
            )
            nodes_to_try.extend(backup_nodes[:self._max_retries - 1])
        except Exception as e:
            logger.warning("获取备用节点失败: %s", e)

        last_error: Optional[Exception] = None

        for attempt, node in enumerate(nodes_to_try):
            try:
                logger.info(
                    "尝试请求: attempt=%d/%d, node=%s, model=%s",
                    attempt + 1,
                    len(nodes_to_try),
                    node.node_id,
                    model_name,
                )

                timeout = self._request_timeout * (1 - 0.15 * attempt)
                timeout = max(timeout, 10.0)  # 最低 10 秒超时

                result = await self.forward_to_node(node.node_id, request, timeout=timeout)

                logger.info(
                    "请求成功: node=%s, attempt=%d, model=%s",
                    node.node_id,
                    attempt + 1,
                    model_name,
                )
                return result

            except asyncio.TimeoutError as e:
                last_error = e
                logger.warning(
                    "节点超时: node=%s, attempt=%d, timeout=%.1fs",
                    node.node_id,
                    attempt + 1,
                    timeout,
                )
                # 标记节点可能有问题
                self._mark_node_degraded(node.node_id)

            except (aiohttp.ClientError, RuntimeError) as e:
                last_error = e
                logger.warning(
                    "节点错误: node=%s, attempt=%d, error=%s",
                    node.node_id,
                    attempt + 1,
                    e,
                )
                self._mark_node_degraded(node.node_id)

            # 指数退避等待
            if attempt < len(nodes_to_try) - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.info("等待 %.2f 秒后尝试下一个节点...", delay)
                await asyncio.sleep(delay)

        # 所有节点都失败了
        raise RuntimeError(
            f"所有 {len(nodes_to_try)} 个计算节点均不可用: "
            f"model={model_name}, tier={priority}, last_error={last_error}"
        )

    # ================================================================
    # 计费
    # ================================================================

    def calculate_burn_amount(
        self, model: str, input_tokens: int, output_tokens: int, tier: str
    ) -> int:
        """计算需要燃烧的 AICoin 数量（输入/输出分离 + 模型分级 + 优先级倍率）

        计算公式:
            input_cost = ceil(input_tokens / 1000) * model.input_rate * tier_multiplier
            output_cost = ceil(output_tokens / 1000) * model.output_rate * tier_multiplier
            total = input_cost + output_cost

        Args:
            model: 模型名称
            input_tokens: 预估的输入 token 数量
            output_tokens: 预估的输出 token 数量
            tier: 优先级层级 (basic/premium/priority)

        Returns:
            需要燃烧的 AICoin 数量 (最小单位, 10^8 = 1 AIC)
        """
        # 获取模型定价
        model_info = SUPPORTED_MODELS.get(model)
        if model_info is None:
            # 未知模型使用默认小模型定价
            input_rate = 100_000   # 0.001 AIC / 1K
            output_rate = 300_000  # 0.003 AIC / 1K
        else:
            input_rate = model_info.get("input_rate", 100_000)
            output_rate = model_info.get("output_rate", 300_000)

        # 获取优先级倍率
        multiplier = TIER_MULTIPLIERS.get(tier, 1.0)

        # 向上取整到 1K token 的倍数
        input_1k = (input_tokens + 999) // 1000
        output_1k = (output_tokens + 999) // 1000

        input_cost = int(input_1k * input_rate * multiplier)
        output_cost = int(output_1k * output_rate * multiplier)

        total = input_cost + output_cost

        # 最低消费 1 个最小单位
        return max(total, 1)

    def estimate_tokens(self, request: dict) -> Tuple[int, int]:
        """估算请求的输入和输出 token 数量（分别返回）

        基于消息/提示长度估算输入，基于 max_tokens 参数估算输出。
        使用简化的经验公式: ~4 字符 = 1 token

        Args:
            request: 请求数据 (chat 或 completions 格式)

        Returns:
            (input_tokens, output_tokens) 元组
        """
        input_tokens = 0

        # 聊天格式
        messages = request.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    input_tokens += len(content) // 4
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            input_tokens += len(part.get("text", "")) // 4
                        elif isinstance(part, dict) and part.get("type") == "image_url":
                            input_tokens += 1000  # 图片约 1000 tokens
                # 每条消息的格式开销
                input_tokens += 4

        # 补全格式
        prompt = request.get("prompt")
        if isinstance(prompt, str):
            input_tokens = len(prompt) // 4
        elif isinstance(prompt, list):
            for p in prompt:
                if isinstance(p, str):
                    input_tokens += len(p) // 4

        # 估算输出 token 数
        max_tokens = request.get("max_tokens")
        if max_tokens is not None and isinstance(max_tokens, (int, float)):
            output_tokens = int(max_tokens)
        else:
            # 默认输出 token 数
            output_tokens = 1024

        # 最低保底
        input_tokens = max(input_tokens, 10)
        output_tokens = max(output_tokens, 10)

        return input_tokens, output_tokens

    # ================================================================
    # HTTP 服务器
    # ================================================================

    def start_server(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """启动 API 网关 HTTP 服务器

        在独立线程中运行 aiohttp 异步事件循环。

        路由:
          POST /v1/chat/completions  - 聊天补全
          POST /v1/completions        - 文本补全
          GET  /v1/models             - 可用模型列表
          GET  /v1/balance/{address}  - 余额查询
          GET  /v1/pricing            - 价格查询
          GET  /health                - 健康检查
          GET  /stats                 - 网关统计

        Args:
            host: 监听地址
            port: 监听端口
        """
        if self._is_running:
            logger.warning("API 网关已在运行中")
            return

        self._app = web.Application()
        self._setup_routes()
        self._setup_middlewares()

        self._runner = web.AppRunner(self._app)
        loop = asyncio.new_event_loop()

        async def _run():
            await self._runner.setup()
            site = web.TCPSite(self._runner, host, port)
            await site.start()
            logger.info("API 网关已启动: http://%s:%d", host, port)
            # 保持运行
            while self._is_running:
                await asyncio.sleep(1)

        self._is_running = True

        def _thread_target():
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_run())
            except Exception as e:
                logger.error("服务器线程异常: %s", e)
            finally:
                loop.close()

        self._server_thread = threading.Thread(
            target=_thread_target,
            name="api-gateway-server",
            daemon=True,
        )
        self._server_thread.start()
        logger.info("API 网关服务器线程已启动")

    def stop_server(self) -> None:
        """停止服务器

        优雅关闭: 等待当前请求完成, 释放所有资源。
        """
        if not self._is_running:
            logger.warning("API 网关未在运行")
            return

        self._is_running = False
        logger.info("正在停止 API 网关...")

        async def _cleanup():
            if self._runner:
                await self._runner.cleanup()
            await self._close_client_session()

        # 在新的循环中执行清理
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_cleanup())
            loop.close()
        except Exception as e:
            logger.error("清理异常: %s", e)

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5.0)

        logger.info("API 网关已停止")

    def _setup_routes(self) -> None:
        """设置 HTTP 路由"""
        if not self._app:
            return

        router = self._app.router
        router.add_post("/v1/chat/completions", self._http_handle_chat_completions)
        router.add_post("/v1/completions", self._http_handle_completions)
        router.add_get("/v1/models", self._http_handle_models)
        router.add_get("/v1/balance/{address}", self._http_handle_balance)
        router.add_get("/v1/pricing", self._http_handle_pricing)
        router.add_get("/health", self._http_handle_health)
        router.add_get("/stats", self._http_handle_stats)

        logger.info("HTTP 路由设置完成: 7 个端点")

    def _setup_middlewares(self) -> None:
        """设置中间件 (CORS, 请求日志, 错误处理)"""
        if not self._app:
            return

        @web.middleware
        async def cors_middleware(request: web.Request, handler):
            """CORS 中间件"""
            if request.method == "OPTIONS":
                response = web.Response(status=204)
            else:
                response = await handler(request)

            # 设置 CORS 头
            response.headers["Access-Control-Allow-Origin"] = self._cors_origins
            response.headers["Access-Control-Allow-Methods"] = self._cors_methods
            response.headers["Access-Control-Allow-Headers"] = self._cors_headers
            response.headers["Access-Control-Max-Age"] = "86400"

            return response

        @web.middleware
        async def logging_middleware(request: web.Request, handler):
            """请求日志中间件"""
            start = time.time()
            try:
                response = await handler(request)
                duration = (time.time() - start) * 1000
                logger.info(
                    "%s %s -> %d (%.1fms)",
                    request.method,
                    request.path,
                    response.status,
                    duration,
                )
                return response
            except Exception as e:
                duration = (time.time() - start) * 1000
                logger.error(
                    "%s %s -> ERROR (%.1fms): %s",
                    request.method,
                    request.path,
                    duration,
                    e,
                )
                raise

        @web.middleware
        async def error_handler_middleware(request: web.Request, handler):
            """全局错误处理中间件"""
            try:
                return await handler(request)
            except web.HTTPNotFound:
                return web.json_response(
                    {
                        "error": {
                            "type": "not_found",
                            "message": f"端点不存在: {request.method} {request.path}",
                        }
                    },
                    status=404,
                )
            except web.HTTPMethodNotAllowed:
                return web.json_response(
                    {
                        "error": {
                            "type": "method_not_allowed",
                            "message": f"方法不允许: {request.method} {request.path}",
                        }
                    },
                    status=405,
                )
            except json.JSONDecodeError as e:
                return web.json_response(
                    {
                        "error": {
                            "type": "invalid_json",
                            "message": f"请求体 JSON 格式无效: {str(e)}",
                        }
                    },
                    status=400,
                )
            except Exception as e:
                logger.exception("未处理的请求异常: %s %s", request.method, request.path)
                return web.json_response(
                    {
                        "error": {
                            "type": "server_error",
                            "message": f"内部服务器错误: {str(e)}",
                        }
                    },
                    status=500,
                )

        # 中间件按顺序执行 (最后注册的最先执行)
        self._app.middlewares.append(error_handler_middleware)
        self._app.middlewares.append(logging_middleware)
        self._app.middlewares.append(cors_middleware)

    # ================================================================
    # HTTP 处理函数
    # ================================================================

    async def _http_handle_chat_completions(self, request: web.Request) -> web.Response:
        """处理 POST /v1/chat/completions"""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception) as e:
            return web.json_response(
                {
                    "error": {
                        "type": "invalid_json",
                        "message": f"请求体 JSON 格式无效: {str(e)}",
                    }
                },
                status=400,
            )

        headers = dict(request.headers)
        result = await self.handle_chat_completions({"headers": headers, "body": body})

        status_code = result.get("_status_code", 200)
        if "_status_code" in result:
            del result["_status_code"]
        return web.json_response(result, status=status_code)

    async def _http_handle_completions(self, request: web.Request) -> web.Response:
        """处理 POST /v1/completions"""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception) as e:
            return web.json_response(
                {
                    "error": {
                        "type": "invalid_json",
                        "message": f"请求体 JSON 格式无效: {str(e)}",
                    }
                },
                status=400,
            )

        headers = dict(request.headers)
        result = await self.handle_completions({"headers": headers, "body": body})

        status_code = result.get("_status_code", 200)
        if "_status_code" in result:
            del result["_status_code"]
        return web.json_response(result, status=status_code)

    async def _http_handle_models(self, request: web.Request) -> web.Response:
        """处理 GET /v1/models"""
        result = await self.handle_models()
        return web.json_response(result)

    async def _http_handle_balance(self, request: web.Request) -> web.Response:
        """处理 GET /v1/balance/{address}"""
        address = request.match_info.get("address", "")
        result = await self.handle_balance(address)
        status_code = 200 if "error" not in result else 400
        return web.json_response(result, status=status_code)

    async def _http_handle_pricing(self, request: web.Request) -> web.Response:
        """处理 GET /v1/pricing"""
        result = await self.handle_pricing()
        return web.json_response(result)

    async def _http_handle_health(self, request: web.Request) -> web.Response:
        """处理 GET /health"""
        health_status = {
            "status": "healthy" if self._is_running else "unhealthy",
            "service": "aicoin-api-gateway",
            "version": "1.0.0",
            "uptime_seconds": int(time.time() - self._start_time),
            "stats": self.stats,
        }

        # 检查依赖服务
        try:
            self._blockchain.get_balance("0x" + "0" * 40)
            health_status["blockchain"] = "connected"
        except Exception:
            health_status["blockchain"] = "disconnected"

        status_code = 200 if health_status["status"] == "healthy" else 503
        return web.json_response(health_status, status=status_code)

    async def _http_handle_stats(self, request: web.Request) -> web.Response:
        """处理 GET /stats"""
        return web.json_response(self.stats)

    # ================================================================
    # 内部辅助方法
    # ================================================================

    def _get_client_session(self) -> Optional[aiohttp.ClientSession]:
        """获取或创建 HTTP 客户端会话

        使用懒加载和线程安全的方式管理会话。

        Returns:
            aiohttp 客户端会话, 如果不可用返回 None
        """
        if self._client_session is not None and not self._client_session.closed:
            return self._client_session

        with self._session_lock:
            if self._client_session is not None and not self._client_session.closed:
                return self._client_session

            try:
                connector = aiohttp.TCPConnector(
                    limit=100,
                    limit_per_host=20,
                    ttl_dns_cache=300,
                    enable_cleanup_closed=True,
                )
                self._client_session = aiohttp.ClientSession(
                    connector=connector,
                    json_serialize=json.dumps,
                    headers={"Content-Type": "application/json"},
                )
                logger.info("HTTP 客户端会话已创建")
                return self._client_session
            except Exception as e:
                logger.error("创建 HTTP 客户端会话失败: %s", e)
                return None

    async def _close_client_session(self) -> None:
        """关闭 HTTP 客户端会话"""
        if self._client_session and not self._client_session.closed:
            await self._client_session.close()
            self._client_session = None
            logger.info("HTTP 客户端会话已关闭")

    def _generate_request_id(self) -> str:
        """生成唯一的请求 ID

        格式: req_<时间戳前8位>_<16位随机hex>

        Returns:
            唯一请求 ID
        """
        timestamp = int(time.time() * 1000) % 10**8
        random_hex = secrets.token_hex(8)
        return f"req_{timestamp}_{random_hex}"

    def _get_node_info(self, node_id: str) -> Optional[NodeInfo]:
        """获取节点信息

        Args:
            node_id: 节点 ID

        Returns:
            节点信息, 不存在则返回 None
        """
        try:
            # 尝试从路由器获取
            if hasattr(self._router, "get_node_info"):
                return self._router.get_node_info(node_id)
        except Exception as e:
            logger.warning("获取节点信息失败: node=%s, error=%s", node_id, e)
        return None

    def _mark_node_degraded(self, node_id: str) -> None:
        """标记节点为降级状态

        当节点请求失败时调用, 降低其在路由选择中的优先级。

        Args:
            node_id: 节点 ID
        """
        try:
            if hasattr(self._router, "mark_node_degraded"):
                self._router.mark_node_degraded(node_id)
            logger.info("节点已标记为降级: %s", node_id)
        except Exception as e:
            logger.warning("标记节点降级失败: node=%s, error=%s", node_id, e)

    def _update_stats(
        self,
        success: bool,
        tokens: int = 0,
        burned: int = 0,
    ) -> None:
        """更新网关统计数据 (线程安全)

        Args:
            success: 请求是否成功
            tokens: 处理的 token 数量
            burned: 燃烧的 AIC 数量
        """
        with self._stats_lock:
            self._total_requests += 1
            if success:
                self._total_success += 1
            else:
                self._total_failed += 1
            self._total_tokens_processed += tokens
            self._total_aic_burned += burned

    def _extract_token_count(
        self,
        response: dict,
        estimated_tokens: int,
    ) -> int:
        """从节点响应中提取实际的 token 使用量

        Args:
            response: 节点响应
            estimated_tokens: 预估的 token 数量

        Returns:
            实际使用的 token 数量, 如果无法提取则使用预估值
        """
        try:
            usage = response.get("usage", {})
            if usage:
                return usage.get("total_tokens", estimated_tokens)
        except Exception:
            pass
        return estimated_tokens

    # ================================================================
    # OpenAI 兼容格式化
    # ================================================================

    @staticmethod
    def _make_error_response(
        error_type: str,
        message: str,
        status_code: int,
        request_id: str,
    ) -> dict:
        """构建 OpenAI 兼容的错误响应

        OpenAI 错误响应格式:
        {
            "error": {
                "type": "<error_type>",
                "message": "<message>",
                "param": null,
                "code": null
            }
        }

        Args:
            error_type: 错误类型
            message: 错误消息
            status_code: HTTP 状态码
            request_id: 请求 ID

        Returns:
            OpenAI 兼容的错误响应
        """
        return {
            "error": {
                "type": error_type,
                "message": message,
                "param": None,
                "code": str(status_code),
            },
            "_status_code": status_code,
            "id": request_id,
            "object": "error",
        }

    def _ensure_openai_format(
        self,
        node_response: dict,
        request_id: str,
        model: str,
        original_request: dict,
    ) -> dict:
        """确保聊天补全响应格式符合 OpenAI 规范

        OpenAI Chat Completions 响应格式:
        {
            "id": "chatcmpl-...",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "...",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "..."},
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30
            }
        }

        Args:
            node_response: 节点原始响应
            request_id: 请求 ID
            model: 模型名称
            original_request: 原始请求

        Returns:
            OpenAI 兼容的响应
        """
        # 如果响应已经是 OpenAI 格式, 只补充缺失字段
        if node_response.get("object") == "chat.completion":
            node_response.setdefault("id", f"chatcmpl-{request_id}")
            node_response.setdefault("created", int(time.time()))
            node_response.setdefault("model", model)
            return node_response

        # 构建标准 OpenAI 格式
        created = int(time.time())
        choices = node_response.get("choices", [])

        # 如果节点返回的不是标准格式, 尝试适配
        if not choices:
            # 尝试从其他字段构建 choices
            content = ""
            if isinstance(node_response, dict):
                # 尝试常见的非标准响应格式
                content = (
                    node_response.get("response")
                    or node_response.get("output")
                    or node_response.get("text")
                    or node_response.get("content")
                    or json.dumps(node_response, ensure_ascii=False)
                )

            choices = [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": str(content),
                    },
                    "finish_reason": "stop",
                }
            ]

        # 标准化 choices 格式
        normalized_choices = []
        for i, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue

            message = choice.get("message", {})
            if not isinstance(message, dict):
                message = {"role": "assistant", "content": str(message)}

            message.setdefault("role", "assistant")

            normalized_choices.append(
                {
                    "index": choice.get("index", i),
                    "message": message,
                    "finish_reason": choice.get("finish_reason", "stop"),
                }
            )

        if not normalized_choices:
            normalized_choices = [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "stop",
                }
            ]

        usage = node_response.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}

        response = {
            "id": node_response.get("id", f"chatcmpl-{request_id}"),
            "object": "chat.completion",
            "created": node_response.get("created", created),
            "model": node_response.get("model", model),
            "choices": normalized_choices,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        # 保留 metadata (AICoin 扩展)
        if "metadata" in node_response:
            response["metadata"] = node_response["metadata"]

        return response

    def _ensure_completions_format(
        self,
        node_response: dict,
        request_id: str,
        model: str,
        original_request: dict,
    ) -> dict:
        """确保文本补全响应格式符合 OpenAI 规范

        OpenAI Completions 响应格式:
        {
            "id": "cmpl-...",
            "object": "text_completion",
            "created": 1700000000,
            "model": "...",
            "choices": [
                {
                    "index": 0,
                    "text": "...",
                    "finish_reason": "stop"
                }
            ],
            "usage": {...}
        }

        Args:
            node_response: 节点原始响应
            request_id: 请求 ID
            model: 模型名称
            original_request: 原始请求

        Returns:
            OpenAI 兼容的响应
        """
        if node_response.get("object") == "text_completion":
            node_response.setdefault("id", f"cmpl-{request_id}")
            node_response.setdefault("created", int(time.time()))
            node_response.setdefault("model", model)
            return node_response

        created = int(time.time())
        choices = node_response.get("choices", [])

        if not choices:
            content = ""
            if isinstance(node_response, dict):
                content = (
                    node_response.get("response")
                    or node_response.get("output")
                    or node_response.get("text")
                    or node_response.get("content")
                    or json.dumps(node_response, ensure_ascii=False)
                )

            choices = [
                {
                    "index": 0,
                    "text": str(content),
                    "finish_reason": "stop",
                }
            ]

        normalized_choices = []
        for i, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            normalized_choices.append(
                {
                    "index": choice.get("index", i),
                    "text": choice.get("text", ""),
                    "finish_reason": choice.get("finish_reason", "stop"),
                    "logprobs": choice.get("logprobs", None),
                }
            )

        usage = node_response.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}

        response = {
            "id": node_response.get("id", f"cmpl-{request_id}"),
            "object": "text_completion",
            "created": node_response.get("created", created),
            "model": node_response.get("model", model),
            "choices": normalized_choices,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        if "metadata" in node_response:
            response["metadata"] = node_response["metadata"]

        return response
