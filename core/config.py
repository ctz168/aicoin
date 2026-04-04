"""
AICoin 项目配置
===============

提供 AICoin 节点的全局配置管理，支持从文件、环境变量加载配置，
以及配置持久化保存。

配置优先级 (由高到低):
    1. 环境变量 (AICOIN_ 前缀)
    2. 配置文件 (config.json)
    3. 默认值
"""

import os
import sys
import json
import uuid
import copy
import logging
from dataclasses import dataclass, field, asdict, fields
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

logger = logging.getLogger("aicoin.config")


# ==================== 类型别名 ====================

EnvMapping = Dict[str, str]  # 环境变量名 -> 配置字段名映射


# ==================== 环境变量映射表 ====================

# 配置字段名 -> (环境变量名, 类型转换函数)
_ENV_VAR_MAP: Dict[str, Tuple[str, Any]] = {
    # 节点身份
    "node_id": ("AICOIN_NODE_ID", str),
    "node_name": ("AICOIN_NODE_NAME", str),
    "wallet_address": ("AICOIN_WALLET_ADDRESS", str),

    # 网络
    "host": ("AICOIN_HOST", str),
    "api_port": ("AICOIN_API_PORT", int),
    "p2p_port": ("AICOIN_P2P_PORT", int),
    "seeds": ("AICOIN_SEEDS", lambda v: [s.strip() for s in v.split(",") if s.strip()]),

    # 区块链
    "blockchain_mode": ("AICOIN_BLOCKCHAIN_MODE", str),
    "web3_rpc_url": ("AICOIN_WEB3_RPC_URL", str),
    "contract_address": ("AICOIN_CONTRACT_ADDRESS", str),
    "chain_id": ("AICOIN_CHAIN_ID", int),

    # 挖矿
    "auto_mine": ("AICOIN_AUTO_MINE", lambda v: v.lower() in ("1", "true", "yes")),
    "mining_interval": ("AICOIN_MINING_INTERVAL", int),
    "proof_submission_enabled": ("AICOIN_PROOF_SUBMISSION", lambda v: v.lower() in ("1", "true", "yes")),

    # API 网关
    "api_enabled": ("AICOIN_API_ENABLED", lambda v: v.lower() in ("1", "true", "yes")),
    "daily_burn_limit": ("AICOIN_DAILY_BURN_LIMIT", int),

    # 治理
    "governance_enabled": ("AICOIN_GOVERNANCE_ENABLED", lambda v: v.lower() in ("1", "true", "yes")),
    "min_proposal_stake": ("AICOIN_MIN_PROPOSAL_STAKE", int),
    "voting_period": ("AICOIN_VOTING_PERIOD", int),
    "quorum_percentage": ("AICOIN_QUORUM_PERCENTAGE", float),
    "approval_threshold": ("AICOIN_APPROVAL_THRESHOLD", float),

    # 路由
    "routing_strategy": ("AICOIN_ROUTING_STRATEGY", str),
    "max_routing_retries": ("AICOIN_MAX_ROUTING_RETRIES", int),
    "routing_timeout": ("AICOIN_ROUTING_TIMEOUT", float),

    # 模型
    "model_name": ("AICOIN_MODEL_NAME", str),
    "model_base_path": ("AICOIN_MODEL_BASE_PATH", str),

    # 代币经济学
    "initial_block_reward": ("AICOIN_INITIAL_BLOCK_REWARD", int),
    "halving_interval": ("AICOIN_HALVING_INTERVAL", int),
    "max_supply": ("AICOIN_MAX_SUPPLY", int),

    # 收入分配
    "node_reward_percentage": ("AICOIN_NODE_REWARD_PCT", float),
    "treasury_percentage": ("AICOIN_TREASURY_PCT", float),

    # 日志
    "log_level": ("AICOIN_LOG_LEVEL", str),
    "log_file": ("AICOIN_LOG_FILE", str),

    # 持久化
    "data_dir": ("AICOIN_DATA_DIR", str),
    "state_file": ("AICOIN_STATE_FILE", str),
}

# 需要特殊处理的嵌套字段 (不支持简单的环境变量映射)
_COMPLEX_FIELDS = {"api_tiers"}


@dataclass
class AICoinConfig:
    """AICoin 节点配置

    所有可配置参数的集中管理。支持从 JSON 文件和环境变量加载。

    Attributes:
        node_id: 节点唯一标识符 (自动生成 UUID 若为空)
        node_name: 节点显示名称
        wallet_address: AICoin 钱包地址 (用于接收挖矿奖励)
        host: 监听地址
        api_port: API 网关端口
        p2p_port: P2P 网络通信端口
        seeds: 种子节点列表 (格式: ["host:port", ...])
        blockchain_mode: 区块链模式 ("simulation" 模拟链 / "web3" 真实链)
        web3_rpc_url: Web3 RPC 节点 URL
        contract_address: 智能合约地址
        chain_id: 区块链 ID
        auto_mine: 是否自动开始挖矿
        mining_interval: 挖矿证明提交间隔 (秒)
        proof_submission_enabled: 是否启用算力证明提交
        api_enabled: 是否启用 API 网关服务
        api_tiers: API 服务等级定价配置
        daily_burn_limit: 每地址每日最大消耗量 (AIC)
        governance_enabled: 是否启用链上治理
        min_proposal_stake: 提案最低质押量 (AIC)
        voting_period: 投票周期 (秒)
        quorum_percentage: 法定人数百分比 (占总供应量)
        approval_threshold: 通过阈值 (占投票数百分比)
        routing_strategy: 请求路由策略
        max_routing_retries: 路由最大重试次数
        routing_timeout: 路由超时时间 (秒)
        model_name: 推理模型名称
        model_base_path: 模型文件存储基础路径
        initial_block_reward: 初始区块奖励 (AIC)
        halving_interval: 减半间隔 (区块数)
        max_supply: AIC 最大供应量
        node_reward_percentage: 节点奖励占比 (%)
        treasury_percentage: 国库奖励占比 (%)
        log_level: 日志级别
        log_file: 日志文件路径
        data_dir: 数据存储目录
        state_file: 节点状态文件名
    """

    # === 节点身份 ===
    node_id: str = ""
    node_name: str = "aicoin-node"
    wallet_address: str = ""  # AICoin 钱包地址，用于接收挖矿奖励

    # === 网络 ===
    host: str = "0.0.0.0"
    api_port: int = 8080       # API 网关端口
    p2p_port: int = 5000       # P2P 网络端口
    seeds: list = field(default_factory=lambda: [])

    # === 区块链 ===
    blockchain_mode: str = "simulation"  # "simulation" 或 "web3"
    web3_rpc_url: str = ""
    contract_address: str = ""
    chain_id: int = 1

    # === 挖矿 ===
    auto_mine: bool = True
    mining_interval: int = 60  # 两次算力证明提交之间的间隔 (秒)
    proof_submission_enabled: bool = True

    # === API 网关 ===
    api_enabled: bool = True
    # v2 上线初期优惠定价 (闲置算力 9 折已包含)
    # 单位: 最小单位 (10^8 = 1 AIC), 乘以优先级倍率后为实际价格
    api_tiers: dict = field(default_factory=lambda: {
        "basic": {"price_per_1k_tokens_input": 1, "price_per_1k_tokens_output": 3, "priority": 1},   # ×1.0 (基准价)
        "premium": {"price_per_1k_tokens_input": 2, "price_per_1k_tokens_output": 6, "priority": 2},   # ×2.0
        "priority": {"price_per_1k_tokens_input": 3, "price_per_1k_tokens_output": 9, "priority": 3},  # ×3.0
    })
    daily_burn_limit: int = 100000  # 每地址每日最大消耗 AIC

    # === 治理 ===
    governance_enabled: bool = True
    min_proposal_stake: int = 1000  # AIC
    voting_period: int = 7 * 24 * 3600  # 7 天 (秒)
    quorum_percentage: float = 10.0  # 占总供应量的 10%
    approval_threshold: float = 51.0  # 占投票数的 51%

    # === 路由 ===
    routing_strategy: str = "BALANCED"  # LATENCY_FIRST, CAPABILITY_FIRST, COST_FIRST, BALANCED
    max_routing_retries: int = 3
    routing_timeout: float = 30.0  # 秒

    # === 模型 ===
    model_name: str = "Qwen/Qwen2.5-0.5B-Instruct"
    model_base_path: str = "./models"

    # === 代币经济学 ===
    initial_block_reward: int = 50  # AIC
    halving_interval: int = 210000  # 区块数
    max_supply: int = 21000000  # AIC

    # === 收入分配 ===
    node_reward_percentage: float = 80.0  # 80% 分配给挖矿节点
    treasury_percentage: float = 20.0     # 20% 分配给国库

    # === 日志 ===
    log_level: str = "INFO"
    log_file: str = "aicoin.log"

    # === 持久化 ===
    data_dir: str = "./data"
    state_file: str = "state.json"

    # ==================== 生命周期方法 ====================

    def __post_init__(self) -> None:
        """初始化后处理: 自动生成节点 ID，校验关键参数"""
        if not self.node_id:
            self.node_id = str(uuid.uuid4())

        if not self.node_name or self.node_name == "aicoin-node":
            self.node_name = f"aicoin-{self.node_id[:8]}"

        self._validate()

    def _validate(self) -> None:
        """校验配置参数的合法性

        Raises:
            ValueError: 配置参数不合法时抛出
        """
        # 端口范围校验
        if not (1 <= self.api_port <= 65535):
            raise ValueError(f"api_port 超出有效范围 (1-65535): {self.api_port}")
        if not (1 <= self.p2p_port <= 65535):
            raise ValueError(f"p2p_port 超出有效范围 (1-65535): {self.p2p_port}")
        if self.api_port == self.p2p_port:
            raise ValueError(
                f"api_port 和 p2p_port 不能相同: {self.api_port}"
            )

        # 区块链模式校验
        if self.blockchain_mode not in ("simulation", "web3"):
            raise ValueError(
                f"blockchain_mode 必须为 'simulation' 或 'web3': {self.blockchain_mode}"
            )

        # Web3 模式下必须配置 RPC URL
        if self.blockchain_mode == "web3" and not self.web3_rpc_url:
            raise ValueError("web3 模式下必须配置 web3_rpc_url")

        # 路由策略校验
        valid_strategies = (
            "LATENCY_FIRST", "CAPABILITY_FIRST", "COST_FIRST", "BALANCED"
        )
        if self.routing_strategy not in valid_strategies:
            raise ValueError(
                f"routing_strategy 必须为 {valid_strategies} 之一: "
                f"{self.routing_strategy}"
            )

        # 挖矿间隔校验
        if self.mining_interval < 1:
            raise ValueError(f"mining_interval 必须大于 0: {self.mining_interval}")

        # 收入分配校验
        total_pct = self.node_reward_percentage + self.treasury_percentage
        if abs(total_pct - 100.0) > 0.01:
            raise ValueError(
                f"node_reward_percentage + treasury_percentage 必须等于 100: "
                f"{total_pct}"
            )

        # 治理参数校验
        if self.quorum_percentage <= 0 or self.quorum_percentage > 100:
            raise ValueError(
                f"quorum_percentage 必须在 (0, 100] 范围内: {self.quorum_percentage}"
            )
        if self.approval_threshold <= 0 or self.approval_threshold > 100:
            raise ValueError(
                f"approval_threshold 必须在 (0, 100] 范围内: {self.approval_threshold}"
            )
        if self.voting_period <= 0:
            raise ValueError(
                f"voting_period 必须大于 0: {self.voting_period}"
            )

        # 代币经济学校验
        if self.initial_block_reward <= 0:
            raise ValueError(
                f"initial_block_reward 必须大于 0: {self.initial_block_reward}"
            )
        if self.halving_interval <= 0:
            raise ValueError(
                f"halving_interval 必须大于 0: {self.halving_interval}"
            )
        if self.max_supply <= 0:
            raise ValueError(f"max_supply 必须大于 0: {self.max_supply}")

        # 日志级别校验
        valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        if self.log_level.upper() not in valid_levels:
            raise ValueError(
                f"log_level 必须为 {valid_levels} 之一: {self.log_level}"
            )

        # API 定价层级校验
        for tier_name, tier_config in self.api_tiers.items():
            if not isinstance(tier_config, dict):
                raise ValueError(
                    f"api_tiers[{tier_name}] 必须是字典类型"
                )
            if "priority" not in tier_config:
                raise ValueError(
                    f"api_tiers[{tier_name}] 缺少 'priority' 字段"
                )
            input_key = "price_per_1k_tokens_input"
            output_key = "price_per_1k_tokens_output"
            if input_key not in tier_config:
                raise ValueError(
                    f"api_tiers[{tier_name}] 缺少 '{input_key}' 字段"
                )
            if output_key not in tier_config:
                raise ValueError(
                    f"api_tiers[{tier_name}] 缺少 '{output_key}' 字段"
                )
            if tier_config[input_key] < 0:
                raise ValueError(
                    f"api_tiers[{tier_name}].{input_key} 不能为负数"
                )
            if tier_config[output_key] < 0:
                raise ValueError(
                    f"api_tiers[{tier_name}].{output_key} 不能为负数"
                )

    # ==================== 序列化方法 ====================

    def to_dict(self) -> Dict[str, Any]:
        """将配置转换为可序列化的字典

        Returns:
            包含所有配置字段的字典
        """
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        """将配置序列化为 JSON 字符串

        Args:
            indent: JSON 缩进空格数

        Returns:
            格式化的 JSON 字符串
        """
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    # ==================== 类方法: 加载配置 ====================

    @classmethod
    def from_file(cls, path: str = "config.json") -> "AICoinConfig":
        """从 JSON 配置文件加载配置

        配置文件中的字段名与 dataclass 字段名一致。未指定的字段使用默认值。

        Args:
            path: 配置文件路径 (相对于当前工作目录或绝对路径)

        Returns:
            加载并校验后的 AICoinConfig 实例

        Raises:
            FileNotFoundError: 配置文件不存在
            json.JSONDecodeError: 配置文件 JSON 格式错误
            ValueError: 配置参数校验失败

        Example:
            >>> config = AICoinConfig.from_file("config.json")
            >>> config.api_port
            8080
        """
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path.absolute()}")

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(
                f"配置文件 JSON 格式错误: {e}", e.doc, e.pos
            )

        if not isinstance(data, dict):
            raise ValueError("配置文件顶层必须是 JSON 对象 (dict)")

        logger.info(f"从文件加载配置: {config_path.absolute()}")
        return cls._from_dict(data)

    @classmethod
    def from_env(cls) -> "AICoinConfig":
        """从环境变量加载配置

        环境变量命名规则: AICOIN_ + 字段名 (大写下划线)

        例如:
            AICOIN_API_PORT=9090        -> api_port = 9090
            AICOIN_AUTO_MINE=true       -> auto_mine = True
            AICOIN_SEEDS=a:5000,b:5000  -> seeds = ["a:5000", "b:5000"]

        Returns:
            加载并校验后的 AICoinConfig 实例

        Note:
            环境变量只覆盖简单标量字段，嵌套字段 (如 api_tiers)
            需要通过配置文件设置。
        """
        overrides: Dict[str, Any] = {}
        applied_vars: List[str] = []

        for field_name, (env_var, converter) in _ENV_VAR_MAP.items():
            value = os.environ.get(env_var)
            if value is None:
                continue

            try:
                overrides[field_name] = converter(value)
                applied_vars.append(env_var)
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"环境变量 {env_var}={value} 转换失败: {e}, "
                    f"使用默认值"
                )

        if applied_vars:
            logger.info(f"从环境变量加载配置: {', '.join(applied_vars)}")

        return cls._from_dict(overrides)

    @classmethod
    def from_file_and_env(cls, path: str = "config.json") -> "AICoinConfig":
        """先从文件加载，再用环境变量覆盖

        合并优先级: 环境变量 > 配置文件 > 默认值

        Args:
            path: 配置文件路径

        Returns:
            合并后的 AICoinConfig 实例
        """
        # 先尝试加载文件
        config_path = Path(path)
        if config_path.exists():
            config = cls.from_file(path)
        else:
            logger.warning(f"配置文件 {path} 不存在, 使用默认值")
            config = cls()

        # 再用环境变量覆盖
        for field_name, (env_var, converter) in _ENV_VAR_MAP.items():
            value = os.environ.get(env_var)
            if value is None:
                continue

            try:
                setattr(config, field_name, converter(value))
                logger.debug(f"环境变量覆盖: {field_name} = {value}")
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"环境变量 {env_var}={value} 转换失败: {e}, 保持原值"
                )

        return config

    # ==================== 实例方法: 保存配置 ====================

    def save(self, path: str = "config.json") -> None:
        """将配置保存到 JSON 文件

        Args:
            path: 目标文件路径。如果父目录不存在则自动创建。

        Raises:
            OSError: 文件写入失败
        """
        config_path = Path(path)

        # 自动创建父目录
        config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(config_path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

        logger.info(f"配置已保存到: {config_path.absolute()}")

    # ==================== 内部方法 ====================

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "AICoinConfig":
        """从字典创建配置实例

        仅使用字典中存在的键来覆盖默认值。不存在的键保持默认值。

        Args:
            data: 配置字典

        Returns:
            创建并校验后的 AICoinConfig 实例

        Raises:
            TypeError: 未知配置字段
        """
        # 获取 dataclass 所有字段名
        valid_fields = {f.name for f in fields(cls)}

        # 检查是否有未知字段
        unknown_keys = set(data.keys()) - valid_fields
        if unknown_keys:
            logger.warning(
                f"配置中包含未知字段 (将被忽略): {', '.join(sorted(unknown_keys))}"
            )

        # 过滤出有效字段并深拷贝 (避免外部修改影响配置)
        filtered_data: Dict[str, Any] = {}
        for key, value in data.items():
            if key in valid_fields:
                filtered_data[key] = copy.deepcopy(value)

        return cls(**filtered_data)

    def __repr__(self) -> str:
        """配置的可读表示 (隐藏敏感信息)"""
        wallet_display = (
            self.wallet_address[:10] + "..." if len(self.wallet_address) > 10
            else self.wallet_address or "(未设置)"
        )
        return (
            f"AICoinConfig("
            f"node_id={self.node_id[:8]}..., "
            f"node_name={self.node_name!r}, "
            f"wallet={wallet_display}, "
            f"api_port={self.api_port}, "
            f"p2p_port={self.p2p_port}, "
            f"mode={self.blockchain_mode})"
        )


# ==================== 便捷函数 ====================

def setup_logging_from_config(config: AICoinConfig) -> None:
    """根据配置初始化日志系统

    Args:
        config: AICoinConfig 实例
    """
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    log_format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # 清除已有 handler
    root_logger = logging.getLogger("aicoin")
    root_logger.handlers.clear()
    root_logger.setLevel(log_level)

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))
    root_logger.addHandler(console_handler)

    # 文件 handler (如果配置了日志文件)
    if config.log_file:
        log_path = Path(config.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(log_format, date_format))
        root_logger.addHandler(file_handler)

    logger.info(f"日志系统已初始化: level={config.log_level}, file={config.log_file or '(无)'}")
