#!/usr/bin/env python3
"""
AICoin 挖矿引擎 - Mining Engine
=================================

AICoin 去中心化AI算力挖矿网络的核心挖矿引擎。
节点通过贡献GPU/CPU算力运行AI推理模型来赚取AICoin，
奖励按算力占比分配，每年减半（类似比特币）。

核心组件:
    - ComputeMeter:     算力计量器，实时追踪节点的计算贡献
    - MiningEngine:     挖矿引擎，管理算力奖励的计算和分发
    - RewardDistributor: 奖励分配器，将API调用收入分配给参与节点

版本: 1.0.0
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import struct
import time
import threading
import uuid
from collections import deque, OrderedDict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "ComputeMeter",
    "MiningEngine",
    "RewardDistributor",
    "MINING_CONSTANTS",
    "InferenceRecord",
    "MiningConfig",
    "RewardDistributionEvent",
    "ComputeProof",
    "MiningState",
]

# ==================== 日志配置 ====================

logger = logging.getLogger("aicoin.mining_engine")


# ==================== 常量定义 ====================

@dataclass(frozen=True)
class MiningConstants:
    """挖矿引擎常量定义"""
    # --- 比特币经济模型 ---
    INITIAL_BLOCK_REWARD: int = 50          # 初始区块奖励 (AIC)
    HALVING_INTERVAL: int = 210000           # 减半周期（区块数，约等于1年）
    MAX_SUPPLY: int = 21_000_000             # AICoin 最大供应量 (AIC)
    TARGET_BLOCK_TIME: float = 10.0          # 目标出块时间（秒，用于模拟）

    # --- 算力计量 ---
    COMPUTE_SCORE_MAX: float = 10000.0       # 算力分数上限
    ROLLING_WINDOW_24H: int = 86400          # 24小时窗口（秒）
    ROLLING_WINDOW_7D: int = 604800          # 7天窗口（秒）
    MAX_INFERENCE_RECORDS: int = 100_000     # 内存中最大推理记录数

    # --- 算力分数权重 ---
    WEIGHT_GPU_UTILIZATION: float = 0.30     # GPU利用率权重
    WEIGHT_THROUGHPUT: float = 0.30          # 推理吞吐权重
    WEIGHT_UPTIME: float = 0.20             # 在线时长权重
    WEIGHT_COMPLETION_RATE: float = 0.20    # 任务完成率权重

    # --- 收入分配比例 ---
    NODE_REVENUE_SHARE: float = 0.80         # 节点分享比例 (80%)
    TREASURY_SHARE: float = 0.20            # 金库比例 (20%)

    # --- 挖矿线程 ---
    MINING_LOOP_INTERVAL: float = 30.0       # 挖矿主循环间隔（秒）
    REWARD_CLAIM_INTERVAL: float = 60.0     # 奖励领取间隔（秒）
    SUBMISSION_TIMEOUT: float = 15.0        # 算力证明提交超时（秒）

    # --- 安全阈值 ---
    MIN_GPU_USAGE_FOR_REWARD: float = 0.01  # 最低GPU使用率（低于此不计分）
    MAX_PENDING_REWARDS: int = 10_000       # 最大待领取奖励条目数

    # --- 模型算力系数 (tokens/秒, 基准参考) ---
    MODEL_POWER_FACTORS: Dict[str, float] = field(default_factory=lambda: {
        "qwen2.5-0.5b": 1.0,
        "qwen2.5-1.5b": 2.5,
        "qwen2.5-3b": 5.0,
        "qwen2.5-7b": 12.0,
        "qwen2.5-14b": 25.0,
        "qwen2.5-32b": 55.0,
        "qwen2.5-72b": 120.0,
        "llama-3-8b": 14.0,
        "llama-3-70b": 130.0,
        "default": 5.0,
    })


# 全局常量实例
MINING_CONSTANTS = MiningConstants()


# ==================== 数据结构 ====================

class MiningState(Enum):
    """挖矿状态枚举"""
    IDLE = "idle"
    MINING = "mining"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class InferenceRecord:
    """推理记录 - 单次推理任务的详细信息"""
    task_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    inference_time: float = 0.0          # 秒
    model_name: str = ""
    gpu_used: float = 0.0                # GPU显存使用量 (GB)
    timestamp: float = field(default_factory=time.time)
    success: bool = True
    error_msg: str = ""

    @property
    def total_tokens(self) -> int:
        """总token数"""
        return self.tokens_in + self.tokens_out

    @property
    def tokens_per_second(self) -> float:
        """每秒处理token数"""
        if self.inference_time > 0:
            return self.total_tokens / self.inference_time
        return 0.0


@dataclass
class ComputeProof:
    """算力证明 - 提交到区块链的证据"""
    node_id: str = ""
    timestamp: float = 0.0
    compute_power: float = 0.0           # 算力分数 (0-10000)
    tasks_24h: int = 0
    tokens_processed: int = 0
    gpu_hours_24h: float = 0.0
    signature_hash: str = ""
    block_height: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

    def serialize(self) -> str:
        """序列化为字符串（用于签名）"""
        payload = json.dumps({
            "node_id": self.node_id,
            "timestamp": self.timestamp,
            "compute_power": self.compute_power,
            "tasks_24h": self.tasks_24h,
            "tokens_processed": self.tokens_processed,
            "gpu_hours_24h": self.gpu_hours_24h,
            "block_height": self.block_height,
        }, sort_keys=True, separators=(",", ":"))
        return payload

    def compute_hash(self) -> str:
        """计算内容哈希"""
        return hashlib.sha256(self.serialize().encode("utf-8")).hexdigest()


@dataclass
class MiningConfig:
    """挖矿配置"""
    node_id: str = ""
    mining_enabled: bool = True
    auto_claim_rewards: bool = True
    proof_submission_interval: float = MINING_CONSTANTS.MINING_LOOP_INTERVAL
    max_retries: int = 3
    retry_delay: float = 5.0

    def __post_init__(self):
        if not self.node_id:
            self.node_id = str(uuid.uuid4())


@dataclass
class RewardDistributionEvent:
    """奖励分配事件记录"""
    event_id: str = ""
    block_height: int = 0
    timestamp: float = 0.0
    total_revenue: int = 0               # 总收入 (AIC, 最小单位)
    node_distribution: int = 0           # 分配给节点 (AIC)
    treasury: int = 0                    # 进入金库 (AIC)
    participants: int = 0                # 参与节点数
    distribution_details: Dict[str, int] = field(default_factory=dict)


# ==================== ComputeMeter: 算力计量器 ====================

class ComputeMeter:
    """
    算力计量器 - 实时追踪节点的计算贡献

    跟踪GPU小时数、处理token数、完成任务数、推理时间等指标。
    维护三个滑动窗口：最近24小时、最近7天、全时间。
    """

    def __init__(self, node_id: str) -> None:
        """
        初始化算力计量器

        Args:
            node_id: 节点唯一标识
        """
        self.node_id: str = node_id
        self._lock: threading.RLock = threading.RLock()

        # --- 推理记录 (滚动窗口) ---
        self._records: deque[InferenceRecord] = deque(
            maxlen=MINING_CONSTANTS.MAX_INFERENCE_RECORDS
        )

        # --- 累计统计 (全时间) ---
        self._total_tasks: int = 0
        self._total_success: int = 0
        self._total_failed: int = 0
        self._total_tokens_in: int = 0
        self._total_tokens_out: int = 0
        self._total_inference_time: float = 0.0
        self._total_gpu_hours: float = 0.0

        # --- 在线追踪 ---
        self._start_time: float = time.time()
        self._last_activity: float = time.time()

        # --- 模型使用统计 ---
        self._model_usage: Dict[str, int] = {}

        logger.info(f"[ComputeMeter] 初始化完成 node_id={node_id[:12]}")

    # ----------------------------------------------------------------
    #  核心记录接口
    # ----------------------------------------------------------------

    def record_inference(
        self,
        tokens_in: int,
        tokens_out: int,
        inference_time: float,
        model_name: str,
        gpu_used: float,
        success: bool = True,
        error_msg: str = "",
    ) -> str:
        """
        记录一次推理任务

        Args:
            tokens_in:       输入token数
            tokens_out:      输出token数
            inference_time:  推理耗时（秒）
            model_name:      模型名称
            gpu_used:        GPU显存使用量（GB）
            success:         是否成功
            error_msg:       失败时的错误信息

        Returns:
            task_id: 推理任务唯一ID
        """
        if tokens_in < 0 or tokens_out < 0:
            logger.warning("[ComputeMeter] token数量为负值，已修正为0")
            tokens_in = max(0, tokens_in)
            tokens_out = max(0, tokens_out)

        if inference_time <= 0 and success:
            logger.warning("[ComputeMeter] 推理时间为0或负值，默认设为0.001s")
            inference_time = 0.001

        task_id = str(uuid.uuid4())
        now = time.time()

        record = InferenceRecord(
            task_id=task_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            inference_time=inference_time,
            model_name=model_name.lower().strip(),
            gpu_used=max(0.0, gpu_used),
            timestamp=now,
            success=success,
            error_msg=error_msg,
        )

        with self._lock:
            self._records.append(record)

            self._total_tasks += 1
            if success:
                self._total_success += 1
                self._total_tokens_in += tokens_in
                self._total_tokens_out += tokens_out
                self._total_inference_time += inference_time
                # GPU小时数 = GPU使用量(GB) × 时间(小时)
                self._total_gpu_hours += (gpu_used * inference_time) / 3600.0
            else:
                self._total_failed += 1

            # 模型使用统计
            key = record.model_name or "unknown"
            self._model_usage[key] = self._model_usage.get(key, 0) + 1

            self._last_activity = now

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"[ComputeMeter] 记录推理 task={task_id[:8]} "
                f"tokens={tokens_in}+{tokens_out} time={inference_time:.3f}s "
                f"model={model_name} gpu={gpu_used:.1f}GB success={success}"
            )

        return task_id

    # ----------------------------------------------------------------
    #  算力分数计算
    # ----------------------------------------------------------------

    def get_compute_score(self) -> float:
        """
        计算综合算力分数 (0-10000)

        基于四个维度加权:
            - GPU利用率 (30%): 基于最近24小时的GPU使用量和推理时间
            - 推理吞吐 (30%): 基于最近24小时每秒处理token数
            - 在线时长 (20%): 基于节点运行时长，上限30天
            - 任务完成率 (20%): 基于最近24小时的成功率

        Returns:
            算力分数，范围 [0, 10000]
        """
        with self._lock:
            now = time.time()
            window_24h = MINING_CONSTANTS.ROLLING_WINDOW_24H

            # 收集最近24小时的记录
            recent_records = [
                r for r in self._records
                if now - r.timestamp <= window_24h and r.success
            ]

            # ---- 1. GPU利用率得分 (0-100) ----
            gpu_score = self._calc_gpu_score(recent_records)

            # ---- 2. 推理吞吐得分 (0-100) ----
            throughput_score = self._calc_throughput_score(recent_records)

            # ---- 3. 在线时长得分 (0-100) ----
            uptime_hours = (now - self._start_time) / 3600.0
            # 使用对数缩放，30天≈100分
            uptime_score = min(100.0, 100.0 * math.log1p(uptime_hours) / math.log1p(720.0))

            # ---- 4. 任务完成率得分 (0-100) ----
            completion_score = self._calc_completion_score(recent_records, now, window_24h)

            # ---- 加权汇总 ----
            W = MINING_CONSTANTS
            raw_score = (
                W.WEIGHT_GPU_UTILIZATION * gpu_score
                + W.WEIGHT_THROUGHPUT * throughput_score
                + W.WEIGHT_UPTIME * uptime_score
                + W.WEIGHT_COMPLETION_RATE * completion_score
            )

            score = min(W.COMPUTE_SCORE_MAX, max(0.0, raw_score * 100.0))

        return round(score, 4)

    def _calc_gpu_score(self, records: List[InferenceRecord]) -> float:
        """计算GPU利用率得分 (0-100)

        基于假设的GPU容量，评估实际利用率。
        高GPU使用量 × 高推理时长 → 高分。
        """
        if not records:
            return 0.0

        total_gpu_time = sum(r.gpu_used * r.inference_time for r in records)

        # 假设节点的基准GPU容量 (GB·秒/24h)
        # 一张RTX 4090 约24GB显存, 24h = 86400s, 基准 = 24 * 86400 / 100 = 20736
        benchmark_gpu_time = 20000.0

        utilization = min(1.0, total_gpu_time / benchmark_gpu_time)

        # 使用sigmoid映射，让中等利用率也能获得合理分数
        score = 100.0 * (2.0 / (1.0 + math.exp(-6.0 * (utilization - 0.3))) - 1.0)
        return max(0.0, min(100.0, score))

    def _calc_throughput_score(self, records: List[InferenceRecord]) -> float:
        """计算推理吞吐得分 (0-100)

        基于平均每秒处理token数，参考基准值做缩放。
        大模型处理token更慢，但算力贡献更高，因此用模型系数校正。
        """
        if not records:
            return 0.0

        model_factors = MINING_CONSTANTS.MODEL_POWER_FACTORS
        weighted_tokens_per_sec = 0.0
        total_weight = 0.0

        for r in records:
            factor = model_factors.get(r.model_name, model_factors["default"])
            weighted_tps = r.tokens_per_second * factor
            weight = r.inference_time  # 按推理时间加权
            weighted_tokens_per_sec += weighted_tps * weight
            total_weight += weight

        if total_weight <= 0:
            return 0.0

        avg_weighted_tps = weighted_tokens_per_sec / total_weight

        # 基准: 加权1000 tokens/s → 满分
        benchmark_tps = 1000.0
        score = min(100.0, (avg_weighted_tps / benchmark_tps) * 100.0)
        return max(0.0, score)

    def _calc_completion_score(
        self,
        recent_success: List[InferenceRecord],
        now: float,
        window: float,
    ) -> float:
        """计算任务完成率得分 (0-100)"""
        total_recent = sum(
            1 for r in self._records
            if now - r.timestamp <= window
        )

        if total_recent == 0:
            return 50.0  # 无数据时给中间分

        success_count = len(recent_success)
        rate = success_count / total_recent

        # 完成率 < 50% 时快速下降
        if rate >= 0.95:
            return 100.0
        elif rate >= 0.80:
            return 80.0 + (rate - 0.80) / 0.15 * 20.0
        elif rate >= 0.50:
            return 40.0 + (rate - 0.50) / 0.30 * 40.0
        else:
            return max(0.0, rate / 0.50 * 40.0)

    # ----------------------------------------------------------------
    #  统计接口
    # ----------------------------------------------------------------

    def get_hourly_stats(self) -> Dict[str, Any]:
        """获取最近24小时统计"""
        with self._lock:
            return self._build_stats(MINING_CONSTANTS.ROLLING_WINDOW_24H, "24小时")

    def get_daily_stats(self) -> Dict[str, Any]:
        """获取最近7天统计"""
        with self._lock:
            return self._build_stats(MINING_CONSTANTS.ROLLING_WINDOW_7D, "7天")

    def get_all_time_stats(self) -> Dict[str, Any]:
        """获取全时间统计"""
        with self._lock:
            return self._build_stats(float("inf"), "全时间")

    def _build_stats(self, window_seconds: float, label: str) -> Dict[str, Any]:
        """构建统计报告"""
        now = time.time()

        if window_seconds == float("inf"):
            records = list(self._records)
            is_all_time = True
        else:
            cutoff = now - window_seconds
            records = [r for r in self._records if r.timestamp >= cutoff]
            is_all_time = False

        success_records = [r for r in records if r.success]

        total_tasks = len(records)
        success_tasks = len(success_records)
        failed_tasks = total_tasks - success_tasks

        total_tokens_in = sum(r.tokens_in for r in success_records)
        total_tokens_out = sum(r.tokens_out for r in success_records)
        total_inference_time = sum(r.inference_time for r in success_records)
        total_gpu_hours = sum(
            (r.gpu_used * r.inference_time) / 3600.0
            for r in success_records
        )

        if is_all_time:
            uptime_seconds = now - self._start_time
        else:
            uptime_seconds = window_seconds

        # 推理吞吐
        avg_tps = 0.0
        if total_inference_time > 0:
            avg_tps = (total_tokens_in + total_tokens_out) / total_inference_time

        # 完成率
        completion_rate = (success_tasks / total_tasks * 100.0) if total_tasks > 0 else 0.0

        # 模型使用频率
        model_counts: Dict[str, int] = {}
        for r in success_records:
            model_counts[r.model_name] = model_counts.get(r.model_name, 0) + 1

        # 排序取前5
        top_models = sorted(
            model_counts.items(), key=lambda x: x[1], reverse=True
        )[:5]

        # 时间窗口内的峰值信息
        peak_tps = 0.0
        for r in success_records:
            if r.tokens_per_second > peak_tps:
                peak_tps = r.tokens_per_second

        return {
            "period": label,
            "total_tasks": total_tasks,
            "success_tasks": success_tasks,
            "failed_tasks": failed_tasks,
            "completion_rate": round(completion_rate, 2),
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "total_tokens": total_tokens_in + total_tokens_out,
            "total_inference_time_s": round(total_inference_time, 3),
            "total_gpu_hours": round(total_gpu_hours, 6),
            "avg_tokens_per_second": round(avg_tps, 2),
            "peak_tokens_per_second": round(peak_tps, 2),
            "uptime_seconds": round(uptime_seconds, 1),
            "top_models": [
                {"model": m, "count": c} for m, c in top_models
            ],
        }

    # ----------------------------------------------------------------
    #  算力证明生成
    # ----------------------------------------------------------------

    def generate_proof(self, block_height: int = 0) -> ComputeProof:
        """
        生成算力证明 (用于提交到智能合约)

        包含: node_id, timestamp, compute_power, tasks_24h,
              tokens_processed, gpu_hours, signature_hash

        Args:
            block_height: 当前区块高度

        Returns:
            ComputeProof 算力证明对象
        """
        with self._lock:
            now = time.time()
            cutoff = now - MINING_CONSTANTS.ROLLING_WINDOW_24H
            recent = [r for r in self._records if r.timestamp >= cutoff]

            success_recent = [r for r in recent if r.success]

            compute_power = self.get_compute_score()
            tasks_24h = len(success_recent)
            tokens_processed = sum(r.total_tokens for r in success_recent)
            gpu_hours_24h = sum(
                (r.gpu_used * r.inference_time) / 3600.0
                for r in success_recent
            )

        proof = ComputeProof(
            node_id=self.node_id,
            timestamp=now,
            compute_power=compute_power,
            tasks_24h=tasks_24h,
            tokens_processed=tokens_processed,
            gpu_hours_24h=round(gpu_hours_24h, 6),
            block_height=block_height,
            signature_hash="",  # 由区块链层签名
        )

        # 计算内容哈希作为预签名
        proof.signature_hash = proof.compute_hash()

        logger.info(
            f"[ComputeMeter] 生成算力证明 power={compute_power:.2f} "
            f"tasks_24h={tasks_24h} tokens={tokens_processed} "
            f"gpu_hours={gpu_hours_24h:.4f}"
        )

        return proof

    # ----------------------------------------------------------------
    #  辅助方法
    # ----------------------------------------------------------------

    def get_uptime_seconds(self) -> float:
        """获取节点在线时长（秒）"""
        return time.time() - self._start_time

    def get_last_activity(self) -> float:
        """获取最后活动时间戳"""
        with self._lock:
            return self._last_activity

    def get_record_count(self) -> int:
        """获取当前记录总数"""
        with self._lock:
            return len(self._records)

    def reset(self) -> None:
        """重置所有统计数据（谨慎使用）"""
        with self._lock:
            self._records.clear()
            self._total_tasks = 0
            self._total_success = 0
            self._total_failed = 0
            self._total_tokens_in = 0
            self._total_tokens_out = 0
            self._total_inference_time = 0.0
            self._total_gpu_hours = 0.0
            self._start_time = time.time()
            self._last_activity = time.time()
            self._model_usage.clear()
            logger.warning(f"[ComputeMeter] 统计已重置 node_id={self.node_id[:12]}")


# ==================== MiningEngine: 挖矿引擎 ====================

class MiningEngine:
    """
    AICoin挖矿引擎 - 管理算力奖励的计算和分发

    核心功能:
        - 后台挖矿线程：持续提交算力证明并领取奖励
        - 奖励计算：按算力占比分配区块奖励
        - 减半机制：每年减半，类似比特币
        - 网络统计：全网算力、活跃节点数等

    线程安全: 所有公共方法均为线程安全。
    """

    def __init__(
        self,
        blockchain_manager: Any,
        node_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        初始化挖矿引擎

        Args:
            blockchain_manager: 区块链管理器实例（需支持特定接口方法）
            node_id: 本节点ID
            config: 配置字典，可选覆盖默认配置
        """
        self._bc = blockchain_manager
        self._node_id: str = node_id
        self._state: MiningState = MiningState.IDLE
        self._lock: threading.RLock = threading.RLock()

        # --- 配置 ---
        raw_config = config or {}
        self._config = MiningConfig(**{
            k: v for k, v in raw_config.items()
            if k in MiningConfig.__dataclass_fields__
        })
        self._config.node_id = node_id

        # --- 算力计量 ---
        self._meter = ComputeMeter(node_id=node_id)

        # --- 奖励追踪 ---
        self._pending_rewards: List[Tuple[int, float]] = []  # [(amount, timestamp)]
        self._total_mined: int = 0
        self._last_claim_time: float = 0.0
        self._last_proof_time: float = 0.0
        self._proof_history: deque[ComputeProof] = deque(maxlen=1000)

        # --- 挖矿线程 ---
        self._mining_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # --- 网络缓存（模拟/真实数据） ---
        self._cached_total_power: float = 0.0
        self._cached_active_nodes: int = 0
        self._cached_block_height: int = 0
        self._cache_lock = threading.Lock()
        self._cache_expiry: float = 0.0
        self._cache_ttl: float = 60.0  # 缓存有效期（秒）

        # --- 难度调整 ---
        self._current_difficulty: float = 1.0

        logger.info(f"[MiningEngine] 初始化完成 node_id={node_id[:12]}")

    # ----------------------------------------------------------------
    #  属性
    # ----------------------------------------------------------------

    @property
    def meter(self) -> ComputeMeter:
        """获取算力计量器"""
        return self._meter

    @property
    def state(self) -> MiningState:
        """获取当前挖矿状态"""
        with self._lock:
            return self._state

    @property
    def node_id(self) -> str:
        """获取节点ID"""
        return self._node_id

    @property
    def is_mining(self) -> bool:
        """是否正在挖矿"""
        return self._state == MiningState.MINING

    # ----------------------------------------------------------------
    #  核心挖矿控制
    # ----------------------------------------------------------------

    def start_mining(self) -> None:
        """
        启动挖矿进程 (后台线程)

        持续提交算力证明并领取奖励。
        如果已在挖矿，则忽略。
        """
        with self._lock:
            if self._state == MiningState.MINING:
                logger.warning("[MiningEngine] 挖矿已在运行中")
                return

            if not self._config.mining_enabled:
                logger.warning("[MiningEngine] 挖矿已禁用 (config.mining_enabled=False)")
                return

            self._stop_event.clear()
            self._mining_thread = threading.Thread(
                target=self._mining_loop,
                name=f"mining-{self._node_id[:8]}",
                daemon=True,
            )
            self._mining_thread.start()
            self._state = MiningState.MINING

        logger.info("[MiningEngine] 挖矿已启动")

    def stop_mining(self) -> None:
        """
        停止挖矿

        发出停止信号并等待线程结束。
        """
        with self._lock:
            if self._state != MiningState.MINING:
                logger.warning("[MiningEngine] 挖矿未在运行")
                return

            self._stop_event.set()

        logger.info("[MiningEngine] 正在停止挖矿...")

        if self._mining_thread is not None:
            self._mining_thread.join(timeout=5.0)
            if self._mining_thread.is_alive():
                logger.warning("[MiningEngine] 挖矿线程未能正常结束")
            self._mining_thread = None

        with self._lock:
            self._state = MiningState.STOPPED

        logger.info("[MiningEngine] 挖矿已停止")

    def pause_mining(self) -> None:
        """暂停挖矿"""
        with self._lock:
            if self._state == MiningState.MINING:
                self._state = MiningState.PAUSED
                logger.info("[MiningEngine] 挖矿已暂停")
            else:
                logger.warning(f"[MiningEngine] 无法从状态 {self._state.value} 暂停")

    def resume_mining(self) -> None:
        """恢复挖矿"""
        with self._lock:
            if self._state == MiningState.PAUSED:
                self._state = MiningState.MINING
                logger.info("[MiningEngine] 挖矿已恢复")
            elif self._state == MiningState.STOPPED:
                # 从停止状态恢复 -> 重新启动
                self._lock.release()
                try:
                    self.start_mining()
                finally:
                    self._lock.acquire()
            else:
                logger.warning(f"[MiningEngine] 无法从状态 {self._state.value} 恢复")

    def _mining_loop(self) -> None:
        """
        挖矿主循环（后台线程）

        周期性执行:
        1. 刷新网络缓存（总算力、活跃节点数等）
        2. 提交算力证明
        3. 领取待处理奖励
        """
        logger.info("[MiningEngine] 挖矿主循环启动")

        while not self._stop_event.is_set():
            try:
                # 检查暂停状态
                with self._lock:
                    if self._state == MiningState.PAUSED:
                        self._stop_event.wait(timeout=5.0)
                        continue

                # 1. 刷新网络缓存
                self._refresh_network_cache()

                # 2. 提交算力证明
                if time.time() - self._last_proof_time >= self._config.proof_submission_interval:
                    self.submit_compute_proof()
                    self._last_proof_time = time.time()

                # 3. 领取奖励
                if (
                    self._config.auto_claim_rewards
                    and self._pending_rewards
                    and time.time() - self._last_claim_time >= MINING_CONSTANTS.REWARD_CLAIM_INTERVAL
                ):
                    claimed = self.claim_reward()
                    if claimed > 0:
                        logger.info(f"[MiningEngine] 自动领取奖励: {claimed} AIC")

            except Exception as e:
                logger.error(f"[MiningEngine] 挖矿循环异常: {e}", exc_info=True)
                with self._lock:
                    self._state = MiningState.ERROR

                # 等待后重试
                self._stop_event.wait(timeout=self._config.retry_delay)

                with self._lock:
                    if not self._stop_event.is_set():
                        self._state = MiningState.MINING

            # 等待下一个周期
            self._stop_event.wait(timeout=5.0)

        logger.info("[MiningEngine] 挖矿主循环结束")

    # ----------------------------------------------------------------
    #  算力证明提交
    # ----------------------------------------------------------------

    def submit_compute_proof(self) -> bool:
        """
        提交算力证明到区块链

        将本节点的算力证明提交到区块链网络，由其他节点验证。
        支持重试机制。

        Returns:
            True 提交成功, False 提交失败
        """
        proof = self._meter.generate_proof(block_height=self._get_block_height())

        # 算力为0时不提交（没有贡献）
        if proof.compute_power <= 0:
            logger.debug("[MiningEngine] 算力为0，跳过提交")
            return False

        for attempt in range(1, self._config.max_retries + 1):
            try:
                success = self._bc_submit_proof(proof)

                if success:
                    with self._lock:
                        self._proof_history.append(proof)
                    logger.info(
                        f"[MiningEngine] 算力证明已提交 power={proof.compute_power:.2f} "
                        f"tasks={proof.tasks_24h} hash={proof.signature_hash[:16]}"
                    )
                    return True
                else:
                    logger.warning(
                        f"[MiningEngine] 算力证明提交失败 (尝试 {attempt}/{self._config.max_retries})"
                    )

            except Exception as e:
                logger.error(
                    f"[MiningEngine] 算力证明提交异常 (尝试 {attempt}): {e}"
                )

            if attempt < self._config.max_retries:
                time.sleep(self._config.retry_delay * attempt)

        logger.error("[MiningEngine] 算力证明提交最终失败")
        return False

    def _bc_submit_proof(self, proof: ComputeProof) -> bool:
        """
        调用区块链管理器提交算力证明

        Args:
            proof: 算力证明

        Returns:
            是否成功
        """
        try:
            if self._bc is None:
                logger.warning("[MiningEngine] blockchain_manager 未配置，模拟提交成功")
                return True

            # 尝试调用区块链接口
            if hasattr(self._bc, "submit_compute_proof"):
                return bool(self._bc.submit_compute_proof(proof.to_dict()))
            elif hasattr(self._bc, "submit_proof"):
                return bool(self._bc.submit_proof(proof.to_dict()))
            else:
                logger.warning(
                    "[MiningEngine] blockchain_manager 缺少 submit_compute_proof 方法，"
                    "模拟提交成功"
                )
                return True

        except Exception as e:
            logger.error(f"[MiningEngine] 区块链提交异常: {e}")
            return False

    # ----------------------------------------------------------------
    #  奖励管理
    # ----------------------------------------------------------------

    def add_pending_reward(self, amount: int) -> None:
        """
        添加待领取奖励

        Args:
            amount: 奖励金额 (AIC, 最小单位)
        """
        if amount <= 0:
            return

        with self._lock:
            if len(self._pending_rewards) >= MINING_CONSTANTS.MAX_PENDING_REWARDS:
                logger.warning("[MiningEngine] 待领取奖励列表已满")
                return

            self._pending_rewards.append((amount, time.time()))

    def claim_reward(self) -> int:
        """
        领取待处理的挖矿奖励

        将所有待领取奖励一次性领取并写入区块链。

        Returns:
            本次领取的总奖励 (AIC)
        """
        with self._lock:
            if not self._pending_rewards:
                return 0

            rewards_to_claim = list(self._pending_rewards)
            self._pending_rewards.clear()

        total = sum(amount for amount, _ in rewards_to_claim)

        try:
            if self._bc is not None and hasattr(self._bc, "claim_mining_reward"):
                bc_result = self._bc.claim_mining_reward(self._node_id, total)
                if not bc_result:
                    # 回滚
                    with self._lock:
                        self._pending_rewards.extend(rewards_to_claim)
                    logger.warning("[MiningEngine] 奖励领取失败，已回滚")
                    return 0

            self._total_mined += total
            self._last_claim_time = time.time()

            logger.info(
                f"[MiningEngine] 奖励领取成功: {total} AIC "
                f"(累计: {self._total_mined} AIC)"
            )
            return total

        except Exception as e:
            # 回滚
            with self._lock:
                self._pending_rewards.extend(rewards_to_claim)
            logger.error(f"[MiningEngine] 奖励领取异常: {e}")
            return 0

    def get_pending_reward(self) -> int:
        """
        查询待领取奖励

        Returns:
            待领取奖励总额 (AIC)
        """
        with self._lock:
            return sum(amount for amount, _ in self._pending_rewards)

    def get_total_mined(self) -> int:
        """获取累计挖矿奖励"""
        with self._lock:
            return self._total_mined

    # ----------------------------------------------------------------
    #  奖励计算
    # ----------------------------------------------------------------

    def calculate_reward(
        self,
        node_power: float,
        total_power: float,
        block_reward: int,
    ) -> int:
        """
        计算节点应得奖励

        reward = block_reward * (node_power / total_power)

        Args:
            node_power:    本节点的算力分数
            total_power:   全网总算力分数
            block_reward:  当前区块奖励 (AIC)

        Returns:
            节点应得奖励 (AIC)，向下取整，最小0
        """
        if total_power <= 0:
            logger.warning("[MiningEngine] 全网算力为0，无法计算奖励")
            return 0

        if node_power <= 0:
            return 0

        if block_reward <= 0:
            return 0

        ratio = node_power / total_power
        reward = int(block_reward * ratio)

        # 检查是否会超过剩余供应量
        C = MINING_CONSTANTS
        current_epoch = self.get_halving_epoch()
        max_possible = C.MAX_SUPPLY // (2 ** current_epoch)

        if reward > max_possible:
            reward = max_possible

        return reward

    def get_current_block_reward(self) -> int:
        """
        获取当前区块奖励 (考虑减半)

        reward = INITIAL_BLOCK_REWARD / (2 ^ halving_epoch)

        Returns:
            当前区块奖励 (AIC)
        """
        epoch = self.get_halving_epoch()
        C = MINING_CONSTANTS

        # 检查是否已超过最大供应量
        max_epoch = int(math.log2(C.MAX_SUPPLY / (C.INITIAL_BLOCK_REWARD * C.HALVING_INTERVAL * 0.01)))
        if epoch > max_epoch + 1:
            # 奖励已趋近于0
            return 0

        reward = C.INITIAL_BLOCK_REWARD // (2 ** epoch)

        if reward <= 0:
            return 0

        return reward

    def get_halving_epoch(self) -> int:
        """
        获取当前减半周期编号

        Returns:
            减半周期编号 (从0开始)
        """
        block_height = self._get_block_height()
        C = MINING_CONSTANTS
        return block_height // C.HALVING_INTERVAL

    def get_halving_countdown(self) -> Dict[str, Any]:
        """
        获取距离下次减半的信息

        Returns:
            {
                "current_epoch": int,        # 当前减半周期
                "blocks_remaining": int,     # 距下次减半的区块数
                "estimated_time": str,       # 预计剩余时间 (ISO格式)
                "estimated_seconds": float,  # 预计剩余秒数
                "current_reward": int,       # 当前区块奖励
                "next_reward": int,          # 下次减半后区块奖励
                "progress_percent": float,   # 当前周期进度百分比
            }
        """
        C = MINING_CONSTANTS
        block_height = self._get_block_height()
        current_epoch = block_height // C.HALVING_INTERVAL
        blocks_in_epoch = block_height % C.HALVING_INTERVAL
        blocks_remaining = C.HALVING_INTERVAL - blocks_in_epoch

        current_reward = C.INITIAL_BLOCK_REWARD // (2 ** current_epoch)
        next_reward = C.INITIAL_BLOCK_REWARD // (2 ** (current_epoch + 1))
        if next_reward < 0:
            next_reward = 0

        # 预估时间
        estimated_seconds = blocks_remaining * C.TARGET_BLOCK_TIME
        estimated_dt = datetime.now(tz=timezone.utc) + timedelta(seconds=estimated_seconds)

        progress = (blocks_in_epoch / C.HALVING_INTERVAL) * 100.0

        return {
            "current_epoch": current_epoch,
            "blocks_remaining": blocks_remaining,
            "estimated_time": estimated_dt.isoformat(),
            "estimated_seconds": estimated_seconds,
            "current_reward": current_reward,
            "next_reward": next_reward,
            "progress_percent": round(progress, 2),
        }

    # ----------------------------------------------------------------
    #  网络统计
    # ----------------------------------------------------------------

    def get_network_stats(self) -> Dict[str, Any]:
        """
        获取网络挖矿统计

        Returns:
            {
                "total_power": float,         # 全网总算力
                "active_nodes": int,          # 活跃节点数
                "total_mined": int,           # 本节点累计挖矿
                "supply_remaining": int,      # 剩余供应量
                "current_difficulty": float,  # 当前难度
                "current_block_reward": int,  # 当前区块奖励
                "next_halving": dict,         # 减半倒计时
                "node_power": float,          # 本节点算力
                "node_rank": float,           # 本节点算力排名
                "estimated_daily_reward": float,  # 预估日收益
            }
        """
        C = MINING_CONSTANTS
        total_power, active_nodes = self._get_network_power()

        # 本节点算力
        node_power = self._meter.get_compute_score()

        # 算力排名（占全网比例）
        power_ratio = (node_power / total_power * 100.0) if total_power > 0 else 0.0

        # 剩余供应量
        current_epoch = self.get_halving_epoch()
        blocks_mined_in_epoch = self._get_block_height() % C.HALVING_INTERVAL
        reward_per_epoch = C.INITIAL_BLOCK_REWARD // (2 ** current_epoch)
        total_mined_estimate = 0
        for e in range(current_epoch):
            total_mined_estimate += (C.INITIAL_BLOCK_REWARD // (2 ** e)) * C.HALVING_INTERVAL
        total_mined_estimate += reward_per_epoch * blocks_mined_in_epoch
        supply_remaining = max(0, C.MAX_SUPPLY - total_mined_estimate)

        # 预估日收益
        block_reward = self.get_current_block_reward()
        blocks_per_day = 86400.0 / C.TARGET_BLOCK_TIME
        daily_block_reward = block_reward * blocks_per_day
        estimated_daily = (daily_block_reward * node_power / total_power) if total_power > 0 else 0.0

        return {
            "total_power": round(total_power, 2),
            "active_nodes": active_nodes,
            "total_mined": self.get_total_mined(),
            "pending_reward": self.get_pending_reward(),
            "supply_remaining": supply_remaining,
            "current_difficulty": round(self._current_difficulty, 4),
            "current_block_reward": block_reward,
            "halving_epoch": current_epoch,
            "next_halving": self.get_halving_countdown(),
            "node_power": round(node_power, 2),
            "node_power_ratio": round(power_ratio, 4),
            "estimated_daily_reward": round(estimated_daily, 2),
        }

    # ----------------------------------------------------------------
    #  区块链接口适配
    # ----------------------------------------------------------------

    def _get_block_height(self) -> int:
        """获取当前区块高度"""
        try:
            if self._bc is not None:
                if hasattr(self._bc, "get_block_height"):
                    return int(self._bc.get_block_height())
                elif hasattr(self._bc, "block_height"):
                    return int(self._bc.block_height)
        except Exception as e:
            logger.error(f"[MiningEngine] 获取区块高度失败: {e}")
        return 0

    def _get_network_power(self) -> Tuple[float, int]:
        """
        获取全网总算力和活跃节点数

        使用缓存以减少区块链查询频率。

        Returns:
            (total_power, active_nodes)
        """
        now = time.time()

        with self._cache_lock:
            if now < self._cache_expiry and self._cached_total_power > 0:
                return self._cached_total_power, self._cached_active_nodes

        # 缓存过期或无数据，从区块链获取
        total_power = 0.0
        active_nodes = 0

        try:
            if self._bc is not None:
                if hasattr(self._bc, "get_total_power"):
                    total_power = float(self._bc.get_total_power())
                if hasattr(self._bc, "get_active_nodes"):
                    active_nodes = int(self._bc.get_active_nodes())
        except Exception as e:
            logger.error(f"[MiningEngine] 获取网络算力失败: {e}")

        # 如果区块链返回0，使用本地算力作为后备
        if total_power <= 0:
            local_power = self._meter.get_compute_score()
            total_power = max(local_power, 1.0)
            active_nodes = max(active_nodes, 1)

        with self._cache_lock:
            self._cached_total_power = total_power
            self._cached_active_nodes = active_nodes
            self._cached_block_height = self._get_block_height()
            self._cache_expiry = now + self._cache_ttl

        return total_power, active_nodes

    def _refresh_network_cache(self) -> None:
        """刷新网络缓存"""
        self._get_network_power()

        # 难度调整（简化版）
        with self._cache_lock:
            active_nodes = self._cached_active_nodes
        if active_nodes > 100:
            self._current_difficulty = 1.0 + math.log10(active_nodes)
        else:
            self._current_difficulty = 1.0

    # ----------------------------------------------------------------
    #  便捷方法
    # ----------------------------------------------------------------

    def get_mining_summary(self) -> Dict[str, Any]:
        """获取挖矿摘要（便于展示）"""
        return {
            "node_id": self._node_id,
            "state": self._state.value,
            "uptime_s": round(self._meter.get_uptime_seconds(), 1),
            "compute_score": self._meter.get_compute_score(),
            "pending_reward": self.get_pending_reward(),
            "total_mined": self.get_total_mined(),
            "block_reward": self.get_current_block_reward(),
            "halving": self.get_halving_countdown(),
            "hourly_stats": self._meter.get_hourly_stats(),
            "all_time_stats": self._meter.get_all_time_stats(),
        }


# ==================== RewardDistributor: 奖励分配器 ====================

class RewardDistributor:
    """
    奖励分配器 - 将API调用收入分配给参与节点

    当用户调用AI推理API时，燃烧的AICoin中:
        - 80% 分配给提供算力的节点（按算力比例）
        - 20% 进入金库（用于网络维护和发展基金）

    分配过程:
        1. 记录API调用收入（已燃烧的AICoin）
        2. 累积到分配周期
        3. 按算力比例分配给参与节点
        4. 记录分配历史
    """

    # 分配周期（秒）- 每隔此时间执行一次分配
    DISTRIBUTION_INTERVAL: float = 3600.0  # 1小时

    # 最大历史记录数
    MAX_HISTORY: int = 10_000

    # 最小参与算力（低于此不参与分配）
    MIN_PARTICIPATION_POWER: float = 1.0

    def __init__(self, blockchain_manager: Any) -> None:
        """
        初始化奖励分配器

        Args:
            blockchain_manager: 区块链管理器实例
        """
        self._bc = blockchain_manager
        self._lock: threading.RLock = threading.RLock()

        # --- 待分配收入池 ---
        self._revenue_pool: int = 0  # 待分配总收入 (AIC, 最小单位)
        self._node_contributions: Dict[str, float] = {}  # node_id -> 累计算力

        # --- 分配历史 ---
        self._history: deque[RewardDistributionEvent] = deque(maxlen=self.MAX_HISTORY)

        # --- 分配线程 ---
        self._distribution_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_distribution_time: float = 0.0

        # --- 统计 ---
        self._total_distributed: int = 0
        self._total_treasury: int = 0
        self._total_api_revenue: int = 0

        logger.info("[RewardDistributor] 初始化完成")

    # ----------------------------------------------------------------
    #  收入记录
    # ----------------------------------------------------------------

    def record_api_revenue(
        self,
        node_id: str,
        burn_amount: int,
        compute_power: float = 0.0,
    ) -> None:
        """
        记录API调用收入 (已燃烧的AICoin)

        当用户调用某个节点的AI推理API时，燃烧的AICoin进入收入池。
        80% 将分配给提供算力的节点，20% 进入金库。

        Args:
            node_id:       提供服务的节点ID
            burn_amount:   燃烧的AICoin数量（最小单位）
            compute_power: 该节点的算力分数（可选，用于分配计算）
        """
        if burn_amount <= 0:
            logger.warning("[RewardDistributor] 燃烧金额无效: %d", burn_amount)
            return

        if not node_id:
            logger.warning("[RewardDistributor] 节点ID为空")
            return

        with self._lock:
            self._revenue_pool += burn_amount
            self._total_api_revenue += burn_amount

            # 更新节点贡献（使用指数移动平均）
            if compute_power > 0:
                current = self._node_contributions.get(node_id, 0.0)
                # EMA: alpha=0.3, 新值占30%
                self._node_contributions[node_id] = current * 0.7 + compute_power * 0.3

        logger.debug(
            f"[RewardDistributor] 记录API收入 node={node_id[:12]} "
            f"amount={burn_amount} pool={self._revenue_pool}"
        )

    def update_node_power(self, node_id: str, compute_power: float) -> None:
        """
        更新节点算力（外部调用）

        Args:
            node_id:       节点ID
            compute_power: 算力分数
        """
        if compute_power <= 0 or not node_id:
            return

        with self._lock:
            current = self._node_contributions.get(node_id, 0.0)
            self._node_contributions[node_id] = current * 0.7 + compute_power * 0.3

    def remove_node(self, node_id: str) -> None:
        """
        移除节点（节点下线时调用）

        Args:
            node_id: 节点ID
        """
        with self._lock:
            self._node_contributions.pop(node_id, None)
            logger.info(f"[RewardDistributor] 节点已移除 node={node_id[:12]}")

    # ----------------------------------------------------------------
    #  收入分配
    # ----------------------------------------------------------------

    def distribute_revenue(self) -> Dict[str, Any]:
        """
        按算力比例分配收入

        分配公式:
            node_reward = total_revenue * 0.8 * (node_power / total_power)
            treasury    = total_revenue * 0.2

        Returns:
            {
                "success": bool,
                "total_revenue": int,
                "node_distribution": int,
                "treasury": int,
                "participants": int,
                "details": {node_id: amount, ...},
            }
        """
        with self._lock:
            if self._revenue_pool <= 0:
                return {
                    "success": False,
                    "message": "收入池为空",
                    "total_revenue": 0,
                    "node_distribution": 0,
                    "treasury": 0,
                    "participants": 0,
                    "details": {},
                }

            # 获取当前快照
            pool = self._revenue_pool
            contributions = dict(self._node_contributions)

            # 清空收入池（防止重复分配）
            self._revenue_pool = 0

        # 计算总算力
        total_power = sum(
            power for power in contributions.values()
            if power >= self.MIN_PARTICIPATION_POWER
        )

        if total_power <= 0:
            # 无合格节点，全部进入金库
            logger.warning(
                "[RewardDistributor] 无合格参与节点，收入全部进入金库"
            )
            treasury = pool

            self._bc_transfer_treasury(treasury)
            self._total_treasury += treasury

            event = self._create_event(
                total_revenue=pool,
                node_distribution=0,
                treasury=treasury,
                participants=0,
                details={},
            )
            with self._lock:
                self._history.append(event)

            return {
                "success": True,
                "message": "无合格节点，收入进入金库",
                "total_revenue": pool,
                "node_distribution": 0,
                "treasury": treasury,
                "participants": 0,
                "details": {},
            }

        # 分配计算
        C = MINING_CONSTANTS
        node_share_total = int(pool * C.NODE_REVENUE_SHARE)  # 80%
        treasury = pool - node_share_total                     # 20%

        details: Dict[str, int] = {}
        distributed_total = 0

        for node_id, power in contributions.items():
            if power < self.MIN_PARTICIPATION_POWER:
                continue

            ratio = power / total_power
            reward = int(node_share_total * ratio)
            details[node_id] = reward
            distributed_total += reward

        # 处理舍入误差
        rounding_diff = node_share_total - distributed_total
        if rounding_diff > 0 and details:
            # 将余额分配给算力最高的节点
            top_node = max(details, key=details.get)
            details[top_node] += rounding_diff
            distributed_total += rounding_diff

        # 执行转账
        transfer_success = self._bc_distribute_rewards(details)
        if not transfer_success:
            logger.error("[RewardDistributor] 奖励转账失败")
            # 回滚到收入池
            with self._lock:
                self._revenue_pool += pool
            return {
                "success": False,
                "message": "区块链转账失败，收入已回滚",
                "total_revenue": pool,
                "node_distribution": 0,
                "treasury": 0,
                "participants": 0,
                "details": {},
            }

        # 金库转账
        self._bc_transfer_treasury(treasury)

        with self._lock:
            self._total_distributed += distributed_total
            self._total_treasury += treasury
            self._last_distribution_time = time.time()

            event = self._create_event(
                total_revenue=pool,
                node_distribution=distributed_total,
                treasury=treasury,
                participants=len(details),
                details=details,
            )
            self._history.append(event)

        logger.info(
            f"[RewardDistributor] 收入分配完成: 总计={pool} AIC "
            f"节点分配={distributed_total} AIC 金库={treasury} AIC "
            f"参与节点={len(details)}"
        )

        return {
            "success": True,
            "total_revenue": pool,
            "node_distribution": distributed_total,
            "treasury": treasury,
            "participants": len(details),
            "details": details,
        }

    # ----------------------------------------------------------------
    #  分配历史
    # ----------------------------------------------------------------

    def get_distribution_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取分配历史记录

        Args:
            limit: 最大返回条数

        Returns:
            分配历史列表，按时间倒序
        """
        with self._lock:
            history = list(self._history)

        history = sorted(history, key=lambda e: e.timestamp, reverse=True)
        history = history[:limit]

        return [
            {
                "event_id": e.event_id,
                "block_height": e.block_height,
                "timestamp": e.timestamp,
                "datetime": datetime.fromtimestamp(e.timestamp, tz=timezone.utc).isoformat(),
                "total_revenue": e.total_revenue,
                "node_distribution": e.node_distribution,
                "treasury": e.treasury,
                "participants": e.participants,
                "top_recipients": dict(
                    sorted(e.distribution_details.items(), key=lambda x: x[1], reverse=True)[:10]
                ),
            }
            for e in history
        ]

    def get_node_earnings(self, node_id: str) -> Dict[str, Any]:
        """
        获取指定节点的累计收益

        Args:
            node_id: 节点ID

        Returns:
            {"total_earned": int, "distribution_count": int, "last_earning_time": float}
        """
        total_earned = 0
        distribution_count = 0
        last_time = 0.0

        with self._lock:
            for event in self._history:
                earned = event.distribution_details.get(node_id, 0)
                if earned > 0:
                    total_earned += earned
                    distribution_count += 1
                    last_time = max(last_time, event.timestamp)

        return {
            "node_id": node_id,
            "total_earned": total_earned,
            "distribution_count": distribution_count,
            "last_earning_time": last_time,
        }

    def get_pool_balance(self) -> Dict[str, Any]:
        """
        获取当前收入池状态

        Returns:
            收入池余额和统计信息
        """
        with self._lock:
            return {
                "revenue_pool": self._revenue_pool,
                "active_nodes": len(self._node_contributions),
                "total_distributed": self._total_distributed,
                "total_treasury": self._total_treasury,
                "total_api_revenue": self._total_api_revenue,
                "last_distribution_time": self._last_distribution_time,
                "node_count": len(self._node_contributions),
            }

    # ----------------------------------------------------------------
    #  自动分配线程
    # ----------------------------------------------------------------

    def start_auto_distribution(self) -> None:
        """启动自动分配线程"""
        if self._distribution_thread is not None and self._distribution_thread.is_alive():
            logger.warning("[RewardDistributor] 自动分配已在运行")
            return

        self._stop_event.clear()
        self._distribution_thread = threading.Thread(
            target=self._distribution_loop,
            name="reward-distributor",
            daemon=True,
        )
        self._distribution_thread.start()
        logger.info("[RewardDistributor] 自动分配已启动")

    def stop_auto_distribution(self) -> None:
        """停止自动分配线程"""
        self._stop_event.set()
        if self._distribution_thread is not None:
            self._distribution_thread.join(timeout=15.0)
            self._distribution_thread = None
        logger.info("[RewardDistributor] 自动分配已停止")

    def _distribution_loop(self) -> None:
        """自动分配循环"""
        while not self._stop_event.is_set():
            try:
                self._stop_event.wait(timeout=self.DISTRIBUTION_INTERVAL)

                if self._stop_event.is_set():
                    break

                result = self.distribute_revenue()
                if result.get("success"):
                    logger.debug("[RewardDistributor] 自动分配完成")

            except Exception as e:
                logger.error(f"[RewardDistributor] 自动分配异常: {e}", exc_info=True)

    # ----------------------------------------------------------------
    #  区块链接口适配
    # ----------------------------------------------------------------

    def _bc_distribute_rewards(self, details: Dict[str, int]) -> bool:
        """调用区块链分配奖励"""
        try:
            if self._bc is not None and hasattr(self._bc, "distribute_rewards"):
                return bool(self._bc.distribute_rewards(details))
            return True  # 模拟成功
        except Exception as e:
            logger.error(f"[RewardDistributor] 区块链分配失败: {e}")
            return False

    def _bc_transfer_treasury(self, amount: int) -> bool:
        """调用区块链转入金库"""
        try:
            if self._bc is not None and hasattr(self._bc, "transfer_to_treasury"):
                return bool(self._bc.transfer_to_treasury(amount))
            return True  # 模拟成功
        except Exception as e:
            logger.error(f"[RewardDistributor] 金库转账失败: {e}")
            return False

    # ----------------------------------------------------------------
    #  内部方法
    # ----------------------------------------------------------------

    def _create_event(
        self,
        total_revenue: int,
        node_distribution: int,
        treasury: int,
        participants: int,
        details: Dict[str, int],
    ) -> RewardDistributionEvent:
        """创建分配事件记录"""
        return RewardDistributionEvent(
            event_id=str(uuid.uuid4()),
            block_height=self._get_block_height(),
            timestamp=time.time(),
            total_revenue=total_revenue,
            node_distribution=node_distribution,
            treasury=treasury,
            participants=participants,
            distribution_details=details,
        )

    def _get_block_height(self) -> int:
        """获取当前区块高度"""
        try:
            if self._bc is not None:
                if hasattr(self._bc, "get_block_height"):
                    return int(self._bc.get_block_height())
                elif hasattr(self._bc, "block_height"):
                    return int(self._bc.block_height)
        except Exception:
            pass
        return 0
