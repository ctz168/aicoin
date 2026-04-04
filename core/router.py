"""
AICoin 最优路由模块
==================
为去中心化 AI 计算网络提供智能路由功能，包括：
- 节点注册与心跳管理
- 网络延迟探测
- 基于多因素加权评分的最优节点选择
- 故障转移与负载均衡
- 请求追踪与计费分析
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import socket
import statistics
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger("aicoin.router")


# ---------------------------------------------------------------------------
# 枚举与配置
# ---------------------------------------------------------------------------

class RoutingStrategy(Enum):
    """路由策略枚举"""
    LATENCY_FIRST = "latency_first"       # 延迟优先
    CAPABILITY_FIRST = "capability_first"  # 算力优先
    COST_FIRST = "cost_first"             # 费用优先
    BALANCED = "balanced"                 # 均衡模式（默认）
    AVAILABILITY_FIRST = "availability_first"  # 可用性优先
    HYBRID = "hybrid"                     # 混合模式 (自动选择 STANDALONE/并行)


class NodeStatus(Enum):
    """节点状态枚举"""
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    DRAINING = "draining"   # 正在排空，不再接收新请求
    UNHEALTHY = "unhealthy"


class RequestPriority(Enum):
    """请求优先级"""
    BASIC = "basic"
    PREMIUM = "premium"
    PRIORITY = "priority"


@dataclass
class RoutingConfig:
    """路由配置 - 所有可调参数的集中管理"""
    # ---- 心跳与超时 ----
    heartbeat_timeout_seconds: float = 30.0       # 节点无心跳超过此时间视为离线
    heartbeat_check_interval: float = 10.0        # 心跳检查间隔（秒）
    node_cleanup_interval: float = 60.0           # 死节点清理间隔（秒）

    # ---- 延迟探测 ----
    probe_timeout_seconds: float = 5.0            # 单次探测超时（秒）
    probe_interval_seconds: float = 60.0          # 全量探测间隔（秒）
    probe_packet_size: int = 64                   # 探测包大小（字节）
    probe_retries: int = 3                        # 探测重试次数
    latency_to_distance_factor: float = 200.0     # 1ms 延迟 ≈ 多少 km

    # ---- 评分权重 (总和应为 1.0) ----
    weight_latency: float = 0.30
    weight_capability: float = 0.25
    weight_cost: float = 0.20
    weight_availability: float = 0.15
    weight_load: float = 0.10

    # ---- 路由行为 ----
    strategy: RoutingStrategy = RoutingStrategy.BALANCED
    fallback_count: int = 2                       # 备用节点数量
    request_timeout_seconds: float = 30.0         # 单次请求超时
    max_concurrent_per_node: int = 100            # 单节点最大并发
    busy_load_threshold: float = 0.80             # 负载超过此值视为繁忙

    # ---- 费用与计费 ----
    cost_per_1k_input_tokens: float = 0.01        # 基础价格 / 1K input tokens
    cost_per_1k_output_tokens: float = 0.03       # 基础价格 / 1K output tokens
    priority_multiplier: float = 2.0              # premium 优先级价格倍率
    priority_max_multiplier: float = 5.0          # priority 优先级价格倍率

    def __post_init__(self) -> None:
        """校验权重总和"""
        total = (self.weight_latency + self.weight_capability +
                 self.weight_cost + self.weight_availability + self.weight_load)
        if not math.isclose(total, 1.0, rel_tol=0.05):
            logger.warning(
                "评分权重总和为 %.3f，建议调整为 1.0（当前按比例归一化处理）", total
            )


# ---------------------------------------------------------------------------
# 节点资料
# ---------------------------------------------------------------------------

@dataclass
class NodeProfile:
    """计算节点资料"""
    id: str
    address: str                           # peer address / multiaddr
    host: str                              # IP 地址或域名
    port: int                              # 服务端口

    capabilities: Set[str] = field(default_factory=set)       # 支持的能力标签
    compute_score: float = 0.0            # 算力评分 0-100
    latency_map: Dict[str, float] = field(default_factory=dict)  # node_id -> ms
    last_heartbeat: Optional[float] = None
    available_models: List[str] = field(default_factory=list)
    gpu_info: Dict[str, Any] = field(default_factory=dict)
    status: NodeStatus = NodeStatus.ONLINE

    # 运行时状态
    current_load: float = 0.0             # 当前负载 0-1
    cost_per_token: float = 1.0           # 该节点每 token 相对成本因子
    geographic_region: str = ""           # 地理位置 (如 "us-west", "eu-central")
    concurrent_requests: int = 0          # 当前并发请求数
    total_requests_served: int = 0        # 历史服务总请求数
    total_failures: int = 0               # 历史总失败次数
    registered_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 混合推理扩展字段
    vram_total_gb: float = 0.0              # GPU 显存总量 (GB)
    vram_available_gb: float = 0.0          # 可用显存 (GB)
    hybrid_mode: str = ""                   # 支持的推理模式

    # ---- 便捷属性 ----
    @property
    def is_alive(self) -> bool:
        return self.status in (NodeStatus.ONLINE, NodeStatus.BUSY)

    @property
    def success_rate(self) -> float:
        """历史成功率"""
        total = self.total_requests_served + self.total_failures
        if total == 0:
            return 1.0  # 无历史记录时默认满分
        return self.total_requests_served / total

    @property
    def effective_capacity(self) -> int:
        """有效剩余容量 = 最大并发 - 当前并发"""
        return max(0, int(self.compute_score) - self.concurrent_requests)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（处理 set 等不可 JSON 化的类型）"""
        d = asdict(self)
        d["capabilities"] = list(self.capabilities)
        d["status"] = self.status.value
        return d


# ---------------------------------------------------------------------------
# 节点注册表
# ---------------------------------------------------------------------------

class NodeRegistry:
    """
    节点注册表 - 维护所有活跃计算节点的信息

    负责：
    - 节点注册 / 注销
    - 心跳更新与超时判定
    - 按模型 / 状态查询节点
    - 定期清理失效节点
    """

    def __init__(self, config: Optional[RoutingConfig] = None):
        self._config = config or RoutingConfig()
        self._nodes: Dict[str, NodeProfile] = {}
        self._lock = threading.RLock()

        # 启动后台清理线程
        self._cleanup_thread: Optional[threading.Thread] = None
        self._running = False
        self.start_background_cleanup()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start_background_cleanup(self) -> None:
        """启动后台清理线程"""
        if self._running:
            return
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="node-registry-cleanup"
        )
        self._cleanup_thread.start()
        logger.info("节点注册表后台清理线程已启动")

    def stop_background_cleanup(self) -> None:
        """停止后台清理线程"""
        self._running = False
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5.0)
        logger.info("节点注册表后台清理线程已停止")

    def _cleanup_loop(self) -> None:
        """定期清理超时节点"""
        while self._running:
            try:
                time.sleep(self._config.heartbeat_check_interval)
                self._check_heartbeats()
            except Exception:
                logger.exception("心跳检查异常")

    def _check_heartbeats(self) -> None:
        """将超时无心跳的节点标记为离线"""
        now = time.time()
        timeout = self._config.heartbeat_timeout_seconds
        expired: List[str] = []

        with self._lock:
            for node_id, node in self._nodes.items():
                if (node.last_heartbeat is not None
                        and now - node.last_heartbeat > timeout):
                    node.status = NodeStatus.OFFLINE
                    expired.append(node_id)
                    logger.info(
                        "节点 %s 心跳超时（上次心跳: %.1fs 前），标记为离线",
                        node_id, now - node.last_heartbeat,
                    )

        if expired:
            logger.warning("共 %d 个节点心跳超时: %s", len(expired), expired)

    # ------------------------------------------------------------------
    # 注册 / 注销
    # ------------------------------------------------------------------

    def register_node(self, node_info: dict) -> bool:
        """
        注册新节点

        Args:
            node_info: 节点信息字典，必须包含 id, host, port。
                       可选: capabilities, compute_score, available_models,
                             gpu_info, cost_per_token, geographic_region, metadata

        Returns:
            是否注册成功
        """
        try:
            required_keys = {"id", "host", "port"}
            missing = required_keys - set(node_info.keys())
            if missing:
                logger.error("注册节点缺少必要字段: %s", missing)
                return False

            node_id = str(node_info["id"])

            with self._lock:
                if node_id in self._nodes:
                    logger.warning("节点 %s 已存在，将更新其信息", node_id)

                now = time.time()
                profile = NodeProfile(
                    id=node_id,
                    address=node_info.get("address", f"{node_info['host']}:{node_info['port']}"),
                    host=str(node_info["host"]),
                    port=int(node_info["port"]),
                    capabilities=set(node_info.get("capabilities", [])),
                    compute_score=float(node_info.get("compute_score", 0.0)),
                    last_heartbeat=now,
                    available_models=list(node_info.get("available_models", [])),
                    gpu_info=node_info.get("gpu_info", {}),
                    status=NodeStatus.ONLINE,
                    cost_per_token=float(node_info.get("cost_per_token", 1.0)),
                    geographic_region=str(node_info.get("geographic_region", "")),
                    metadata=node_info.get("metadata", {}),
                )
                self._nodes[node_id] = profile

            logger.info(
                "节点注册成功: %s (%s:%s), 模型=%s, 算力=%.1f, 区域=%s",
                node_id, profile.host, profile.port,
                profile.available_models, profile.compute_score,
                profile.geographic_region or "未知",
            )
            return True

        except Exception:
            logger.exception("注册节点失败: %s", node_info.get("id", "unknown"))
            return False

    def unregister_node(self, node_id: str) -> bool:
        """
        注销节点

        Args:
            node_id: 要注销的节点 ID

        Returns:
            是否注销成功
        """
        with self._lock:
            node = self._nodes.pop(node_id, None)

        if node:
            logger.info("节点已注销: %s (%s:%s)", node_id, node.host, node.port)
            return True

        logger.warning("尝试注销不存在的节点: %s", node_id)
        return False

    def update_heartbeat(self, node_id: str, compute_info: dict) -> None:
        """
        更新节点心跳和状态

        Args:
            node_id: 节点 ID
            compute_info: 可包含 current_load, concurrent_requests, status, 等
        """
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                logger.debug("心跳来自未注册节点: %s，自动注册", node_id)
                # 自动注册
                self.register_node({"id": node_id, **compute_info})
                return

            now = time.time()
            node.last_heartbeat = now

            # 更新运行时信息
            if "current_load" in compute_info:
                node.current_load = float(compute_info["current_load"])
            if "concurrent_requests" in compute_info:
                node.concurrent_requests = int(compute_info["concurrent_requests"])
            if "compute_score" in compute_info:
                node.compute_score = float(compute_info["compute_score"])
            if "available_models" in compute_info:
                node.available_models = list(compute_info["available_models"])
                node.capabilities = set(compute_info["available_models"])
            if "gpu_info" in compute_info:
                node.gpu_info = compute_info["gpu_info"]
            if "cost_per_token" in compute_info:
                node.cost_per_token = float(compute_info["cost_per_token"])
            if "geographic_region" in compute_info:
                node.geographic_region = str(compute_info["geographic_region"])

            # 根据负载更新状态
            if "status" in compute_info:
                try:
                    node.status = NodeStatus(compute_info["status"])
                except ValueError:
                    logger.warning("节点 %s 状态值无效: %s", node_id, compute_info["status"])
            else:
                # 自动推断状态
                if node.current_load >= self._config.busy_load_threshold:
                    node.status = NodeStatus.BUSY
                elif node.status in (NodeStatus.OFFLINE, NodeStatus.UNHEALTHY):
                    node.status = NodeStatus.ONLINE

        logger.debug("节点 %s 心跳更新，负载=%.2f", node_id, node.current_load)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_alive_nodes(self) -> List[NodeProfile]:
        """获取所有活跃节点（最近有心跳且非离线状态）"""
        now = time.time()
        with self._lock:
            alive = [
                n for n in self._nodes.values()
                if n.is_alive
                and (n.last_heartbeat is None
                     or now - n.last_heartbeat <= self._config.heartbeat_timeout_seconds)
            ]
        return alive

    def get_nodes_by_model(self, model_name: str) -> List[NodeProfile]:
        """获取能运行指定模型的所有活跃节点"""
        alive = self.get_alive_nodes()
        return [n for n in alive if model_name in n.available_models or model_name in n.capabilities]

    def get_node_info(self, node_id: str) -> Optional[Dict[str, Any]]:
        """获取节点详细信息，不存在则返回 None"""
        with self._lock:
            node = self._nodes.get(node_id)
        return node.to_dict() if node else None

    def get_node(self, node_id: str) -> Optional[NodeProfile]:
        """获取节点对象（内部使用）"""
        with self._lock:
            return self._nodes.get(node_id)

    @property
    def total_nodes(self) -> int:
        with self._lock:
            return len(self._nodes)

    @property
    def alive_node_count(self) -> int:
        return len(self.get_alive_nodes())

    def get_all_nodes(self) -> Dict[str, NodeProfile]:
        """获取所有节点的浅拷贝"""
        with self._lock:
            return dict(self._nodes)

    def update_node_stats(self, node_id: str, success: bool = True) -> None:
        """更新节点请求统计"""
        with self._lock:
            node = self._nodes.get(node_id)
            if node:
                if success:
                    node.total_requests_served += 1
                else:
                    node.total_failures += 1

    def __len__(self) -> int:
        return self.total_nodes

    def __contains__(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self._nodes

    def __repr__(self) -> str:
        return f"NodeRegistry(nodes={self.total_nodes}, alive={self.alive_node_count})"


# ---------------------------------------------------------------------------
# 延迟探测器
# ---------------------------------------------------------------------------

class LatencyProbe:
    """
    延迟探测器 - 测量节点间网络延迟

    实现方式：
    - TCP 连接建立时间作为延迟近似值
    - 支持异步批量探测
    - 延迟矩阵缓存
    - 基于延迟估算地理距离
    """

    def __init__(self, registry: NodeRegistry, config: Optional[RoutingConfig] = None):
        self._registry = registry
        self._config = config or RoutingConfig()
        self._lock = threading.RLock()
        self._latency_cache: Dict[str, float] = {}           # node_id -> latency_ms
        self._latency_matrix: Dict[str, Dict[str, float]] = {}  # from -> {to -> ms}
        self._last_full_probe: float = 0.0

        # 后台探测线程
        self._probe_thread: Optional[threading.Thread] = None
        self._running = False
        self._probe_callback: Optional[Callable[[Dict[str, float]], None]] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start_background_probe(self) -> None:
        """启动后台定期探测"""
        if self._running:
            return
        self._running = True
        self._probe_thread = threading.Thread(
            target=self._probe_loop, daemon=True, name="latency-probe"
        )
        self._probe_thread.start()
        logger.info("延迟探测器后台线程已启动")

    def stop_background_probe(self) -> None:
        """停止后台探测"""
        self._running = False
        if self._probe_thread and self._probe_thread.is_alive():
            self._probe_thread.join(timeout=10.0)
        logger.info("延迟探测器后台线程已停止")

    def _probe_loop(self) -> None:
        """定期全量探测"""
        while self._running:
            try:
                interval = self._config.probe_interval_seconds
                time.sleep(max(1.0, interval))
                results = self.probe_all_nodes()
                if results:
                    logger.info("全量延迟探测完成，共 %d 个节点", len(results))
                    if self._probe_callback:
                        try:
                            self._probe_callback(results)
                        except Exception:
                            logger.exception("探测回调异常")
            except Exception:
                logger.exception("后台延迟探测异常")

    def set_probe_callback(self, callback: Callable[[Dict[str, float]], None]) -> None:
        """设置探测完成后的回调函数"""
        self._probe_callback = callback

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------

    def probe_node(self, node_id: str) -> float:
        """
        测量到指定节点的延迟（毫秒）

        通过 TCP 连接建立时间测量 RTT，
        多次尝试取中位数以降低波动。

        Args:
            node_id: 目标节点 ID

        Returns:
            延迟（毫秒），探测失败返回 -1.0
        """
        node = self._registry.get_node(node_id)
        if node is None:
            logger.warning("探测失败: 节点 %s 不存在", node_id)
            return -1.0

        latencies: List[float] = []
        timeout = self._config.probe_timeout_seconds

        for attempt in range(self._config.probe_retries):
            try:
                start = time.monotonic()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect((node.host, node.port))
                elapsed_ms = (time.monotonic() - start) * 1000.0
                sock.close()
                latencies.append(elapsed_ms)
            except socket.timeout:
                logger.debug("探测 %s 第 %d 次超时", node_id, attempt + 1)
            except OSError as e:
                logger.debug("探测 %s 第 %d 次失败: %s", node_id, attempt + 1, e)
            except Exception:
                logger.debug("探测 %s 第 %d 次异常", node_id, attempt + 1)

        if not latencies:
            logger.warning("节点 %s 全部 %d 次探测失败", node_id, self._config.probe_retries)
            return -1.0

        # 取中位数以过滤极端值
        median_latency = statistics.median(latencies)
        min_latency = min(latencies)
        avg_latency = statistics.mean(latencies)

        with self._lock:
            self._latency_cache[node_id] = median_latency

        logger.debug(
            "节点 %s 延迟探测: min=%.1fms, med=%.1fms, avg=%.1fms (retries=%d)",
            node_id, min_latency, median_latency, avg_latency, len(latencies),
        )
        return median_latency

    def probe_all_nodes(self) -> Dict[str, float]:
        """
        测量到所有活跃节点的延迟

        Returns:
            {node_id: latency_ms} 字典
        """
        alive = self._registry.get_alive_nodes()
        results: Dict[str, float] = {}

        for node in alive:
            latency = self.probe_node(node.id)
            results[node.id] = latency

        self._last_full_probe = time.time()
        return results

    def get_latency(self, node_id: str) -> float:
        """
        获取节点的缓存延迟值，如无缓存则即时探测

        Returns:
            延迟（毫秒），失败返回 -1.0
        """
        with self._lock:
            cached = self._latency_cache.get(node_id)

        if cached is not None:
            return cached

        # 缓存未命中，执行即时探测
        return self.probe_node(node_id)

    def get_latency_matrix(self) -> Dict[str, Dict[str, float]]:
        """
        获取延迟矩阵

        在当前实现中，延迟矩阵表示从本机到各节点的延迟。
        完整的节点间延迟矩阵需要在各节点部署探测器后汇总。

        Returns:
            {"local": {node_id: latency_ms, ...}}
        """
        # 确保缓存是最新的
        if time.time() - self._last_full_probe > self._config.probe_interval_seconds:
            self.probe_all_nodes()

        with self._lock:
            self._latency_matrix["local"] = dict(self._latency_cache)

        return self._latency_matrix

    def estimate_geographic_distance(self, node_id: str) -> float:
        """
        基于延迟估算地理距离（公里）

        光在光纤中传播速度约 200,000 km/s，
        因此 1ms 单程延迟 ≈ 200km（考虑 RTT 则除以 2）。

        Args:
            node_id: 目标节点 ID

        Returns:
            估算距离（公里），失败返回 -1.0
        """
        latency = self.get_latency(node_id)
        if latency < 0:
            return -1.0

        # RTT / 2 = 单程延迟，然后乘以光纤传输因子
        one_way_ms = latency / 2.0
        distance_km = one_way_ms * self._config.latency_to_distance_factor

        return round(distance_km, 1)

    def update_latency(self, node_id: str, latency_ms: float) -> None:
        """
        外部更新节点延迟（例如从心跳包中获取的 RTT 信息）

        Args:
            node_id: 节点 ID
            latency_ms: 延迟（毫秒）
        """
        if latency_ms < 0:
            return
        with self._lock:
            self._latency_cache[node_id] = latency_ms

    @property
    def last_full_probe_time(self) -> float:
        return self._last_full_probe


# ---------------------------------------------------------------------------
# 最优路由引擎
# ---------------------------------------------------------------------------

class OptimalRouter:
    """
    最优路由引擎 - 为每个 API 调用选择最佳节点

    评分公式:
        score = w1 * normalize(latency) + w2 * normalize(capability)
              + w3 * normalize(cost)    + w4 * normalize(availability)
              + w5 * normalize(load)

    所有维度归一化到 [0, 1]，其中:
        - latency: 越低越好 → score 用 (1 - normalized)
        - capability: 越高越好 → score 用 normalized
        - cost: 越低越好 → score 用 (1 - normalized)
        - availability: 越高越好 → score 用 normalized
        - load: 越低越好 → score 用 (1 - normalized)

    最终 score 越高代表节点越优。
    """

    def __init__(
        self,
        registry: NodeRegistry,
        probe: LatencyProbe,
        config: Optional[RoutingConfig] = None,
    ):
        self._registry = registry
        self._probe = probe
        self._config = config or RoutingConfig()
        self._lock = threading.RLock()

        # 路由统计
        self._total_requests: int = 0
        self._total_success: int = 0
        self._total_failures: int = 0
        self._total_latency: float = 0.0  # 累计延迟（ms）
        self._node_request_counts: Dict[str, int] = {}
        self._recent_latencies: List[float] = []  # 最近 N 次请求延迟

        # 执行回调 - 实际发送请求的函数
        self._execute_callback: Optional[Callable] = None

        # 启动后台探测
        self._probe.start_background_probe()

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def set_execute_callback(self, callback: Callable) -> None:
        """
        设置请求执行回调函数

        回调签名: async def callback(node_id: str, request_data: dict) -> dict
        返回: {"success": bool, "response": Any, "error": str|None}
        """
        self._execute_callback = callback

    # ------------------------------------------------------------------
    # 评分
    # ------------------------------------------------------------------

    def _normalize(
        self,
        value: float,
        min_val: float,
        max_val: float,
        higher_is_better: bool = True,
    ) -> float:
        """
        将值归一化到 [0, 1]

        Args:
            value: 原始值
            min_val: 最小值
            max_val: 最大值
            higher_is_better: True 表示值越大越好，False 表示越小越好

        Returns:
            归一化后的分数 [0, 1]
        """
        if max_val <= min_val:
            return 1.0 if higher_is_better else 0.5

        clamped = max(min_val, min(max_val, value))
        normalized = (clamped - min_val) / (max_val - min_val)

        if not higher_is_better:
            normalized = 1.0 - normalized

        return normalized

    def _compute_node_score(
        self,
        node: NodeProfile,
        latency_ms: float,
        all_nodes: List[NodeProfile],
        strategy: Optional[RoutingStrategy] = None,
    ) -> float:
        """
        计算节点综合评分

        Args:
            node: 目标节点
            latency_ms: 到该节点的延迟
            all_nodes: 所有候选节点（用于归一化参考范围）
            strategy: 路由策略，默认使用配置中的策略

        Returns:
            综合评分 [0, 1]
        """
        cfg = self._config
        strategy = strategy or cfg.strategy

        if not all_nodes:
            return 0.5

        # 收集归一化范围
        latencies = [
            self._probe.get_latency(n.id)
            for n in all_nodes
            if self._probe.get_latency(n.id) >= 0
        ]
        scores = [n.compute_score for n in all_nodes]
        costs = [n.cost_per_token for n in all_nodes]
        loads = [n.current_load for n in all_nodes]
        avail = [n.success_rate for n in all_nodes]

        lat_min = min(latencies) if latencies else 0
        lat_max = max(latencies) if latencies else 100
        score_min = min(scores) if scores else 0
        score_max = max(scores) if scores else 100
        cost_min = min(costs) if costs else 0.5
        cost_max = max(costs) if costs else 2.0
        load_min = min(loads) if loads else 0
        load_max = max(loads) if loads else 1.0
        avail_min = min(avail) if avail else 0
        avail_max = max(avail) if avail else 1.0

        # 延迟: 越低越好
        effective_latency = latency_ms if latency_ms >= 0 else lat_max
        s_latency = self._normalize(effective_latency, lat_min, lat_max, higher_is_better=False)

        # 算力: 越高越好
        s_capability = self._normalize(node.compute_score, score_min, score_max, higher_is_better=True)

        # 费用: 越低越好
        s_cost = self._normalize(node.cost_per_token, cost_min, cost_max, higher_is_better=False)

        # 可用性 (成功率): 越高越好
        s_availability = self._normalize(node.success_rate, avail_min, avail_max, higher_is_better=True)

        # 负载: 越低越好
        s_load = self._normalize(node.current_load, load_min, load_max, higher_is_better=False)

        # 根据策略调整权重
        if strategy == RoutingStrategy.LATENCY_FIRST:
            w = (0.50, 0.15, 0.10, 0.15, 0.10)
        elif strategy == RoutingStrategy.CAPABILITY_FIRST:
            w = (0.15, 0.50, 0.10, 0.15, 0.10)
        elif strategy == RoutingStrategy.COST_FIRST:
            w = (0.15, 0.15, 0.50, 0.10, 0.10)
        elif strategy == RoutingStrategy.AVAILABILITY_FIRST:
            w = (0.15, 0.15, 0.10, 0.50, 0.10)
        else:
            w = (
                cfg.weight_latency,
                cfg.weight_capability,
                cfg.weight_cost,
                cfg.weight_availability,
                cfg.weight_load,
            )

        final_score = (
            w[0] * s_latency
            + w[1] * s_capability
            + w[2] * s_cost
            + w[3] * s_availability
            + w[4] * s_load
        )

        logger.debug(
            "节点 %s 评分=%.4f (延迟=%.1f→%.2f, 算力=%.1f→%.2f, "
            "费用=%.2f→%.2f, 可用性=%.2f→%.2f, 负载=%.2f→%.2f)",
            node.id, final_score,
            effective_latency, s_latency,
            node.compute_score, s_capability,
            node.cost_per_token, s_cost,
            node.success_rate, s_availability,
            node.current_load, s_load,
        )

        return final_score

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def find_best_node(
        self,
        model_name: str,
        requester_location: str = None,
        priority: str = "basic",
    ) -> Optional[str]:
        """
        为 API 调用找到最佳节点

        综合考虑: 延迟(30%), 算力能力(25%), 费用(20%), 可用性(15%), 负载(10%)

        Args:
            model_name: 需要运行的模型名称
            requester_location: 请求者位置（用于就近路由的提示）
            priority: API 调用优先级 (basic/premium/priority)

        Returns:
            最佳节点 ID，无可用节点时返回 None
        """
        candidates = self._registry.get_nodes_by_model(model_name)

        if not candidates:
            logger.warning("没有能运行模型 %s 的节点", model_name)
            return None

        # 过滤掉过载节点（优先级请求可以使用繁忙节点）
        try:
            priority_enum = RequestPriority(priority)
        except ValueError:
            priority_enum = RequestPriority.BASIC

        usable: List[NodeProfile] = []
        for node in candidates:
            if node.status == NodeStatus.DRAINING:
                continue
            if (node.current_load >= self._config.busy_load_threshold
                    and priority_enum == RequestPriority.BASIC):
                continue
            if node.concurrent_requests >= self._config.max_concurrent_per_node:
                continue
            usable.append(node)

        if not usable:
            # 降级: 允许使用繁忙节点
            logger.info("所有 %s 节点过载，降级使用繁忙节点", model_name)
            usable = [n for n in candidates if n.status != NodeStatus.DRAINING]

        if not usable:
            logger.error("没有任何可用节点可运行模型 %s", model_name)
            return None

        # 优先级策略映射
        strategy_map = {
            RequestPriority.BASIC: RoutingStrategy.BALANCED,
            RequestPriority.PREMIUM: RoutingStrategy.LATENCY_FIRST,
            RequestPriority.PRIORITY: RoutingStrategy.LATENCY_FIRST,
        }
        strategy = strategy_map.get(priority_enum, RoutingStrategy.BALANCED)

        # 如果有请求者位置，优先同区域节点
        if requester_location:
            same_region = [n for n in usable if n.geographic_region == requester_location]
            if same_region:
                usable = same_region  # 缩小候选范围

        # 评分并排序
        scored: List[Tuple[NodeProfile, float]] = []
        for node in usable:
            latency = self._probe.get_latency(node.id)
            score = self._compute_node_score(node, latency, usable, strategy)
            scored.append((node, score))

        scored.sort(key=lambda x: x[1], reverse=True)

        best_node, best_score = scored[0]
        logger.info(
            "最佳节点选择: 模型=%s, 节点=%s, 评分=%.4f, "
            "策略=%s, 优先级=%s, 候选数=%d",
            model_name, best_node.id, best_score,
            strategy.value, priority, len(usable),
        )

        return best_node.id

    def find_backup_nodes(
        self,
        model_name: str,
        primary_node: str,
        count: int = 2,
    ) -> List[str]:
        """
        找到备用节点列表（故障转移用）

        Args:
            model_name: 模型名称
            primary_node: 主节点 ID（排除）
            count: 需要的备用节点数量

        Returns:
            按评分降序排列的备用节点 ID 列表
        """
        candidates = self._registry.get_nodes_by_model(model_name)
        filtered = [n for n in candidates if n.id != primary_node and n.status != NodeStatus.DRAINING]

        if not filtered:
            return []

        scored: List[Tuple[NodeProfile, float]] = []
        for node in filtered:
            latency = self._probe.get_latency(node.id)
            score = self._compute_node_score(node, latency, filtered)
            scored.append((node, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        backups = [node.id for node, _ in scored[:count]]

        logger.debug(
            "模型 %s 的备用节点: %s（排除主节点 %s）",
            model_name, backups, primary_node,
        )
        return backups

    def route_with_fallback(
        self,
        model_name: str,
        request_data: dict,
        priority: str = "basic",
    ) -> Dict[str, Any]:
        """
        带故障转移的路由

        尝试主节点 → 备用节点 1 → 备用节点 2 → ...

        Args:
            model_name: 需要运行的模型
            request_data: 请求数据
            priority: 优先级

        Returns:
            {
                "success": bool,
                "node_id": str | None,
                "response": Any,
                "tried_nodes": [{"node_id", "success", "error", "latency_ms"}],
                "total_latency_ms": float,
            }
        """
        tried_nodes: List[Dict[str, Any]] = []
        start_time = time.monotonic()

        # 选择主节点
        primary = self.find_best_node(model_name, priority=priority)
        if primary is None:
            return {
                "success": False,
                "node_id": None,
                "response": None,
                "error": f"没有可用节点运行模型 {model_name}",
                "tried_nodes": tried_nodes,
                "total_latency_ms": 0.0,
            }

        # 构建尝试顺序
        backup_nodes = self.find_backup_nodes(model_name, primary, count=self._config.fallback_count)
        nodes_to_try = [primary] + backup_nodes

        last_error: Optional[str] = None

        for node_id in nodes_to_try:
            node_start = time.monotonic()
            logger.info("尝试路由到节点 %s（模型=%s）", node_id, model_name)

            try:
                if self._execute_callback is not None:
                    result = self._execute_callback(node_id, request_data)
                    # 支持同步和异步回调
                    if asyncio.iscoroutine(result):
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            # 无法在运行中的事件循环中 await，需要创建新线程
                            import concurrent.futures
                            with concurrent.futures.ThreadPoolExecutor() as pool:
                                result = pool.submit(asyncio.run, result).result(
                                    timeout=self._config.request_timeout_seconds
                                )
                        else:
                            result = loop.run_until_complete(result)
                else:
                    # 无回调时模拟成功（用于测试）
                    result = {"success": True, "response": {"simulated": True}}

                node_latency = (time.monotonic() - node_start) * 1000.0
                success = result.get("success", False)

                tried_nodes.append({
                    "node_id": node_id,
                    "success": success,
                    "error": result.get("error"),
                    "latency_ms": round(node_latency, 2),
                })

                if success:
                    self._registry.update_node_stats(node_id, success=True)
                    with self._lock:
                        self._total_requests += 1
                        self._total_success += 1
                        self._total_latency += node_latency
                        self._node_request_counts[node_id] = (
                            self._node_request_counts.get(node_id, 0) + 1
                        )
                        self._recent_latencies.append(node_latency)
                        if len(self._recent_latencies) > 1000:
                            self._recent_latencies = self._recent_latencies[-500:]

                    total_latency = (time.monotonic() - start_time) * 1000.0
                    logger.info(
                        "路由成功: 节点=%s, 模型=%s, 延迟=%.1fms, 总耗时=%.1fms",
                        node_id, model_name, node_latency, total_latency,
                    )
                    return {
                        "success": True,
                        "node_id": node_id,
                        "response": result.get("response"),
                        "tried_nodes": tried_nodes,
                        "total_latency_ms": round(total_latency, 2),
                    }
                else:
                    last_error = result.get("error", "未知错误")
                    self._registry.update_node_stats(node_id, success=False)
                    logger.warning("节点 %s 返回失败: %s", node_id, last_error)

            except Exception as e:
                node_latency = (time.monotonic() - node_start) * 1000.0
                last_error = str(e)
                tried_nodes.append({
                    "node_id": node_id,
                    "success": False,
                    "error": last_error,
                    "latency_ms": round(node_latency, 2),
                })
                self._registry.update_node_stats(node_id, success=False)
                logger.exception("路由到节点 %s 时异常", node_id)

        # 所有节点都失败
        total_latency = (time.monotonic() - start_time) * 1000.0
        with self._lock:
            self._total_requests += 1
            self._total_failures += 1

        logger.error(
            "路由全部失败: 模型=%s, 尝试了 %d 个节点, 总耗时=%.1fms, 最后错误: %s",
            model_name, len(tried_nodes), total_latency, last_error,
        )
        return {
            "success": False,
            "node_id": None,
            "response": None,
            "error": last_error or "所有节点均不可用",
            "tried_nodes": tried_nodes,
            "total_latency_ms": round(total_latency, 2),
        }

    # ------------------------------------------------------------------
    # 高级路由
    # ------------------------------------------------------------------

    def load_balance(
        self,
        model_name: str,
        total_requests: int,
    ) -> Dict[str, int]:
        """
        负载均衡分配: 将多个请求分配到多个节点

        使用加权轮询算法，权重 = 节点评分 * 剩余容量

        Args:
            model_name: 模型名称
            total_requests: 需要分配的请求总数

        Returns:
            {node_id: request_count} 分配结果
        """
        candidates = self._registry.get_nodes_by_model(model_name)
        if not candidates:
            logger.warning("负载均衡: 没有可用节点运行模型 %s", model_name)
            return {}

        # 计算各节点权重
        weights: Dict[str, float] = {}
        for node in candidates:
            if node.status == NodeStatus.DRAINING:
                continue
            latency = self._probe.get_latency(node.id)
            score = self._compute_node_score(node, latency, candidates)
            # 剩余容量因子
            capacity_factor = max(0.1, 1.0 - node.current_load)
            weights[node.id] = score * capacity_factor

        if not weights:
            return {}

        # 按权重分配
        total_weight = sum(weights.values())
        allocation: Dict[str, int] = {nid: 0 for nid in weights}

        remaining = total_requests
        for _ in range(total_requests):
            if remaining <= 0:
                break
            # 加权随机选择
            r = __import__("random").random() * total_weight
            cumulative = 0.0
            for nid, w in weights.items():
                cumulative += w
                if r <= cumulative:
                    allocation[nid] += 1
                    remaining -= 1
                    # 降低该节点权重以实现更均匀的分配
                    weights[nid] *= 0.9
                    total_weight = sum(weights.values())
                    break

        logger.info(
            "负载均衡分配: 模型=%s, 总请求=%d, 节点数=%d, 分配=%s",
            model_name, total_requests, len(allocation),
            {k: v for k, v in allocation.items() if v > 0},
        )
        return allocation

    def get_routing_table(self) -> Dict[str, Any]:
        """
        获取当前路由表

        Returns:
            {
                "nodes": {node_id: {模型列表, 状态, 负载, 延迟, 评分}},
                "models": {model_name: [node_ids]},
                "timestamp": ISO 时间戳,
            }
        """
        now = datetime.now(timezone.utc).isoformat()
        all_nodes = self._registry.get_all_nodes()

        nodes_info: Dict[str, Dict[str, Any]] = {}
        models_map: Dict[str, List[str]] = {}

        for node_id, node in all_nodes.items():
            latency = self._probe.get_latency(node_id)
            nodes_info[node_id] = {
                "host": f"{node.host}:{node.port}",
                "status": node.status.value,
                "models": node.available_models,
                "load": round(node.current_load, 3),
                "latency_ms": latency,
                "compute_score": node.compute_score,
                "concurrent_requests": node.concurrent_requests,
                "region": node.geographic_region,
            }
            for model in node.available_models:
                models_map.setdefault(model, []).append(node_id)

        return {
            "nodes": nodes_info,
            "models": models_map,
            "total_nodes": len(all_nodes),
            "alive_nodes": self._registry.alive_node_count,
            "timestamp": now,
        }

    def get_routing_stats(self) -> Dict[str, Any]:
        """
        获取路由统计

        Returns:
            {
                "total_requests": 总请求数,
                "success_count": 成功数,
                "failure_count": 失败数,
                "success_rate": 成功率,
                "avg_latency_ms": 平均延迟,
                "p50_latency_ms": P50 延迟,
                "p95_latency_ms": P95 延迟,
                "p99_latency_ms": P99 延迟,
                "node_utilization": {node_id: 请求占比},
                "registry": {总节点数, 活跃节点数},
            }
        """
        with self._lock:
            total = self._total_requests
            success = self._total_success
            failures = self._total_failures
            total_lat = self._total_latency
            recent = list(self._recent_latencies)
            node_counts = dict(self._node_request_counts)

        success_rate = (success / total * 100.0) if total > 0 else 0.0
        avg_latency = (total_lat / success) if success > 0 else 0.0

        # 延迟分位数
        p50 = p95 = p99 = 0.0
        if recent:
            recent_sorted = sorted(recent)
            n = len(recent_sorted)
            p50 = recent_sorted[int(n * 0.50)]
            p95 = recent_sorted[int(n * 0.95)] if n > 1 else recent_sorted[-1]
            p99 = recent_sorted[int(n * 0.99)] if n > 1 else recent_sorted[-1]

        # 节点利用率
        utilization: Dict[str, float] = {}
        if total > 0:
            for nid, count in node_counts.items():
                utilization[nid] = round(count / total * 100.0, 2)

        return {
            "total_requests": total,
            "success_count": success,
            "failure_count": failures,
            "success_rate": round(success_rate, 2),
            "avg_latency_ms": round(avg_latency, 2),
            "p50_latency_ms": round(p50, 2),
            "p95_latency_ms": round(p95, 2),
            "p99_latency_ms": round(p99, 2),
            "node_utilization": utilization,
            "registry": {
                "total_nodes": self._registry.total_nodes,
                "alive_nodes": self._registry.alive_node_count,
            },
        }

    def shutdown(self) -> None:
        """关闭路由器，停止后台线程"""
        self._probe.stop_background_probe()
        self._registry.stop_background_cleanup()
        logger.info("最优路由引擎已关闭")

    # ------------------------------------------------------------------
    # 混合推理路由
    # ------------------------------------------------------------------

    @property
    def _hybrid_scheduler(self):  # type: ignore
        """
        懒加载 HybridScheduler 实例，避免循环导入。

        延迟导入 core.hybrid_inference 模块并缓存实例，
        仅在首次调用 route_hybrid / get_hybrid_stats 时创建。
        """
        if not hasattr(self, "_hybrid_scheduler_instance") or self._hybrid_scheduler_instance is None:
            from aicoin.core.hybrid_inference import HybridScheduler  # lazy import
            self._hybrid_scheduler_instance = HybridScheduler()
            logger.info("HybridScheduler 懒加载完成")
        return self._hybrid_scheduler_instance

    def route_hybrid(
        self,
        model_name: str,
        request_data: dict,
        session_key: Optional[str] = None,
        priority: str = "basic",
    ) -> Dict[str, Any]:
        """
        混合推理路由 — 根据模型大小自动选择推理策略

        小模型(≤15B): 单节点推理 + 热备冗余
        中模型(16-40B): 张量并行 + 热备冗余
        大模型(>40B): 张量/流水线并行 + 热备冗余

        通过 HybridScheduler 统一调度:
        - 模型分级 → 自动确定推理模式
        - 冗余策略 → 适度冗余保障稳定性
        - 熔断保护 → 故障节点自动隔离
        - 会话亲和性 → 多轮对话保持一致性

        Args:
            model_name: 模型名称
            request_data: 请求数据
            session_key: 会话标识 (用于多轮对话)
            priority: 优先级 (basic/premium/priority)

        Returns:
            {
                "success": bool,
                "routing_mode": "hybrid",
                "cluster_id": str,
                "inference_mode": str,
                "node_id": str,
                "response": Any,
                "tried_nodes": [...],
                "redundancy_policy": {...},
                "total_latency_ms": float,
            }
        """
        start_time = time.monotonic()
        scheduler = self._hybrid_scheduler

        # ------------------------------------------------------------------
        # 1. 收集可用节点并转换为 HybridScheduler 期望的 dict 格式
        # ------------------------------------------------------------------
        alive_nodes = self._registry.get_nodes_by_model(model_name)

        # 进一步过滤掉 DRAINING / UNHEALTHY 的节点
        usable_nodes = [
            n for n in alive_nodes
            if n.status not in (NodeStatus.DRAINING, NodeStatus.UNHEALTHY)
        ]

        if not usable_nodes:
            logger.warning(
                "route_hybrid: 没有可用节点运行模型 %s", model_name,
            )
            return {
                "success": False,
                "routing_mode": "hybrid",
                "cluster_id": None,
                "inference_mode": None,
                "node_id": None,
                "response": None,
                "tried_nodes": [],
                "redundancy_policy": {},
                "total_latency_ms": round((time.monotonic() - start_time) * 1000.0, 2),
                "error": f"没有可用节点运行模型 {model_name}",
            }

        available_nodes: List[Dict[str, Any]] = []
        for node in usable_nodes:
            node_dict: Dict[str, Any] = {
                "id": node.id,
                "vram_gb": node.vram_available_gb,
                "load": node.current_load,
                "latency_ms": self._probe.get_latency(node.id),
                "compute_score": node.compute_score,
                "host": node.host,
                "port": node.port,
                "status": node.status.value,
                "concurrent_requests": node.concurrent_requests,
                "success_rate": node.success_rate,
                "gpu_info": node.gpu_info,
                "geographic_region": node.geographic_region,
            }
            available_nodes.append(node_dict)

        # ------------------------------------------------------------------
        # 2. 构造 execute_callback 桥接函数
        #    HybridScheduler 期望签名: callback(node_id, cluster, data) -> dict
        #    内部 _execute_callback 签名:  callback(node_id, request_data) -> dict
        # ------------------------------------------------------------------
        def _hybrid_execute_callback(
            node_id: str,
            cluster: Any,
            data: dict,
        ) -> dict:
            """桥接 HybridScheduler 回调签名与内部 _execute_callback"""
            if self._execute_callback is not None:
                result = self._execute_callback(node_id, data)
                # 支持异步回调
                if asyncio.iscoroutine(result):
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            result = pool.submit(asyncio.run, result).result(
                                timeout=self._config.request_timeout_seconds
                            )
                    else:
                        result = loop.run_until_complete(result)
                return result
            # 无回调时模拟成功（用于测试）
            return {"success": True, "response": {"simulated": True, "hybrid": True}}

        # ------------------------------------------------------------------
        # 3. 调用 HybridScheduler.schedule()
        # ------------------------------------------------------------------
        logger.info(
            "route_hybrid: 模型=%s, 可用节点=%d, 会话=%s, 优先级=%s",
            model_name, len(available_nodes),
            session_key or "无", priority,
        )

        result = scheduler.schedule(
            model_name=model_name,
            available_nodes=available_nodes,
            session_key=session_key,
            execute_callback=_hybrid_execute_callback,
            request_data=request_data,
            priority=priority,
        )

        # ------------------------------------------------------------------
        # 4. 更新路由统计
        # ------------------------------------------------------------------
        total_latency = (time.monotonic() - start_time) * 1000.0
        success = result.get("success", False)

        if success:
            node_id = result.get("node_id")
            if node_id:
                self._registry.update_node_stats(node_id, success=True)
            with self._lock:
                self._total_requests += 1
                self._total_success += 1
                self._total_latency += total_latency
                if node_id:
                    self._node_request_counts[node_id] = (
                        self._node_request_counts.get(node_id, 0) + 1
                    )
                self._recent_latencies.append(total_latency)
                if len(self._recent_latencies) > 1000:
                    self._recent_latencies = self._recent_latencies[-500:]
        else:
            with self._lock:
                self._total_requests += 1
                self._total_failures += 1
            # 更新失败节点统计
            for tried in result.get("tried_nodes", []):
                if not tried.get("success", True):
                    nid = tried.get("node_id")
                    if nid:
                        self._registry.update_node_stats(nid, success=False)

        # ------------------------------------------------------------------
        # 5. 组装冗余策略信息
        # ------------------------------------------------------------------
        redundancy_policy: Dict[str, Any] = {
            "cluster_id": result.get("cluster_id"),
            "inference_mode": result.get("mode"),
            "tensor_parallel_size": result.get("tensor_parallel_size", 1),
            "pipeline_parallel_size": result.get("pipeline_parallel_size", 1),
        }

        logger.info(
            "route_hybrid 完成: 模型=%s, 成功=%s, 模式=%s, 耗时=%.1fms",
            model_name, success, result.get("mode"), total_latency,
        )

        return {
            "success": success,
            "routing_mode": "hybrid",
            "cluster_id": result.get("cluster_id"),
            "inference_mode": result.get("mode"),
            "node_id": result.get("node_id"),
            "response": result.get("response"),
            "tried_nodes": result.get("tried_nodes", []),
            "redundancy_policy": redundancy_policy,
            "total_latency_ms": round(total_latency, 2),
            "error": result.get("error"),
        }

    def get_hybrid_stats(self) -> Dict[str, Any]:
        """
        获取 HybridScheduler 统计信息

        Returns:
            {
                "total_scheduled": int,
                "total_success": int,
                "total_fallbacks": int,
                "success_rate": float,
                "circuit_breaker": {...},
                "session_affinity": {...},
                "cluster_manager": {...},
                "config": {...},
            }
        """
        return self._hybrid_scheduler.get_stats()


# ---------------------------------------------------------------------------
# 请求追踪器
# ---------------------------------------------------------------------------

@dataclass
class RequestRecord:
    """单个请求的追踪记录"""
    request_id: str
    node_id: str
    model_name: str
    priority: str
    burner_address: str
    start_time: float
    end_time: Optional[float] = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    status: str = "pending"  # pending / completed / failed
    error: Optional[str] = None
    cost: float = 0.0

    @property
    def duration_ms(self) -> float:
        end = self.end_time or time.time()
        return (end - self.start_time) * 1000.0


class RequestTracker:
    """
    请求追踪器 - 追踪所有 API 请求用于计费和分析

    功能：
    - 记录请求生命周期（开始 → 完成/失败）
    - 节点级别统计
    - 地址级别账单汇总
    - 自动清理过期记录
    """

    def __init__(self, config: Optional[RoutingConfig] = None):
        self._config = config or RoutingConfig()
        self._lock = threading.RLock()
        self._requests: Dict[str, RequestRecord] = {}
        self._node_stats: Dict[str, Dict[str, Any]] = {}  # node_id -> stats dict
        self._address_billing: Dict[str, Dict[str, Any]] = {}  # address -> billing dict

        # 后台清理
        self._running = False
        self._cleanup_thread: Optional[threading.Thread] = None
        self._record_ttl = 3600 * 24  # 保留 24 小时

        self.start_background_cleanup()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start_background_cleanup(self) -> None:
        """启动后台清理线程"""
        if self._running:
            return
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="request-tracker-cleanup"
        )
        self._cleanup_thread.start()

    def stop_background_cleanup(self) -> None:
        self._running = False
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5.0)

    def _cleanup_loop(self) -> None:
        while self._running:
            try:
                time.sleep(300)  # 每 5 分钟清理一次
                self._cleanup_expired()
            except Exception:
                logger.exception("请求追踪器清理异常")

    def _cleanup_expired(self) -> int:
        """清理过期记录"""
        now = time.time()
        expired_ids: List[str] = []

        with self._lock:
            for rid, record in self._requests.items():
                end = record.end_time or record.start_time
                if now - end > self._record_ttl:
                    expired_ids.append(rid)

            for rid in expired_ids:
                del self._requests[rid]

        if expired_ids:
            logger.debug("清理了 %d 条过期请求记录", len(expired_ids))
        return len(expired_ids)

    # ------------------------------------------------------------------
    # 请求追踪
    # ------------------------------------------------------------------

    def start_request(
        self,
        request_id: str,
        node_id: str,
        model_name: str,
        priority: str,
        burner_address: str,
    ) -> None:
        """
        记录请求开始

        Args:
            request_id: 唯一请求 ID
            node_id: 处理节点 ID
            model_name: 模型名称
            priority: 优先级 (basic/premium/priority)
            burner_address: 付费地址
        """
        record = RequestRecord(
            request_id=request_id,
            node_id=node_id,
            model_name=model_name,
            priority=priority,
            burner_address=burner_address,
            start_time=time.time(),
        )

        with self._lock:
            self._requests[request_id] = record

            # 初始化节点统计
            if node_id not in self._node_stats:
                self._node_stats[node_id] = {
                    "total": 0, "completed": 0, "failed": 0,
                    "total_tokens_in": 0, "total_tokens_out": 0,
                    "total_latency_ms": 0.0, "total_cost": 0.0,
                }
            self._node_stats[node_id]["total"] += 1

            # 初始化地址计费
            if burner_address not in self._address_billing:
                self._address_billing[burner_address] = {
                    "total_requests": 0, "completed": 0, "failed": 0,
                    "total_tokens_in": 0, "total_tokens_out": 0,
                    "total_cost": 0.0, "models": {},
                }
            self._address_billing[burner_address]["total_requests"] += 1

        logger.debug(
            "请求开始: id=%s, 节点=%s, 模型=%s, 优先级=%s, 地址=%s",
            request_id, node_id, model_name, priority, burner_address,
        )

    def complete_request(
        self,
        request_id: str,
        tokens_in: int,
        tokens_out: int,
        latency: float,
    ) -> None:
        """
        记录请求完成

        Args:
            request_id: 请求 ID
            tokens_in: 输入 token 数
            tokens_out: 输出 token 数
            latency: 请求延迟（毫秒）
        """
        with self._lock:
            record = self._requests.get(request_id)
            if record is None:
                logger.warning("完成不存在的请求: %s", request_id)
                return

            record.end_time = time.time()
            record.tokens_in = tokens_in
            record.tokens_out = tokens_out
            record.latency_ms = latency
            record.status = "completed"

            # 计算费用
            cost = self._calculate_cost(
                tokens_in, tokens_out, record.priority, record.model_name,
            )
            record.cost = cost

            # 更新节点统计
            stats = self._node_stats.get(record.node_id)
            if stats:
                stats["completed"] += 1
                stats["total_tokens_in"] += tokens_in
                stats["total_tokens_out"] += tokens_out
                stats["total_latency_ms"] += latency
                stats["total_cost"] += cost

            # 更新地址计费
            billing = self._address_billing.get(record.burner_address)
            if billing:
                billing["completed"] += 1
                billing["total_tokens_in"] += tokens_in
                billing["total_tokens_out"] += tokens_out
                billing["total_cost"] += cost
                models_billing = billing["models"]
                if record.model_name not in models_billing:
                    models_billing[record.model_name] = {
                        "requests": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
                    }
                models_billing[record.model_name]["requests"] += 1
                models_billing[record.model_name]["tokens_in"] += tokens_in
                models_billing[record.model_name]["tokens_out"] += tokens_out
                models_billing[record.model_name]["cost"] += cost

        logger.debug(
            "请求完成: id=%s, tokens_in=%d, tokens_out=%d, 延迟=%.1fms, 费用=%.6f",
            request_id, tokens_in, tokens_out, latency, cost,
        )

    def fail_request(self, request_id: str, reason: str) -> None:
        """
        记录请求失败

        Args:
            request_id: 请求 ID
            reason: 失败原因
        """
        with self._lock:
            record = self._requests.get(request_id)
            if record is None:
                logger.warning("标记不存在的请求为失败: %s", request_id)
                return

            record.end_time = time.time()
            record.status = "failed"
            record.error = reason

            # 更新节点统计
            stats = self._node_stats.get(record.node_id)
            if stats:
                stats["failed"] += 1

            # 更新地址计费
            billing = self._address_billing.get(record.burner_address)
            if billing:
                billing["failed"] += 1

        logger.debug("请求失败: id=%s, 原因=%s", request_id, reason)

    # ------------------------------------------------------------------
    # 计费
    # ------------------------------------------------------------------

    def _calculate_cost(
        self,
        tokens_in: int,
        tokens_out: int,
        priority: str,
        model_name: str,
    ) -> float:
        """
        计算请求费用

        Args:
            tokens_in: 输入 token 数
            tokens_out: 输出 token 数
            priority: 优先级
            model_name: 模型名称

        Returns:
            费用（内部单位）
        """
        cfg = self._config

        # 基础费用
        input_cost = (tokens_in / 1000.0) * cfg.cost_per_1k_input_tokens
        output_cost = (tokens_out / 1000.0) * cfg.cost_per_1k_output_tokens
        base_cost = input_cost + output_cost

        # 优先级倍率
        try:
            priority_enum = RequestPriority(priority)
        except ValueError:
            priority_enum = RequestPriority.BASIC

        multiplier_map = {
            RequestPriority.BASIC: 1.0,
            RequestPriority.PREMIUM: cfg.priority_multiplier,
            RequestPriority.PRIORITY: cfg.priority_max_multiplier,
        }
        multiplier = multiplier_map.get(priority_enum, 1.0)

        return base_cost * multiplier

    # ------------------------------------------------------------------
    # 统计查询
    # ------------------------------------------------------------------

    def get_node_stats(self, node_id: str, period: str = "hour") -> Dict[str, Any]:
        """
        获取节点请求统计

        Args:
            node_id: 节点 ID
            period: 统计周期 (hour/day/week/all)

        Returns:
            {
                "total_requests": 总请求数,
                "completed": 完成数,
                "failed": 失败数,
                "success_rate": 成功率,
                "avg_tokens_in": 平均输入 token,
                "avg_tokens_out": 平均输出 token,
                "avg_latency_ms": 平均延迟,
                "total_cost": 总费用,
                "period": 统计周期,
            }
        """
        with self._lock:
            stats = self._node_stats.get(node_id, {})

        total = stats.get("total", 0)
        completed = stats.get("completed", 0)
        failed = stats.get("failed", 0)
        success_rate = (completed / total * 100.0) if total > 0 else 0.0

        avg_tokens_in = stats.get("total_tokens_in", 0) / completed if completed > 0 else 0.0
        avg_tokens_out = stats.get("total_tokens_out", 0) / completed if completed > 0 else 0.0
        avg_latency = stats.get("total_latency_ms", 0.0) / completed if completed > 0 else 0.0

        return {
            "node_id": node_id,
            "total_requests": total,
            "completed": completed,
            "failed": failed,
            "success_rate": round(success_rate, 2),
            "avg_tokens_in": round(avg_tokens_in, 1),
            "avg_tokens_out": round(avg_tokens_out, 1),
            "avg_latency_ms": round(avg_latency, 2),
            "total_tokens_in": stats.get("total_tokens_in", 0),
            "total_tokens_out": stats.get("total_tokens_out", 0),
            "total_cost": round(stats.get("total_cost", 0.0), 6),
            "period": period,
        }

    def get_billing_summary(self, address: str) -> Dict[str, Any]:
        """
        获取地址的账单摘要

        Args:
            address: 付费地址

        Returns:
            {
                "address": 地址,
                "total_requests": 总请求数,
                "completed": 完成数,
                "failed": 失败数,
                "success_rate": 成功率,
                "total_tokens_in": 总输入 token,
                "total_tokens_out": 总输出 token,
                "total_cost": 总费用,
                "breakdown_by_model": {模型: {requests, tokens_in, tokens_out, cost}},
            }
        """
        with self._lock:
            billing = self._address_billing.get(address, {})

        total = billing.get("total_requests", 0)
        completed = billing.get("completed", 0)
        failed = billing.get("failed", 0)
        success_rate = (completed / total * 100.0) if total > 0 else 0.0

        # 获取模型明细的拷贝
        models_breakdown = dict(billing.get("models", {}))

        return {
            "address": address,
            "total_requests": total,
            "completed": completed,
            "failed": failed,
            "success_rate": round(success_rate, 2),
            "total_tokens_in": billing.get("total_tokens_in", 0),
            "total_tokens_out": billing.get("total_tokens_out", 0),
            "total_cost": round(billing.get("total_cost", 0.0), 6),
            "breakdown_by_model": models_breakdown,
        }

    def get_pending_requests(self) -> List[Dict[str, Any]]:
        """获取所有进行中的请求"""
        with self._lock:
            pending = [
                {
                    "request_id": r.request_id,
                    "node_id": r.node_id,
                    "model_name": r.model_name,
                    "priority": r.priority,
                    "elapsed_ms": round(r.duration_ms, 2),
                }
                for r in self._requests.values()
                if r.status == "pending"
            ]
        return pending

    @property
    def total_tracked(self) -> int:
        with self._lock:
            return len(self._requests)

    def shutdown(self) -> None:
        self.stop_background_cleanup()
        logger.info("请求追踪器已关闭")


# ---------------------------------------------------------------------------
# 模块级工厂与便捷函数
# ---------------------------------------------------------------------------

def create_routing_system(
    config: Optional[RoutingConfig] = None,
) -> Tuple[NodeRegistry, LatencyProbe, OptimalRouter, RequestTracker]:
    """
    创建完整的路由系统

    Args:
        config: 路由配置，默认使用标准配置

    Returns:
        (NodeRegistry, LatencyProbe, OptimalRouter, RequestTracker) 元组
    """
    cfg = config or RoutingConfig()
    registry = NodeRegistry(config=cfg)
    probe = LatencyProbe(registry=registry, config=cfg)
    router = OptimalRouter(registry=registry, probe=probe, config=cfg)
    tracker = RequestTracker(config=cfg)
    logger.info("路由系统初始化完成（策略=%s）", cfg.strategy.value)
    return registry, probe, router, tracker
