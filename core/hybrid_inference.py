"""
AICoin 混合推理引擎 - Hybrid Inference Engine
================================================

解决分布式 AI 推理中的三大核心问题：
1. 节点动态波动 (Churn) → 熔断器 + 会话亲和性 + 热备切换
2. 算力极度冗余 (Over-provisioning) → 自适应节点池缩容 + 智能调度
3. 大模型单节点装不下 (Model Parallelism) → 张量并行 + 流水线并行

核心组件:
    - ModelClassifier:     模型分级器 (按参数量自动分级)
    - CircuitBreaker:      熔断器 (自动隔离故障节点)
    - SessionAffinity:     会话亲和性管理器 (同一会话路由到同一节点/集群)
    - NodeCluster:         推理集群 (多节点协作执行)
    - ClusterManager:      集群管理器 (自动组建、维护、解散节点集群)
    - HybridScheduler:     混合调度器 (根据模型大小选择最优推理模式)

推理模式:
    STANDALONE:    小模型 (≤8B) → 单节点完整推理
    TENSOR_PARALLEL: 中大型模型 (9B-72B) → 多节点张量并行 (权重分片)
    PIPELINE_PARALLEL: 超大模型 (>72B) → 流水线并行 (层分段)
    HYBRID:        极大模型 (>120B) → 张量 + 流水线混合并行

版本: 2.0.0
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

logger = logging.getLogger("aicoin.hybrid_inference")

__all__ = [
    "ModelTier",
    "InferenceMode",
    "ModelProfile",
    "CircuitState",
    "CircuitBreaker",
    "SessionAffinity",
    "NodeSlot",
    "NodeCluster",
    "ClusterManager",
    "TierRedundancyPolicy",
    "HYBRID_CONFIG",
    "HybridScheduler",
]


# =====================================================================
#  常量与枚举
# =====================================================================

class ModelTier(Enum):
    """模型大小分级"""
    TINY = "tiny"              # ≤ 8B 参数
    SMALL = "small"            # 9B - 15B
    MEDIUM = "medium"          # 16B - 40B
    LARGE = "large"            # 41B - 72B
    XLARGE = "xlarge"          # 73B - 120B
    MASSIVE = "massive"        # > 120B


class InferenceMode(Enum):
    """推理执行模式"""
    STANDALONE = "standalone"            # 单节点独立推理
    TENSOR_PARALLEL = "tensor_parallel"  # 多节点张量并行 (同层权重分片)
    PIPELINE_PARALLEL = "pipeline_parallel"  # 多节点流水线并行 (层分段)
    HYBRID_PARALLEL = "hybrid_parallel"  # 张量 + 流水线混合


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"      # 正常（放行请求）
    OPEN = "open"          # 熔断（拒绝请求）
    HALF_OPEN = "half_open"  # 半开（试探性放行）


# =====================================================================
#  全局模型配置表
# =====================================================================

# 模型参数量 → 分级 → 推理模式 → 所需最小节点数 → 单节点最小 VRAM
MODEL_CATALOG: Dict[str, Dict[str, Any]] = {
    # ---- Tiny (≤8B) ----
    "aicoin-llama-7b": {
        "params_b": 7, "tier": ModelTier.TINY,
        "mode": InferenceMode.STANDALONE, "min_nodes": 1,
        "min_vram_gb": 8, "weight_gb": 14,
    },
    "aicoin-mistral-7b": {
        "params_b": 7, "tier": ModelTier.TINY,
        "mode": InferenceMode.STANDALONE, "min_nodes": 1,
        "min_vram_gb": 8, "weight_gb": 14,
    },
    "qwen2.5-0.5b": {
        "params_b": 0.5, "tier": ModelTier.TINY,
        "mode": InferenceMode.STANDALONE, "min_nodes": 1,
        "min_vram_gb": 2, "weight_gb": 1,
    },
    "qwen2.5-1.5b": {
        "params_b": 1.5, "tier": ModelTier.TINY,
        "mode": InferenceMode.STANDALONE, "min_nodes": 1,
        "min_vram_gb": 3, "weight_gb": 3,
    },
    "qwen2.5-3b": {
        "params_b": 3, "tier": ModelTier.TINY,
        "mode": InferenceMode.STANDALONE, "min_nodes": 1,
        "min_vram_gb": 5, "weight_gb": 6,
    },
    "qwen2.5-7b": {
        "params_b": 7, "tier": ModelTier.TINY,
        "mode": InferenceMode.STANDALONE, "min_nodes": 1,
        "min_vram_gb": 8, "weight_gb": 14,
    },
    "llama-3-8b": {
        "params_b": 8, "tier": ModelTier.TINY,
        "mode": InferenceMode.STANDALONE, "min_nodes": 1,
        "min_vram_gb": 10, "weight_gb": 16,
    },
    # ---- Small (9-15B) ----
    "aicoin-llama-13b": {
        "params_b": 13, "tier": ModelTier.SMALL,
        "mode": InferenceMode.STANDALONE, "min_nodes": 1,
        "min_vram_gb": 16, "weight_gb": 26,
    },
    "qwen2.5-14b": {
        "params_b": 14, "tier": ModelTier.SMALL,
        "mode": InferenceMode.STANDALONE, "min_nodes": 1,
        "min_vram_gb": 16, "weight_gb": 28,
    },
    # ---- Medium (16-40B) ----
    "aicoin-coder-34b": {
        "params_b": 34, "tier": ModelTier.MEDIUM,
        "mode": InferenceMode.TENSOR_PARALLEL, "min_nodes": 2,
        "min_vram_gb": 24, "weight_gb": 68,
    },
    "qwen2.5-32b": {
        "params_b": 32, "tier": ModelTier.MEDIUM,
        "mode": InferenceMode.TENSOR_PARALLEL, "min_nodes": 2,
        "min_vram_gb": 24, "weight_gb": 64,
    },
    # ---- Large (41-72B) ----
    "aicoin-llama-70b": {
        "params_b": 70, "tier": ModelTier.LARGE,
        "mode": InferenceMode.TENSOR_PARALLEL, "min_nodes": 4,
        "min_vram_gb": 24, "weight_gb": 140,
    },
    "aicoin-qwen-72b": {
        "params_b": 72, "tier": ModelTier.LARGE,
        "mode": InferenceMode.TENSOR_PARALLEL, "min_nodes": 4,
        "min_vram_gb": 24, "weight_gb": 144,
    },
    "llama-3-70b": {
        "params_b": 70, "tier": ModelTier.LARGE,
        "mode": InferenceMode.TENSOR_PARALLEL, "min_nodes": 4,
        "min_vram_gb": 24, "weight_gb": 140,
    },
}


@dataclass
class ModelProfile:
    """模型画像 — 从 MODEL_CATALOG 构建的运行时缓存"""
    name: str
    params_b: float
    tier: ModelTier
    preferred_mode: InferenceMode
    min_nodes: int
    min_vram_gb: float
    weight_gb: float
    estimated_latency_factor: float = 1.0  # 相对延迟系数

    @classmethod
    def from_catalog(cls, model_name: str) -> "ModelProfile":
        """从模型目录查找或自动估算"""
        info = MODEL_CATALOG.get(model_name)
        if info:
            return cls(
                name=model_name,
                params_b=info["params_b"],
                tier=info["tier"],
                preferred_mode=info["mode"],
                min_nodes=info["min_nodes"],
                min_vram_gb=info["min_vram_gb"],
                weight_gb=info["weight_gb"],
                estimated_latency_factor=cls._calc_latency_factor(info["params_b"]),
            )
        # 自动估算 (从未知模型名中提取参数量)
        return cls._estimate(model_name)

    @staticmethod
    def _calc_latency_factor(params_b: float) -> float:
        """基于参数量估算相对延迟系数（以 7B 为基准=1.0）"""
        if params_b <= 1:
            return 0.3
        elif params_b <= 8:
            return 0.5 + (params_b / 8) * 0.5
        elif params_b <= 15:
            return 1.0 + (params_b - 8) / 7 * 0.5
        elif params_b <= 40:
            return 1.5 + (params_b - 15) / 25 * 1.5
        elif params_b <= 72:
            return 3.0 + (params_b - 40) / 32 * 2.0
        else:
            return 5.0 + (params_b - 72) / 48 * 3.0

    @classmethod
    def _estimate(cls, model_name: str) -> "ModelProfile":
        """从模型名自动估算参数量并分级"""
        name_lower = model_name.lower()
        estimated_b = 7.0  # 默认 7B

        # 尝试从名称提取参数量
        import re
        patterns = [
            r"(\d+)b", r"(\d+\.?\d*)b", r"-(\d+)b",
        ]
        for pat in patterns:
            m = re.search(pat, name_lower)
            if m:
                try:
                    estimated_b = float(m.group(1))
                    break
                except ValueError:
                    continue

        # 自动分级
        if estimated_b <= 8:
            tier = ModelTier.TINY
            mode = InferenceMode.STANDALONE
            min_nodes = 1
        elif estimated_b <= 15:
            tier = ModelTier.SMALL
            mode = InferenceMode.STANDALONE
            min_nodes = 1
        elif estimated_b <= 40:
            tier = ModelTier.MEDIUM
            mode = InferenceMode.TENSOR_PARALLEL
            min_nodes = 2
        elif estimated_b <= 72:
            tier = ModelTier.LARGE
            mode = InferenceMode.TENSOR_PARALLEL
            min_nodes = 4
        elif estimated_b <= 120:
            tier = ModelTier.XLARGE
            mode = InferenceMode.PIPELINE_PARALLEL
            min_nodes = 4
        else:
            tier = ModelTier.MASSIVE
            mode = InferenceMode.HYBRID_PARALLEL
            min_nodes = 8

        weight_gb = estimated_b * 2.0  # FP16 粗略估算: 1B参数 ≈ 2GB
        min_vram = weight_gb + 4.0  # 运行时额外开销 ≈ 4GB

        return cls(
            name=model_name,
            params_b=estimated_b,
            tier=tier,
            preferred_mode=mode,
            min_nodes=min_nodes,
            min_vram_gb=min_vram,
            weight_gb=weight_gb,
            estimated_latency_factor=cls._calc_latency_factor(estimated_b),
        )


# =====================================================================
#  熔断器 (Circuit Breaker)
# =====================================================================

@dataclass
class CircuitBreaker:
    """
    熔断器 — 自动隔离故障节点，防止级联失败

    状态机:
        CLOSED → (连续失败 >= threshold) → OPEN
        OPEN → (等待 recovery_timeout) → HALF_OPEN
        HALF_OPEN → (探测成功) → CLOSED
        HALF_OPEN → (探测失败) → OPEN

    用途:
        - 当某节点连续失败超过阈值时，自动熔断，不再将请求路由到该节点
        - 熔断后经过恢复期进入半开状态，试探性放行少量请求
        - 试探成功则恢复正常，否则继续熔断
    """
    # --- 可调参数 ---
    failure_threshold: int = 5           # 连续失败次数阈值
    recovery_timeout: float = 30.0       # 熔断恢复时间（秒）
    half_open_max_calls: int = 3         # 半开状态下最大试探请求数
    success_threshold: int = 2           # 半开状态下恢复所需的连续成功数

    # --- 内部状态 ---
    _states: Dict[str, CircuitState] = field(default_factory=dict)
    _failure_counts: Dict[str, int] = field(default_factory=dict)
    _success_counts: Dict[str, int] = field(default_factory=dict)
    _last_failure_time: Dict[str, float] = field(default_factory=dict)
    _half_open_calls: Dict[str, int] = field(default_factory=dict)
    _total_trips: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def is_available(self, node_id: str) -> bool:
        """检查节点是否可用（未被熔断）"""
        with self._lock:
            state = self._states.get(node_id, CircuitState.CLOSED)

            if state == CircuitState.CLOSED:
                return True

            if state == CircuitState.OPEN:
                # 检查是否已到恢复时间
                last_fail = self._last_failure_time.get(node_id, 0)
                if time.time() - last_fail >= self.recovery_timeout:
                    self._states[node_id] = CircuitState.HALF_OPEN
                    self._half_open_calls[node_id] = 0
                    logger.info(
                        "熔断器: 节点 %s 进入半开状态 (等待 %.1fs)",
                        node_id, self.recovery_timeout,
                    )
                    return True
                return False

            if state == CircuitState.HALF_OPEN:
                # 半开状态只允许有限数量的试探请求
                calls = self._half_open_calls.get(node_id, 0)
                if calls < self.half_open_max_calls:
                    self._half_open_calls[node_id] = calls + 1
                    return True
                return False

        return True  # 默认放行

    def record_success(self, node_id: str) -> None:
        """记录成功调用"""
        with self._lock:
            state = self._states.get(node_id, CircuitState.CLOSED)

            if state == CircuitState.HALF_OPEN:
                self._success_counts[node_id] = (
                    self._success_counts.get(node_id, 0) + 1
                )
                if self._success_counts[node_id] >= self.success_threshold:
                    self._states[node_id] = CircuitState.CLOSED
                    self._failure_counts[node_id] = 0
                    self._success_counts[node_id] = 0
                    logger.info("熔断器: 节点 %s 已恢复正常 (CLOSED)", node_id)
            else:
                # 正常状态下成功，重置失败计数
                self._failure_counts[node_id] = 0

    def record_failure(self, node_id: str) -> None:
        """记录失败调用"""
        with self._lock:
            state = self._states.get(node_id, CircuitState.CLOSED)
            self._failure_counts[node_id] = (
                self._failure_counts.get(node_id, 0) + 1
            )
            self._last_failure_time[node_id] = time.time()

            if state == CircuitState.HALF_OPEN:
                # 半开状态下探测失败 → 重新熔断
                self._states[node_id] = CircuitState.OPEN
                self._success_counts[node_id] = 0
                self._half_open_calls[node_id] = 0
                logger.warning(
                    "熔断器: 节点 %s 半开探测失败，重新熔断", node_id
                )
            elif state == CircuitState.CLOSED:
                if self._failure_counts[node_id] >= self.failure_threshold:
                    self._states[node_id] = CircuitState.OPEN
                    self._total_trips += 1
                    logger.warning(
                        "熔断器: 节点 %s 连续失败 %d 次，已熔断 (OPEN)",
                        node_id, self._failure_counts[node_id],
                    )

    def get_state(self, node_id: str) -> CircuitState:
        """获取节点熔断状态"""
        with self._lock:
            return self._states.get(node_id, CircuitState.CLOSED)

    def get_stats(self) -> Dict[str, Any]:
        """获取熔断器统计"""
        with self._lock:
            closed = sum(1 for s in self._states.values() if s == CircuitState.CLOSED)
            open_count = sum(1 for s in self._states.values() if s == CircuitState.OPEN)
            half_open = sum(1 for s in self._states.values() if s == CircuitState.HALF_OPEN)
            return {
                "total_trips": self._total_trips,
                "nodes_closed": closed,
                "nodes_open": open_count,
                "nodes_half_open": half_open,
                "tracked_nodes": len(self._states),
            }

    def reset(self, node_id: Optional[str] = None) -> None:
        """重置熔断状态"""
        with self._lock:
            if node_id:
                self._states.pop(node_id, None)
                self._failure_counts.pop(node_id, None)
                self._success_counts.pop(node_id, None)
                self._last_failure_time.pop(node_id, None)
                self._half_open_calls.pop(node_id, None)
            else:
                self._states.clear()
                self._failure_counts.clear()
                self._success_counts.clear()
                self._last_failure_time.clear()
                self._half_open_calls.clear()


# =====================================================================
#  会话亲和性管理器
# =====================================================================

@dataclass
class SessionAffinity:
    """
    会话亲和性管理器 — 同一对话/用户的请求路由到相同节点或集群

    设计目标:
        1. 对话上下文一致性 — 同一对话在同一个节点推理，避免切换节点导致
           上下文重建开销
        2. 节点变动时平滑迁移 — 当节点下线时自动迁移到新节点，对用户透明
        3. 避免过度粘滞 — 定期重新评估是否应切换到更优节点

    亲和性策略:
        - STRONG: 强亲和性，仅在节点故障时才切换（适合多轮对话）
        - WEAK:   弱亲和性，每 N 次请求重新评估（适合独立请求）
        - NONE:   无亲和性，每次请求都重新选择节点
    """

    class AffinityStrategy(Enum):
        STRONG = "strong"
        WEAK = "weak"
        NONE = "none"

    strategy: AffinityStrategy = AffinityStrategy.WEAK
    max_session_ttl: float = 600.0        # 会话亲和性 TTL（秒）
    weak_recheck_interval: int = 10       # 弱亲和性每 N 次请求重新评估
    max_sessions: int = 100_000           # 最大会话数

    _sessions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def get_affinity(
        self, session_key: str, model_name: str
    ) -> Optional[str]:
        """
        获取会话绑定的节点 ID

        Args:
            session_key: 会话标识 (如 user_id:conversation_id 或 address)
            model_name: 模型名称

        Returns:
            绑定的节点 ID，无绑定返回 None
        """
        if self.strategy == AffinityStrategy.NONE:
            return None

        with self._lock:
            session = self._sessions.get(session_key)
            if session is None:
                return None

            # 检查 TTL
            if time.time() - session["created_at"] > self.max_session_ttl:
                del self._sessions[session_key]
                return None

            # 检查模型是否匹配
            if session.get("model") != model_name:
                return None

            # 弱亲和性: 检查是否应重新评估
            if self.strategy == AffinityStrategy.WEAK:
                request_count = session.get("request_count", 0)
                if request_count >= self.weak_recheck_interval:
                    # 标记需要重新评估（但不立即解绑）
                    session["needs_recheck"] = True

            return session.get("node_id")

    def bind(
        self, session_key: str, node_id: str, model_name: str,
        cluster_id: Optional[str] = None,
    ) -> None:
        """绑定会话到节点"""
        if self.strategy == AffinityStrategy.NONE:
            return

        with self._lock:
            # 限制会话数量
            if len(self._sessions) >= self.max_sessions:
                # 淘汰最旧的会话
                oldest_key = min(
                    self._sessions.keys(),
                    key=lambda k: self._sessions[k]["created_at"],
                )
                del self._sessions[oldest_key]

            self._sessions[session_key] = {
                "node_id": node_id,
                "model": model_name,
                "cluster_id": cluster_id,
                "created_at": time.time(),
                "request_count": 0,
                "needs_recheck": False,
            }

    def record_request(self, session_key: str) -> None:
        """记录会话请求次数"""
        with self._lock:
            session = self._sessions.get(session_key)
            if session:
                session["request_count"] = session.get("request_count", 0) + 1

    def unbind(self, session_key: str) -> Optional[str]:
        """解绑会话，返回之前的节点 ID"""
        with self._lock:
            session = self._sessions.pop(session_key, None)
            if session:
                logger.info(
                    "会话亲和性: 解绑 %s (节点=%s)",
                    session_key[:16], session["node_id"],
                )
                return session.get("node_id")
            return None

    def unbind_by_node(self, node_id: str) -> List[str]:
        """解绑指定节点的所有会话"""
        with self._lock:
            affected = []
            to_remove = []
            for key, session in self._sessions.items():
                if session.get("node_id") == node_id:
                    affected.append(key)
                    to_remove.append(key)
            for key in to_remove:
                del self._sessions[key]
            if affected:
                logger.info(
                    "会话亲和性: 节点 %s 下线，迁移 %d 个会话",
                    node_id, len(affected),
                )
            return affected

    def get_stats(self) -> Dict[str, Any]:
        """获取亲和性统计"""
        with self._lock:
            return {
                "strategy": self.strategy.value,
                "active_sessions": len(self._sessions),
                "max_sessions": self.max_sessions,
            }

    def clear(self) -> None:
        """清除所有会话亲和性"""
        with self._lock:
            self._sessions.clear()


# =====================================================================
#  节点集群
# =====================================================================

@dataclass
class NodeSlot:
    """集群中节点的槽位信息"""
    node_id: str
    rank: int                    # 在集群中的序号 (0 = leader)
    role: str = "worker"         # "leader" 或 "worker"
    assigned_shards: List[int] = field(default_factory=list)  # 张量并行的分片ID
    assigned_layers: Tuple[int, int] = (0, 0)  # 流水线并行的层范围 (start, end)
    vram_available_gb: float = 0.0


@dataclass
class NodeCluster:
    """
    推理集群 — 多个节点组成一个协作推理单元

    管理内容:
        - 集群 ID 和配置
        - 成员节点列表及角色
        - 推理模式和张量并行度
        - 健康状态和负载均衡
    """
    cluster_id: str
    model_name: str
    model_profile: ModelProfile
    mode: InferenceMode = InferenceMode.STANDALONE
    tensor_parallel_size: int = 1    # 张量并行度
    pipeline_parallel_size: int = 1   # 流水线并行度
    nodes: List[NodeSlot] = field(default_factory=list)
    leader_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    total_requests: int = 0
    total_failures: int = 0
    is_active: bool = True

    # 自适应参数
    _target_load: float = 0.7        # 目标负载率 (0-1)
    _max_idle_seconds: float = 300.0  # 集群最大空闲时间（秒）

    def get_member_ids(self) -> List[str]:
        """获取所有成员节点 ID"""
        return [n.node_id for n in self.nodes]

    def get_leader(self) -> Optional[NodeSlot]:
        """获取 Leader 节点"""
        for n in self.nodes:
            if n.role == "leader":
                return n
        return self.nodes[0] if self.nodes else None

    def is_healthy(self) -> bool:
        """检查集群是否健康（有足够节点在线）"""
        if not self.is_active:
            return False
        required = self.model_profile.min_nodes
        return len(self.nodes) >= required

    def is_idle(self) -> bool:
        """检查集群是否空闲（可用于回收）"""
        if self.total_requests > 0:
            return False
        return time.time() - self.last_active > self._max_idle_seconds

    def record_request(self, success: bool) -> None:
        """记录请求结果"""
        self.total_requests += 1
        if not success:
            self.total_failures += 1
        self.last_active = time.time()

    def calculate_efficiency(self) -> float:
        """
        计算集群效率得分 (0-1)

        综合考虑:
        - 节点利用率 (是否有多余节点)
        - 成功率
        - 负载均衡程度
        """
        if not self.nodes:
            return 0.0

        # 节点利用率: 实际节点数 vs 最小需求
        min_nodes = self.model_profile.min_nodes
        actual_nodes = len(self.nodes)
        utilization = min(1.0, min_nodes / max(1, actual_nodes))

        # 成功率
        success_rate = 1.0
        if self.total_requests > 0:
            success_rate = (self.total_requests - self.total_failures) / self.total_requests

        # 负载均衡: 使用标准差衡量
        avg_vram = sum(n.vram_available_gb for n in self.nodes) / len(self.nodes)
        if avg_vram > 0:
            vram_variance = sum(
                (n.vram_available_gb - avg_vram) ** 2 for n in self.nodes
            ) / len(self.nodes)
            balance = 1.0 - min(1.0, math.sqrt(vram_variance) / avg_vram)
        else:
            balance = 0.5

        return 0.3 * utilization + 0.4 * success_rate + 0.3 * balance

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "cluster_id": self.cluster_id,
            "model_name": self.model_name,
            "mode": self.mode.value,
            "tensor_parallel_size": self.tensor_parallel_size,
            "pipeline_parallel_size": self.pipeline_parallel_size,
            "node_count": len(self.nodes),
            "leader_id": self.leader_id,
            "is_active": self.is_active,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "efficiency": round(self.calculate_efficiency(), 4),
            "created_at": self.created_at,
            "last_active": self.last_active,
            "nodes": [
                {
                    "node_id": n.node_id,
                    "rank": n.rank,
                    "role": n.role,
                    "assigned_shards": n.assigned_shards,
                    "assigned_layers": n.assigned_layers,
                    "vram_available_gb": n.vram_available_gb,
                }
                for n in self.nodes
            ],
        }


# =====================================================================
#  集群管理器
# =====================================================================

class ClusterManager:
    """
    集群管理器 — 自动组建、维护和回收推理集群

    核心职责:
    1. 集群组建: 根据模型大小和节点资源自动组建推理集群
    2. 自适应伸缩: 根据负载动态调整集群大小
    3. 健康监控: 检测节点故障并触发集群重组
    4. 空闲回收: 回收长时间空闲的集群，释放资源
    5. 节点共享: 允许节点同时参与多个小模型集群（时分复用）
    """

    def __init__(self, circuit_breaker: Optional[CircuitBreaker] = None):
        self._clusters: Dict[str, NodeCluster] = {}     # cluster_id -> cluster
        self._model_clusters: Dict[str, List[str]] = {}  # model_name -> [cluster_ids]
        self._node_clusters: Dict[str, Set[str]] = {}   # node_id -> {cluster_ids}
        self._lock = threading.RLock()
        self._circuit_breaker = circuit_breaker or CircuitBreaker()

        # 后台回收线程
        self._running = False
        self._gc_thread: Optional[threading.Thread] = None
        self._gc_interval = 60.0  # 回收检查间隔（秒）
        self.start_gc()

    # ------------------------------------------------------------------
    #  生命周期
    # ------------------------------------------------------------------

    def start_gc(self) -> None:
        """启动后台回收线程"""
        if self._running:
            return
        self._running = True
        self._gc_thread = threading.Thread(
            target=self._gc_loop, daemon=True, name="cluster-gc"
        )
        self._gc_thread.start()
        logger.info("集群管理器回收线程已启动")

    def stop_gc(self) -> None:
        """停止后台回收线程"""
        self._running = False
        if self._gc_thread and self._gc_thread.is_alive():
            self._gc_thread.join(timeout=5.0)

    def _gc_loop(self) -> None:
        """后台回收循环"""
        while self._running:
            try:
                time.sleep(self._gc_interval)
                self._reclaim_idle_clusters()
            except Exception:
                logger.exception("集群回收异常")

    def _reclaim_idle_clusters(self) -> None:
        """回收空闲集群"""
        with self._lock:
            to_reclaim = []
            for cid, cluster in self._clusters.items():
                if not cluster.is_active:
                    continue
                if cluster.is_idle():
                    to_reclaim.append(cid)

            for cid in to_reclaim:
                self._disband_cluster_internal(cid)
                logger.info("集群 %s 因长时间空闲被回收", cid)

    # ------------------------------------------------------------------
    #  集群组建
    # ------------------------------------------------------------------

    def find_or_create_cluster(
        self,
        model_name: str,
        available_nodes: List[Dict[str, Any]],
        preferred_node_id: Optional[str] = None,
    ) -> Optional[NodeCluster]:
        """
        查找或创建适合指定模型的推理集群

        策略:
        1. 如果有活跃集群且容量充足，复用已有集群
        2. 如果首选节点可用且适合，以该节点为核心组建
        3. 否则从可用节点中自动选择最优组合组建新集群

        Args:
            model_name: 模型名称
            available_nodes: 可用节点列表 [{"id", "vram_gb", ...}, ...]
            preferred_node_id: 首选节点 ID（来自会话亲和性）

        Returns:
            推理集群，无法组建返回 None
        """
        profile = ModelProfile.from_catalog(model_name)
        min_nodes = profile.min_nodes
        min_vram = profile.min_vram_gb

        with self._lock:
            # 过滤掉被熔断的节点
            usable_nodes = [
                n for n in available_nodes
                if self._circuit_breaker.is_available(n["id"])
                and n.get("vram_gb", 0) >= (min_vram / min_nodes)
            ]

            if len(usable_nodes) < min_nodes:
                logger.warning(
                    "可用节点不足: 模型=%s 需要 %d 个节点 (最小VRAM %.1fGB/节点)，"
                    "当前可用 %d 个",
                    model_name, min_nodes, min_vram / min_nodes,
                    len(usable_nodes),
                )
                # 降级: 尝试使用已有集群
                return self._find_existing_cluster(model_name)

            # 尝试复用已有集群
            existing = self._find_usable_cluster(model_name)
            if existing and existing.is_healthy():
                return existing

            # 组建新集群
            return self._form_cluster(
                model_name, profile, usable_nodes, preferred_node_id
            )

    def _find_existing_cluster(self, model_name: str) -> Optional[NodeCluster]:
        """查找该模型已有的活跃集群"""
        cids = self._model_clusters.get(model_name, [])
        for cid in cids:
            cluster = self._clusters.get(cid)
            if cluster and cluster.is_active and cluster.is_healthy():
                return cluster
        return None

    def _find_usable_cluster(self, model_name: str) -> Optional[NodeCluster]:
        """查找该模型可复用的集群（考虑负载）"""
        cids = self._model_clusters.get(model_name, [])
        for cid in cids:
            cluster = self._clusters.get(cid)
            if cluster and cluster.is_active and cluster.is_healthy():
                # 检查集群效率，效率过低说明过度配置
                if cluster.calculate_efficiency() >= 0.3:
                    return cluster
        return None

    def _form_cluster(
        self,
        model_name: str,
        profile: ModelProfile,
        usable_nodes: List[Dict[str, Any]],
        preferred_node_id: Optional[str] = None,
    ) -> NodeCluster:
        """
        组建新的推理集群

        逻辑:
        1. 如果是 STANDALONE 模式，选择最优单节点
        2. 如果是 TENSOR_PARALLEL 模式，选择延迟最低的 N 个节点组成
        3. 如果是 PIPELINE_PARALLEL，选择算力最强的 N 个节点
        """
        cluster_id = self._generate_cluster_id(model_name)
        mode = profile.preferred_mode
        min_nodes = profile.min_nodes

        # 排序节点: 综合评分 (VRAM大 + 负载低 + 延迟低 优先)
        sorted_nodes = sorted(
            usable_nodes,
            key=lambda n: (
                n.get("vram_gb", 0) * 0.4
                + (1 - n.get("load", 0)) * 30
                - n.get("latency_ms", 50) * 0.01
            ),
            reverse=True,
        )

        # 如果有首选节点，将其排到最前
        if preferred_node_id:
            preferred = [
                n for n in sorted_nodes if n["id"] == preferred_node_id
            ]
            others = [
                n for n in sorted_nodes if n["id"] != preferred_node_id
            ]
            sorted_nodes = preferred + others

        # 按模型需求选择节点数量
        # 关键: STANDALONE 只选 1 个，避免冗余
        if mode == InferenceMode.STANDALONE:
            selected = sorted_nodes[:1]
        elif mode == InferenceMode.TENSOR_PARALLEL:
            # 张量并行: 选择延迟最低的节点对 (通信敏感)
            sorted_by_latency = sorted(
                sorted_nodes, key=lambda n: n.get("latency_ms", 50)
            )
            tp_size = max(min_nodes, 2)  # 至少 2 路并行
            selected = sorted_by_latency[:tp_size]
        elif mode == InferenceMode.PIPELINE_PARALLEL:
            # 流水线: 选择算力最强的节点 (计算密集)
            selected = sorted_nodes[:min_nodes]
        else:  # HYBRID
            hybrid_size = max(min_nodes, 4)
            selected = sorted_nodes[:hybrid_size]

        if not selected:
            logger.error("组建集群失败: 无可用节点")
            raise RuntimeError(f"无法为模型 {model_name} 组建推理集群")

        # 构建节点槽位
        nodes = []
        total_shards = len(selected)
        total_weight = profile.weight_gb

        for rank, node_info in enumerate(selected):
            # 张量并行: 均匀分配权重分片
            shard_per_node = math.ceil(total_weight / len(selected))
            shard_start = rank * shard_per_node
            shard_ids = list(range(shard_start, min(shard_start + shard_per_node, int(total_weight))))

            # 流水线并行: 按层分配
            layer_total = int(profile.params_b * 10)  # 粗略估计层数
            layers_per_node = math.ceil(layer_total / len(selected))
            layer_start = rank * layers_per_node
            layer_end = min(layer_start + layers_per_node, layer_total)

            slot = NodeSlot(
                node_id=node_info["id"],
                rank=rank,
                role="leader" if rank == 0 else "worker",
                assigned_shards=shard_ids if mode == InferenceMode.TENSOR_PARALLEL else [],
                assigned_layers=(layer_start, layer_end) if mode == InferenceMode.PIPELINE_PARALLEL else (0, 0),
                vram_available_gb=node_info.get("vram_gb", 0),
            )
            nodes.append(slot)

        # 创建集群
        tp_size = len(selected) if mode in (
            InferenceMode.TENSOR_PARALLEL, InferenceMode.HYBRID_PARALLEL
        ) else 1
        pp_size = len(selected) if mode == InferenceMode.PIPELINE_PARALLEL else 1

        cluster = NodeCluster(
            cluster_id=cluster_id,
            model_name=model_name,
            model_profile=profile,
            mode=mode,
            tensor_parallel_size=tp_size,
            pipeline_parallel_size=pp_size,
            nodes=nodes,
            leader_id=nodes[0].node_id if nodes else "",
        )

        # 注册集群
        self._clusters[cluster_id] = cluster
        self._model_clusters.setdefault(model_name, []).append(cluster_id)
        for n in nodes:
            self._node_clusters.setdefault(n.node_id, set()).add(cluster_id)

        logger.info(
            "集群组建成功: id=%s, 模型=%s, 模式=%s, 节点数=%d, 节点=%s",
            cluster_id, model_name, mode.value, len(nodes),
            [n.node_id for n in nodes],
        )
        return cluster

    # ------------------------------------------------------------------
    #  节点变动处理
    # ------------------------------------------------------------------

    def handle_node_leave(self, node_id: str) -> None:
        """
        处理节点离开 — 自动重建受影响的集群

        策略:
        1. 标记受影响集群为 inactive
        2. 将该节点的槽位标记为空
        3. 对于 TINY/SMALL 模型: 直接迁移到其他节点
        4. 对于 MEDIUM+ 模型: 需要重新组建集群（并行推理不能缺节点）
        """
        with self._lock:
            affected_cluster_ids = self._node_clusters.pop(node_id, set())

            for cid in list(affected_cluster_ids):
                cluster = self._clusters.get(cid)
                if not cluster or not cluster.is_active:
                    continue

                logger.warning(
                    "节点 %s 离开，集群 %s 受影响 (模型=%s, 节点数=%d→%d)",
                    node_id, cid, cluster.model_name,
                    len(cluster.nodes), len(cluster.nodes) - 1,
                )

                # 从集群移除节点
                cluster.nodes = [n for n in cluster.nodes if n.node_id != node_id]

                # 检查是否仍满足最小节点需求
                if cluster.is_healthy():
                    # 如果 leader 离开，选举新 leader
                    if cluster.leader_id == node_id:
                        leader = cluster.get_leader()
                        cluster.leader_id = leader.node_id if leader else ""
                    logger.info(
                        "集群 %s 节点减少但仍可用 (剩余 %d 节点)",
                        cid, len(cluster.nodes),
                    )
                else:
                    # 不满足需求，标记为不活跃
                    cluster.is_active = False
                    cluster.leader_id = ""
                    logger.warning(
                        "集群 %s 不再满足最小节点需求 (%d < %d)，标记为不活跃",
                        cid, len(cluster.nodes), cluster.model_profile.min_nodes,
                    )

    def handle_node_join(self, node_id: str, node_info: Dict[str, Any]) -> None:
        """
        处理新节点加入 — 可能触发集群优化

        策略:
        1. 检查是否有不活跃集群可以重建
        2. 检查是否有集群效率过低可以优化
        3. 记录新节点可用性
        """
        with self._lock:
            # 检查是否有不活跃的同模型集群
            for cid, cluster in self._clusters.items():
                if cluster.is_active:
                    continue
                if cluster.model_name in node_info.get("models", []):
                    # 尝试用新节点重建
                    logger.info(
                        "节点 %s 加入，可能可用于重建集群 %s (模型=%s)",
                        node_id, cid, cluster.model_name,
                    )
                    break  # 先不急，等下一次请求时自动组建

    # ------------------------------------------------------------------
    #  集群查询
    # ------------------------------------------------------------------

    def get_cluster(self, cluster_id: str) -> Optional[NodeCluster]:
        """获取集群"""
        with self._lock:
            return self._clusters.get(cluster_id)

    def get_node_clusters(self, node_id: str) -> List[NodeCluster]:
        """获取节点参与的所有集群"""
        with self._lock:
            cids = self._node_clusters.get(node_id, set())
            return [self._clusters[cid] for cid in cids if cid in self._clusters]

    def get_all_clusters(self) -> Dict[str, NodeCluster]:
        """获取所有集群"""
        with self._lock:
            return dict(self._clusters)

    def get_stats(self) -> Dict[str, Any]:
        """获取集群统计"""
        with self._lock:
            active = sum(1 for c in self._clusters.values() if c.is_active)
            by_mode = {}
            for c in self._clusters.values():
                if c.is_active:
                    mode = c.mode.value
                    by_mode[mode] = by_mode.get(mode, 0) + 1
            return {
                "total_clusters": len(self._clusters),
                "active_clusters": active,
                "by_mode": by_mode,
                "circuit_breaker": self._circuit_breaker.get_stats(),
            }

    # ------------------------------------------------------------------
    #  内部工具
    # ------------------------------------------------------------------

    def _disband_cluster_internal(self, cluster_id: str) -> None:
        """内部方法: 解散集群"""
        cluster = self._clusters.pop(cluster_id, None)
        if not cluster:
            return

        # 清理引用
        model_clusters = self._model_clusters.get(cluster.model_name, [])
        if cluster_id in model_clusters:
            model_clusters.remove(cluster_id)

        for node in cluster.nodes:
            node_clusters = self._node_clusters.get(node.node_id, set())
            node_clusters.discard(cluster_id)

        cluster.is_active = False

    def _generate_cluster_id(self, model_name: str) -> str:
        """生成唯一集群 ID"""
        raw = f"{model_name}:{time.time()}:{id(self)}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """获取熔断器实例"""
        return self._circuit_breaker

    def stop(self) -> None:
        """停止集群管理器 — 停止回收线程并清理所有集群"""
        self._running = False
        self.stop_gc()
        with self._lock:
            active_ids = [
                cid for cid, c in self._clusters.items() if c.is_active
            ]
            for cid in active_ids:
                self._disband_cluster_internal(cid)
        logger.info(
            "集群管理器已停止，共清理 %d 个集群", len(active_ids)
        )


# =====================================================================
#  全局混合配置 — 分级冗余策略
# =====================================================================

@dataclass
class TierRedundancyPolicy:
    """
    单个 Tier 的冗余策略

    设计原则: 适度冗余保障稳定性，不搞到冗余又浪费

    - active_nodes:   正常推理所需的最小节点数 (参与张量/流水线并行)
    - standby_count:  热备节点数 (预加载权重但不主动推理，主节点故障时秒级接管)
    - total_required: active + standby (集群组建时的总节点需求)
    """
    tier: ModelTier
    active_nodes: int           # 正常推理所需节点
    standby_count: int          # 热备节点数
    degrade_to_active: int      # 优雅降级: 资源不足时可接受的最小活跃节点

    @property
    def total_required(self) -> int:
        """集群组建时的总节点需求"""
        return self.active_nodes + self.standby_count

    def graceful_degrade(self, available: int) -> Tuple[int, int]:
        """
        优雅降级计算 — 资源不足时优先砍冗余再砍并行

        降级优先级:
        1. 先减少热备 (standby → 0)
        2. 再减少活跃节点 (active → degrade_to_active)
        3. 低于 degrade_to_active 则返回 (0, 0) 表示无法运行

        Returns:
            (actual_active, actual_standby)
        """
        # 足够 → 全量
        if available >= self.total_required:
            return self.active_nodes, self.standby_count

        # 优先保留活跃节点，砍掉热备
        if available >= self.active_nodes:
            return self.active_nodes, 0

        # 进一步降级: 减少活跃节点
        if available >= self.degrade_to_active:
            return available, 0

        # 无法运行
        return 0, 0


@dataclass
class HYBRID_CONFIG:
    """
    全局混合推理配置 — 所有可调参数的单一入口

    核心设计: 分级冗余策略
    ┌──────────┬──────────┬──────────┬────────────────┬───────────────────────────┐
    │ Tier     │ 活跃节点 │ 热备节点 │ 总需求         │ 说明                      │
    ├──────────┼──────────┼──────────┼────────────────┼───────────────────────────┤
    │ TINY     │ 1        │ 1        │ 2              │ 1推理 + 1热备              │
    │ SMALL    │ 1        │ 1        │ 2              │ 1推理 + 1热备              │
    │ MEDIUM   │ 2        │ 1        │ 3              │ 2张量并行 + 1热备           │
    │ LARGE    │ 4        │ 1        │ 5              │ 4张量并行 + 1热备           │
    │ XLARGE   │ 4        │ 1        │ 5              │ 4流水线并行 + 1热备         │
    │ MASSIVE  │ 8        │ 2        │ 10             │ 8混合并行 + 2热备           │
    └──────────┴──────────┴──────────┴────────────────┴───────────────────────────┘

    热备节点:
        - 预加载模型权重，处于待命状态
        - 不主动参与推理（不浪费算力）
        - 主节点故障时秒级接管，无权重加载延迟
        - MASSIVE 级别给 2 个热备，因为集群大、故障概率更高

    优雅降级:
        - 资源不足时先砍热备，再砍并行度
        - 不会因为凑不齐热备节点就不启动推理
    """
    # 分级冗余策略
    tier_policies: Dict[ModelTier, TierRedundancyPolicy] = field(default_factory=dict)

    # 热备预热超时 (秒) — 热备节点必须在此时限内完成权重加载
    standby_warmup_timeout: float = 60.0

    # 故障转移超时 (秒) — 主节点失败后等待热备接管的最大时间
    failover_timeout: float = 5.0

    # 集群回收
    cluster_idle_timeout: float = 300.0     # 集群空闲多久后被回收
    gc_check_interval: float = 60.0         # 回收线程检查间隔

    # 熔断器默认参数
    cb_failure_threshold: int = 5
    cb_recovery_timeout: float = 30.0
    cb_half_open_max_calls: int = 3
    cb_success_threshold: int = 2

    # 会话亲和性
    affinity_strategy: str = "weak"         # "strong" | "weak" | "none"
    affinity_ttl: float = 600.0

    def __post_init__(self):
        """初始化分级冗余策略"""
        if not self.tier_policies:
            self.tier_policies = {
                ModelTier.TINY: TierRedundancyPolicy(
                    tier=ModelTier.TINY,
                    active_nodes=1, standby_count=1, degrade_to_active=1,
                ),
                ModelTier.SMALL: TierRedundancyPolicy(
                    tier=ModelTier.SMALL,
                    active_nodes=1, standby_count=1, degrade_to_active=1,
                ),
                ModelTier.MEDIUM: TierRedundancyPolicy(
                    tier=ModelTier.MEDIUM,
                    active_nodes=2, standby_count=1, degrade_to_active=2,
                ),
                ModelTier.LARGE: TierRedundancyPolicy(
                    tier=ModelTier.LARGE,
                    active_nodes=4, standby_count=1, degrade_to_active=3,
                ),
                ModelTier.XLARGE: TierRedundancyPolicy(
                    tier=ModelTier.XLARGE,
                    active_nodes=4, standby_count=1, degrade_to_active=3,
                ),
                ModelTier.MASSIVE: TierRedundancyPolicy(
                    tier=ModelTier.MASSIVE,
                    active_nodes=8, standby_count=2, degrade_to_active=6,
                ),
            }

    def get_policy(self, tier: ModelTier) -> TierRedundancyPolicy:
        """获取指定 Tier 的冗余策略，未知 Tier 返回 MEDIUM 策略"""
        return self.tier_policies.get(tier, self.tier_policies[ModelTier.MEDIUM])

    def to_dict(self) -> Dict[str, Any]:
        """序列化配置"""
        return {
            "tier_policies": {
                tier.value: {
                    "active_nodes": p.active_nodes,
                    "standby_count": p.standby_count,
                    "total_required": p.total_required,
                    "degrade_to_active": p.degrade_to_active,
                }
                for tier, p in self.tier_policies.items()
            },
            "standby_warmup_timeout": self.standby_warmup_timeout,
            "failover_timeout": self.failover_timeout,
            "cluster_idle_timeout": self.cluster_idle_timeout,
            "cb_failure_threshold": self.cb_failure_threshold,
            "cb_recovery_timeout": self.cb_recovery_timeout,
            "affinity_strategy": self.affinity_strategy,
            "affinity_ttl": self.affinity_ttl,
        }


# 全局默认实例
_DEFAULT_HYBRID_CONFIG = HYBRID_CONFIG()


# =====================================================================
#  混合调度器 — 核心调度入口
# =====================================================================

class HybridScheduler:
    """
    混合调度器 — AICoin 分布式推理的统一调度入口

    解决的核心问题:
    ┌─────────────────────────────────────────────────────────────┐
    │ 问题1: 节点动态波动 (Churn)                                  │
    │   → CircuitBreaker: 连续失败自动熔断                        │
    │   → SessionAffinity: 节点下线自动迁移                       │
    │   → ClusterManager: 集群自动重组                            │
    │                                                             │
    │ 问题2: 算力极度冗余 (Over-provisioning)                     │
    │   → STANDALONE 模式: 小模型只用 1 个节点                    │
    │   → 集群效率监控: 自动回收低效集群                         │
    │   → 自适应伸缩: 负载低时减少节点                            │
    │                                                             │
    │ 问题3: 大模型单节点装不下 (Model Parallelism)                │
    │   → TENSOR_PARALLEL: 同层权重分片到多节点                    │
    │   → PIPELINE_PARALLEL: 按层分段到多节点                     │
    │   → HYBRID: 两者结合                                       │
    └─────────────────────────────────────────────────────────────┘

    使用示例:
        scheduler = HybridScheduler()
        result = scheduler.schedule(
            model_name="aicoin-llama-70b",
            available_nodes=[...],
            session_key="user123:conv456",
            execute_callback=my_inference_fn,
        )
    """

    def __init__(
        self,
        config: Optional[HYBRID_CONFIG] = None,
        legacy_config: Optional[Dict[str, Any]] = None,
    ):
        # 支持两种初始化方式:
        #   - config: HYBRID_CONFIG dataclass 实例
        #   - legacy_config: 旧的 dict 配置 (向后兼容)
        if config is not None:
            self._config = config
        elif legacy_config is not None:
            self._config = HYBRID_CONFIG(
                cb_failure_threshold=legacy_config.get("cb_failure_threshold", 5),
                cb_recovery_timeout=legacy_config.get("cb_recovery_timeout", 30.0),
                cb_half_open_max_calls=legacy_config.get("cb_half_open_max_calls", 3),
                cb_success_threshold=legacy_config.get("cb_success_threshold", 2),
                affinity_strategy=legacy_config.get("affinity_strategy", "weak"),
                affinity_ttl=legacy_config.get("affinity_ttl", 600.0),
            )
        else:
            self._config = HYBRID_CONFIG()

        cfg = self._config

        # 核心组件
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=cfg.cb_failure_threshold,
            recovery_timeout=cfg.cb_recovery_timeout,
            half_open_max_calls=cfg.cb_half_open_max_calls,
            success_threshold=cfg.cb_success_threshold,
        )
        self._session_affinity = SessionAffinity(
            strategy=SessionAffinity.AffinityStrategy(cfg.affinity_strategy),
            max_session_ttl=cfg.affinity_ttl,
        )
        self._cluster_manager = ClusterManager(self._circuit_breaker)

        # 统计
        self._lock = threading.RLock()
        self._total_scheduled = 0
        self._total_success = 0
        self._total_fallbacks = 0
        self._total_standby_activations = 0

        # 模型画像缓存
        self._profile_cache: Dict[str, ModelProfile] = {}

        logger.info(
            "混合调度器初始化完成 — 冗余策略已加载: %s",
            {tier.value: f"{p.active_nodes}+{p.standby_count}"
             for tier, p in cfg.tier_policies.items()},
        )

    # ------------------------------------------------------------------
    #  核心调度
    # ------------------------------------------------------------------

    def schedule(
        self,
        model_name: str,
        available_nodes: List[Dict[str, Any]],
        session_key: Optional[str] = None,
        execute_callback: Optional[Callable] = None,
        request_data: Optional[Dict[str, Any]] = None,
        priority: str = "basic",
    ) -> Dict[str, Any]:
        """
        核心调度入口 — 为推理请求选择最优执行方式

        流程:
        1. 获取模型画像 (参数量、分级、推荐模式)
        2. 检查会话亲和性 → 已有绑定则复用
        3. 查找或创建推理集群
        4. 执行推理 (通过回调)
        5. 记录结果到熔断器和集群
        6. 失败时自动故障转移

        Args:
            model_name: 模型名称
            available_nodes: 可用节点列表
            session_key: 会话标识 (用于亲和性)
            execute_callback: 推理执行回调
                def callback(node_id: str, cluster: NodeCluster, data: dict) -> dict
                返回 {"success": bool, "response": Any, "error": str|None}
            request_data: 推理请求数据
            priority: 优先级

        Returns:
            {
                "success": bool,
                "cluster_id": str,
                "mode": str,
                "node_id": str,
                "response": Any,
                "tried_nodes": [...],
                "total_latency_ms": float,
            }
        """
        start_time = time.monotonic()
        self._total_scheduled += 1

        profile = self._get_profile(model_name)

        # Step 1: 检查会话亲和性
        preferred_node = None
        if session_key:
            preferred_node = self._session_affinity.get_affinity(
                session_key, model_name
            )

        # Step 2: 查找或创建集群
        cluster = self._cluster_manager.find_or_create_cluster(
            model_name, available_nodes, preferred_node
        )

        if cluster is None:
            return self._make_error_result(
                model_name, "无法组建推理集群 (节点不足或全部被熔断)",
                start_time,
            )

        # Step 3: 执行推理
        tried_nodes: List[Dict[str, Any]] = []
        result = self._execute_with_fallback(
            cluster, execute_callback, request_data, tried_nodes
        )

        # Step 4: 记录结果
        self._record_result(cluster, result, tried_nodes)

        # Step 5: 更新会话亲和性
        if session_key and result.get("success"):
            leader = cluster.get_leader()
            if leader:
                self._session_affinity.bind(
                    session_key, leader.node_id, model_name,
                    cluster.cluster_id,
                )
                self._session_affinity.record_request(session_key)

        # Step 6: 更新统计
        if result.get("success"):
            self._total_success += 1
        elif result.get("fallback"):
            self._total_fallbacks += 1

        total_latency = (time.monotonic() - start_time) * 1000.0

        return {
            "success": result.get("success", False),
            "cluster_id": cluster.cluster_id,
            "mode": cluster.mode.value,
            "tensor_parallel_size": cluster.tensor_parallel_size,
            "pipeline_parallel_size": cluster.pipeline_parallel_size,
            "node_id": result.get("node_id"),
            "response": result.get("response"),
            "tried_nodes": tried_nodes,
            "total_latency_ms": round(total_latency, 2),
            "error": result.get("error"),
        }

    def _execute_with_fallback(
        self,
        cluster: NodeCluster,
        callback: Optional[Callable],
        request_data: Optional[Dict],
        tried_nodes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """在集群内执行推理，支持故障转移"""
        if callback is None:
            return {
                "success": True,
                "response": {"simulated": True, "mode": cluster.mode.value},
                "node_id": cluster.leader_id,
            }

        # STANDALONE 模式: 直接在 leader 执行
        if cluster.mode == InferenceMode.STANDALONE:
            return self._try_node(
                cluster.leader_id, callback, cluster, request_data, tried_nodes
            )

        # 并行模式: 先尝试 leader，失败时尝试其他节点
        nodes_sorted = sorted(cluster.nodes, key=lambda n: n.rank)
        for node_slot in nodes_sorted:
            result = self._try_node(
                node_slot.node_id, callback, cluster, request_data, tried_nodes
            )
            if result.get("success"):
                return result

        # 所有节点都失败
        return {
            "success": False,
            "error": f"集群 {cluster.cluster_id} 所有节点均失败",
            "node_id": None,
        }

    def _try_node(
        self,
        node_id: str,
        callback: Callable,
        cluster: NodeCluster,
        request_data: Optional[Dict],
        tried_nodes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """尝试在单个节点上执行"""
        # 检查熔断
        if not self._circuit_breaker.is_available(node_id):
            tried_nodes.append({
                "node_id": node_id,
                "success": False,
                "error": "节点被熔断",
                "circuit_state": self._circuit_breaker.get_state(node_id).value,
            })
            return {"success": False, "error": "节点被熔断", "node_id": node_id}

        node_start = time.monotonic()
        try:
            result = callback(node_id, cluster, request_data or {})
            node_latency = (time.monotonic() - node_start) * 1000.0

            success = result.get("success", False)
            tried_nodes.append({
                "node_id": node_id,
                "success": success,
                "error": result.get("error"),
                "latency_ms": round(node_latency, 2),
            })

            if success:
                self._circuit_breaker.record_success(node_id)
                cluster.record_request(True)
                return {"success": True, "response": result.get("response"), "node_id": node_id}
            else:
                self._circuit_breaker.record_failure(node_id)
                cluster.record_request(False)
                return {"success": False, "error": result.get("error"), "node_id": node_id}

        except Exception as e:
            node_latency = (time.monotonic() - node_start) * 1000.0
            self._circuit_breaker.record_failure(node_id)
            cluster.record_request(False)
            tried_nodes.append({
                "node_id": node_id,
                "success": False,
                "error": str(e),
                "latency_ms": round(node_latency, 2),
            })
            logger.exception("节点 %s 执行推理失败: %s", node_id, e)
            return {"success": False, "error": str(e), "node_id": node_id}

    def _record_result(
        self, cluster: NodeCluster, result: Dict, tried_nodes: List[Dict]
    ) -> None:
        """记录调度结果"""
        if not result.get("success"):
            # 检查是否所有节点都失败 → 可能需要重建集群
            all_failed = all(not t.get("success", True) for t in tried_nodes)
            if all_failed and len(tried_nodes) >= cluster.model_profile.min_nodes:
                logger.warning(
                    "集群 %s 全部节点失败，标记为不活跃并等待重建",
                    cluster.cluster_id,
                )
                cluster.is_active = False

    def _get_profile(self, model_name: str) -> ModelProfile:
        """获取或缓存模型画像"""
        if model_name not in self._profile_cache:
            self._profile_cache[model_name] = ModelProfile.from_catalog(model_name)
        return self._profile_cache[model_name]

    def _make_error_result(
        self, model_name: str, error: str, start_time: float
    ) -> Dict[str, Any]:
        """构建错误结果"""
        latency = (time.monotonic() - start_time) * 1000.0
        return {
            "success": False,
            "cluster_id": "",
            "mode": "none",
            "tensor_parallel_size": 0,
            "pipeline_parallel_size": 0,
            "node_id": None,
            "response": None,
            "tried_nodes": [],
            "total_latency_ms": round(latency, 2),
            "error": error,
        }

    # ------------------------------------------------------------------
    #  热备路由 — 带冗余的推理执行
    # ------------------------------------------------------------------

    def route_hybrid(
        self,
        model_name: str,
        request_data: Optional[Dict[str, Any]] = None,
        session_key: Optional[str] = None,
        priority: str = "normal",
        available_nodes: Optional[List[Dict[str, Any]]] = None,
        execute_callback: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """
        混合路由入口 — 带热备故障转移的完整推理流程

        与 schedule() 的区别:
            schedule() 侧重于集群组建和基本调度
            route_hybrid() 增加了分级冗余策略和热备切换

        调度流程:
        1. 解析模型 → ModelProfile (确定 tier, mode, min_nodes)
        2. 查询冗余策略 → TierRedundancyPolicy (active + standby)
        3. 检查会话亲和性 → 已绑定集群直接复用
        4. 组建/复用推理集群 → ClusterManager
        5. 构建节点列表 (含热备标记) → _prepare_node_list
        6. 执行推理(含热备故障转移) → _execute_on_cluster
        7. 记录结果 → 更新熔断器+亲和性+奖励

        Args:
            model_name: 模型名称
            request_data: 推理请求数据
            session_key: 会话标识
            priority: 优先级 (\"normal\" | \"high\" | \"low\")
            available_nodes: 可用节点列表
            execute_callback: 推理执行回调

        Returns:
            路由决策字典:
            {
                \"success\": bool,
                \"cluster_id\": str,
                \"mode\": str,
                \"tier\": str,
                \"node_id\": str,
                \"standby_activated\": bool,
                \"response\": Any,
                \"tried_nodes\": [...],
                \"redundancy_policy\": {...},
                \"total_latency_ms\": float,
            }
        """
        start_time = time.monotonic()
        self._total_scheduled += 1

        # Step 1: 解析模型画像
        profile = self._get_profile(model_name)

        # Step 2: 查询该 Tier 的冗余策略
        policy = self._config.get_policy(profile.tier)

        # Step 3: 检查会话亲和性
        preferred_node = None
        if session_key:
            preferred_node = self._session_affinity.get_affinity(
                session_key, model_name
            )

        # Step 4: 组建/复用推理集群
        nodes_to_use = available_nodes or []
        cluster = self._cluster_manager.find_or_create_cluster(
            model_name, nodes_to_use, preferred_node
        )

        if cluster is None:
            return {
                **self._make_error_result(
                    model_name,
                    f"无法组建推理集群 (需要 {policy.active_nodes}+"
                    f"{policy.standby_count} 个节点)",
                    start_time,
                ),
                "tier": profile.tier.value,
                "redundancy_policy": {
                    "active": policy.active_nodes,
                    "standby": policy.standby_count,
                },
            }

        # Step 5: 构建带热备标记的节点列表
        node_list = self._prepare_node_list(cluster, profile)

        # Step 6: 执行推理 (含热备故障转移)
        tried_nodes: List[Dict[str, Any]] = []
        result = self._execute_on_cluster(
            cluster, request_data, node_list, execute_callback, tried_nodes
        )

        # Step 7: 记录结果
        standby_activated = result.get("standby_activated", False)
        if standby_activated:
            self._total_standby_activations += 1

        self._record_result(cluster, result, tried_nodes)

        # 更新会话亲和性
        if session_key and result.get("success"):
            actual_node_id = result.get("node_id")
            if actual_node_id:
                self._session_affinity.bind(
                    session_key, actual_node_id, model_name,
                    cluster.cluster_id,
                )
                self._session_affinity.record_request(session_key)

        # 更新统计
        if result.get("success"):
            self._total_success += 1
        elif result.get("fallback"):
            self._total_fallbacks += 1

        total_latency = (time.monotonic() - start_time) * 1000.0

        return {
            "success": result.get("success", False),
            "cluster_id": cluster.cluster_id,
            "mode": cluster.mode.value,
            "tier": profile.tier.value,
            "node_id": result.get("node_id"),
            "standby_activated": standby_activated,
            "response": result.get("response"),
            "tried_nodes": tried_nodes,
            "redundancy_policy": {
                "active": policy.active_nodes,
                "standby": policy.standby_count,
                "total_required": policy.total_required,
                "degrade_to_active": policy.degrade_to_active,
            },
            "total_latency_ms": round(total_latency, 2),
            "error": result.get("error"),
        }

    def _prepare_node_list(
        self, cluster: NodeCluster, profile: ModelProfile
    ) -> List[Dict[str, Any]]:
        """
        构建有序节点列表，标记活跃节点和热备节点

        根据冗余策略:
        - 前 N 个节点 (rank < active_nodes) 为活跃推理节点
        - 后 M 个节点 (rank >= active_nodes) 为热备节点

        热备节点特点:
        - 预加载了模型权重，处于待命状态
        - 不主动参与推理（不浪费算力）
        - 主节点故障时可秒级接管

        Args:
            cluster: 推理集群
            profile: 模型画像

        Returns:
            有序节点列表，每项包含:
            {
                \"node_id\": str,
                \"role\": \"active\" | \"standby\",
                \"rank\": int,
                \"node_slot\": NodeSlot,
            }
        """
        policy = self._config.get_policy(profile.tier)
        active_count = min(policy.active_nodes, len(cluster.nodes))

        node_list = []
        for node_slot in cluster.nodes:
            role = "active" if node_slot.rank < active_count else "standby"
            node_list.append({
                "node_id": node_slot.node_id,
                "role": role,
                "rank": node_slot.rank,
                "node_slot": node_slot,
            })

        logger.debug(
            "节点列表准备完成: 集群=%s, 活跃=%d, 热备=%d",
            cluster.cluster_id,
            active_count,
            len(cluster.nodes) - active_count,
        )
        return node_list

    def _execute_on_cluster(
        self,
        cluster: NodeCluster,
        request_data: Optional[Dict[str, Any]],
        node_list: List[Dict[str, Any]],
        callback: Optional[Callable],
        tried_nodes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        在集群上执行推理，支持热备故障转移

        执行策略:
        1. 先在活跃节点 (role=active) 上尝试执行
        2. 如果所有活跃节点失败，尝试热备节点 (role=standby)
        3. 热备节点接管时记录 standby_activated=True

        对于并行模式 (TENSOR_PARALLEL, PIPELINE_PARALLEL, HYBRID):
        - 活跃节点组成并行推理组，任一节点失败则整组失败
        - 然后尝试用热备节点替换失败节点重试

        对于 STANDALONE 模式:
        - 先在活跃节点上执行
        - 失败则直接切换到热备节点

        Args:
            cluster: 推理集群
            request_data: 请求数据
            node_list: _prepare_node_list 返回的节点列表
            callback: 推理执行回调
            tried_nodes: 已尝试节点记录

        Returns:
            执行结果字典
        """
        if callback is None:
            # 无回调: 模拟成功 (用于测试)
            active_nodes = [n for n in node_list if n["role"] == "active"]
            return {
                "success": True,
                "response": {"simulated": True, "mode": cluster.mode.value},
                "node_id": active_nodes[0]["node_id"] if active_nodes else cluster.leader_id,
                "standby_activated": False,
            }

        # 分离活跃节点和热备节点
        active_nodes = [n for n in node_list if n["role"] == "active"]
        standby_nodes = [n for n in node_list if n["role"] == "standby"]

        # === 并行模式: 所有活跃节点组成推理组 ===
        if cluster.mode != InferenceMode.STANDALONE:
            # 在并行模式下，需要所有活跃节点同时工作
            # 尝试在活跃节点组上执行
            group_success = True
            group_result = None
            failed_active_ids: List[str] = []

            for node_info in active_nodes:
                result = self._try_node(
                    node_info["node_id"], callback, cluster,
                    request_data, tried_nodes,
                )
                if not result.get("success"):
                    group_success = False
                    failed_active_ids.append(node_info["node_id"])
                group_result = result

            if group_success and group_result:
                return {
                    "success": True,
                    "response": group_result.get("response"),
                    "node_id": active_nodes[0]["node_id"],
                    "standby_activated": False,
                }

            # 活跃节点组失败 → 尝试用热备节点替换
            if standby_nodes and len(standby_nodes) >= len(failed_active_ids):
                logger.info(
                    "集群 %s 活跃节点失败 (%s)，启动热备接管",
                    cluster.cluster_id, failed_active_ids,
                )
                return self._failover_to_standby(
                    cluster, request_data, callback,
                    standby_nodes, failed_active_ids, tried_nodes,
                )

            # 热备也不够
            return {
                "success": False,
                "error": (
                    f"集群 {cluster.cluster_id} 所有节点均失败 "
                    f"(活跃失败: {failed_active_ids})"
                ),
                "node_id": None,
                "standby_activated": False,
            }

        # === STANDALONE 模式: 单活跃节点 + 热备 ===
        for node_info in active_nodes:
            result = self._try_node(
                node_info["node_id"], callback, cluster,
                request_data, tried_nodes,
            )
            if result.get("success"):
                return {
                    "success": True,
                    "response": result.get("response"),
                    "node_id": node_info["node_id"],
                    "standby_activated": False,
                }

        # 活跃节点失败 → 切换到热备
        if standby_nodes:
            logger.info(
                "集群 %s STANDALONE 活跃节点失败，切换到热备节点 %s",
                cluster.cluster_id, standby_nodes[0]["node_id"],
            )
            standby_result = self._try_node(
                standby_nodes[0]["node_id"], callback, cluster,
                request_data, tried_nodes,
            )
            if standby_result.get("success"):
                return {
                    "success": True,
                    "response": standby_result.get("response"),
                    "node_id": standby_nodes[0]["node_id"],
                    "standby_activated": True,
                }
            # 热备也失败
            self._handle_cluster_failure(cluster, standby_nodes[0]["node_id"])
            return {
                "success": False,
                "error": standby_result.get("error", "热备节点也失败"),
                "node_id": None,
                "standby_activated": True,
            }

        # 无热备可用
        self._handle_cluster_failure(cluster, active_nodes[0]["node_id"] if active_nodes else "")
        return {
            "success": False,
            "error": f"集群 {cluster.cluster_id} 活跃节点和热备节点均不可用",
            "node_id": None,
            "standby_activated": False,
        }

    def _failover_to_standby(
        self,
        cluster: NodeCluster,
        request_data: Optional[Dict[str, Any]],
        callback: Callable,
        standby_nodes: List[Dict[str, Any]],
        failed_active_ids: List[str],
        tried_nodes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        并行模式下的热备故障转移

        将热备节点提升为活跃节点，替换失败的节点，重新执行并行推理。
        """
        # 用热备节点替换失败节点
        standby_idx = 0
        last_result = None
        for node_info in standby_nodes:
            if standby_idx >= len(failed_active_ids):
                break
            last_result = self._try_node(
                node_info["node_id"], callback, cluster,
                request_data, tried_nodes,
            )
            if last_result.get("success"):
                # 记录热备提升事件
                logger.info(
                    "热备节点 %s 已成功接管失败节点 %s 的职责",
                    node_info["node_id"],
                    failed_active_ids[standby_idx],
                )
            standby_idx += 1

        # 检查是否所有替换都成功
        last_success = any(
            t.get("success") for t in tried_nodes
            if t.get("node_id") in [n["node_id"] for n in standby_nodes]
        )

        if last_success and last_result:
            return {
                "success": True,
                "response": last_result.get("response"),
                "node_id": standby_nodes[0]["node_id"],
                "standby_activated": True,
            }

        # 热备全部失败 → 处理集群故障
        for nid in failed_active_ids:
            self._handle_cluster_failure(cluster, nid)
        return {
            "success": False,
            "error": f"集群 {cluster.cluster_id} 热备故障转移失败",
            "node_id": None,
            "standby_activated": True,
        }

    def _handle_cluster_failure(
        self, cluster: NodeCluster, failed_node_id: str
    ) -> None:
        """
        处理集群级故障 — 重组集群或标记为不可用

        策略:
        1. 将失败节点从集群中移除
        2. 如果热备节点已接管，更新节点角色
        3. 如果剩余节点不满足最小需求，标记集群为不活跃
        4. 记录诊断日志
        """
        if not failed_node_id:
            return

        logger.warning(
            "处理集群故障: 集群=%s, 失败节点=%s, 剩余节点=%d, 最小需求=%d",
            cluster.cluster_id,
            failed_node_id,
            len(cluster.nodes),
            cluster.model_profile.min_nodes,
        )

        # 通知集群管理器节点离开
        self._cluster_manager.handle_node_leave(failed_node_id)

        # 检查集群是否仍可用
        if not cluster.is_healthy():
            cluster.is_active = False
            logger.error(
                "集群 %s 因节点 %s 故障不再满足最小需求，标记为不活跃",
                cluster.cluster_id, failed_node_id,
            )

    # ------------------------------------------------------------------
    #  节点变动通知
    # ------------------------------------------------------------------

    def notify_node_leave(self, node_id: str) -> List[str]:
        """
        通知调度器某节点离开

        Returns:
            受影响的会话列表
        """
        # 熔断该节点
        self._circuit_breaker.record_failure(node_id)
        self._circuit_breaker.record_failure(node_id)
        self._circuit_breaker.record_failure(node_id)
        # 直接设为 OPEN
        with self._circuit_breaker._lock:
            self._circuit_breaker._states[node_id] = CircuitState.OPEN
            self._circuit_breaker._last_failure_time[node_id] = time.time()

        # 迁移会话亲和性
        migrated = self._session_affinity.unbind_by_node(node_id)

        # 通知集群管理器
        self._cluster_manager.handle_node_leave(node_id)

        logger.info(
            "节点 %s 离线处理完成: 熔断=OPEN, 迁移会话=%d",
            node_id, len(migrated),
        )
        return migrated

    def notify_node_join(self, node_id: str, node_info: Dict[str, Any]) -> None:
        """通知调度器新节点加入"""
        self._circuit_breaker.reset(node_id)
        self._cluster_manager.handle_node_join(node_id, node_info)
        logger.info("节点 %s 加入，已重置熔断状态", node_id)

    # ------------------------------------------------------------------
    #  自适应调度优化
    # ------------------------------------------------------------------

    def adaptive_pool_resize(
        self, model_name: str, available_nodes: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        自适应节点池缩容

        针对小模型 + 多节点场景，判断是否可以缩容

        Returns:
            {
                "action": "shrink" | "keep" | "expand",
                "current_nodes": int,
                "recommended_nodes": int,
                "reason": str,
            }
        """
        profile = self._get_profile(model_name)
        min_nodes = profile.min_nodes
        current_clusters = self._cluster_manager.get_stats()

        # 获取该模型的活跃集群
        active_count = 0
        total_nodes_in_clusters = 0
        with self._cluster_manager._lock:
            for cid in self._cluster_manager._model_clusters.get(model_name, []):
                cluster = self._cluster_manager._clusters.get(cid)
                if cluster and cluster.is_active:
                    active_count += 1
                    total_nodes_in_clusters += len(cluster.nodes)

        if active_count == 0:
            return {
                "action": "keep",
                "current_nodes": 0,
                "recommended_nodes": min_nodes,
                "reason": f"无活跃集群，建议 {min_nodes} 个节点",
            }

        avg_nodes = total_nodes_in_clusters / active_count

        # STANDALONE 模式只需要 1 个节点
        if profile.mode == InferenceMode.STANDALONE:
            if avg_nodes > 1:
                return {
                    "action": "shrink",
                    "current_nodes": int(avg_nodes),
                    "recommended_nodes": 1,
                    "reason": (
                        f"模型 {model_name} 为 STANDALONE 模式，"
                        f"当前平均 {avg_nodes:.1f} 个节点/集群，建议缩容至 1 个"
                    ),
                }
            return {
                "action": "keep",
                "current_nodes": int(avg_nodes),
                "recommended_nodes": 1,
                "reason": "节点数量合理",
            }

        # 并行模式: 检查是否过度配置
        efficiency = 0.0
        with self._cluster_manager._lock:
            for cid in self._cluster_manager._model_clusters.get(model_name, []):
                cluster = self._cluster_manager._clusters.get(cid)
                if cluster and cluster.is_active:
                    efficiency = max(efficiency, cluster.calculate_efficiency())

        if efficiency < 0.3 and avg_nodes > min_nodes:
            return {
                "action": "shrink",
                "current_nodes": int(avg_nodes),
                "recommended_nodes": min_nodes,
                "reason": (
                    f"集群效率过低 ({efficiency:.2f})，"
                    f"建议从 {int(avg_nodes)} 缩容至 {min_nodes}"
                ),
            }

        return {
            "action": "keep",
            "current_nodes": int(avg_nodes),
            "recommended_nodes": min_nodes,
            "reason": "当前配置合理",
        }

    # ------------------------------------------------------------------
    #  统计与诊断
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """获取调度器统计"""
        with self._lock:
            return {
                "total_scheduled": self._total_scheduled,
                "total_success": self._total_success,
                "total_fallbacks": self._total_fallbacks,
                "total_standby_activations": self._total_standby_activations,
                "success_rate": (
                    round(self._total_success / max(1, self._total_scheduled) * 100, 2)
                ),
                "circuit_breaker": self._circuit_breaker.get_stats(),
                "session_affinity": self._session_affinity.get_stats(),
                "cluster_manager": self._cluster_manager.get_stats(),
                "config": self._config.to_dict(),
                "cached_profiles": len(self._profile_cache),
            }

    def stop(self) -> None:
        """停止调度器 — 停止集群管理器和清理资源"""
        self._cluster_manager.stop()
        self._session_affinity.clear()
        logger.info("混合调度器已停止")

    def diagnose(self) -> Dict[str, Any]:
        """诊断报告 — 详细的系统健康状态"""
        stats = self.get_stats()
        cb = stats["circuit_breaker"]
        clusters = stats["cluster_manager"]

        issues: List[str] = []
        recommendations: List[str] = []

        # 熔断器诊断
        if cb["nodes_open"] > 0:
            issues.append(f"{cb['nodes_open']} 个节点被熔断")
            recommendations.append("检查熔断节点的网络和硬件状态")

        # 集群效率诊断
        if clusters["active_clusters"] > 0:
            low_efficiency = 0
            for cid, cluster in self._cluster_manager.get_all_clusters().items():
                if cluster.is_active and cluster.calculate_efficiency() < 0.4:
                    low_efficiency += 1
            if low_efficiency > 0:
                issues.append(f"{low_efficiency} 个集群效率低于 40%")
                recommendations.append("考虑缩容低效集群或调整并行策略")

        return {
            "healthy": len(issues) == 0,
            "issues": issues,
            "recommendations": recommendations,
            **stats,
        }

