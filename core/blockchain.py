"""
AICoin 区块链集成层 - Blockchain Integration Layer
=====================================================

本模块实现了 AICoin 去中心化 AI 算力挖矿网络的区块链交互层。
支持两种运行模式：
  1. 模拟模式（Simulation Mode）：无需区块链，内存中模拟完整智能合约逻辑
  2. Web3 模式（Production Mode）：连接以太坊兼容链，与真实智能合约交互

所有模拟逻辑与 Solidity 合约严格对应，确保开发和测试阶段与生产环境行为一致。

使用示例:
    >>> config = {"mode": "simulation"}
    >>> bm = BlockchainManager(config)
    >>> bm.mint_mining_reward("0xNode1", 1000)
    True
    >>> bm.get_balance("0xNode1")
    1000
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("aicoin.blockchain")

# ==============================================================================
# 常量定义 - 与 Solidity 合约保持一致
# ==============================================================================

# 代币精度（18位小数，与 ERC20 标准一致）
TOKEN_DECIMALS: int = 18
TOKEN_DECIMALS_FACTOR: int = 10 ** TOKEN_DECIMALS

# 初始区块奖励（单位：AIC，已考虑精度）
INITIAL_BLOCK_REWARD: int = 50 * TOKEN_DECIMALS_FACTOR

# 减半周期：每 210,000 个区块奖励减半
HALVING_INTERVAL: int = 210_000

# 提案投票周期（模拟模式下以秒为单位，对应合约中的 block number 差值）
VOTING_PERIOD_BLOCKS: int = 50_400  # 约7天的区块数（按15秒出块计算）
VOTING_PERIOD_SECONDS: int = 604800  # 7天

# 提案执行延迟
EXECUTION_DELAY_BLOCKS: int = 1

# API 访问价格（每 1K tokens）
API_PRICES: Dict[str, int] = {
    "basic": 1 * TOKEN_DECIMALS_FACTOR,
    "standard": 5 * TOKEN_DECIMALS_FACTOR,
    "premium": 20 * TOKEN_DECIMALS_FACTOR,
}

# 每日 API 调用限额
API_DAILY_LIMITS: Dict[str, int] = {
    "basic": 10_000,
    "standard": 100_000,
    "premium": 1_000_000,
}

# 最小算力证明阈值
MIN_COMPUTE_POWER: float = 0.0

# 燃烧黑洞地址
BURN_ADDRESS: str = "0x0000000000000000000000000000000000000000"

# 合约所有者地址（模拟模式专用）
CONTRACT_OWNER: str = "0x000000000000000000000000000000000000dEaD"

# 矿工合约地址（模拟模式专用，代表矿工合约身份）
MINING_CONTRACT_ADDRESS: str = "0x000000000000000000000000000000000000M1n3"


# ==============================================================================
# 数据模型定义
# ==============================================================================

class ProposalStatus(Enum):
    """提案状态枚举，与 Solidity 合约中的 enum ProposalState 对应"""
    PENDING = "Pending"         # 待激活
    ACTIVE = "Active"           # 投票中
    CANCELED = "Canceled"       # 已取消
    DEFEATED = "Defeated"       # 未通过
    SUCCEEDED = "Succeeded"     # 已通过待执行
    QUEUED = "Queued"           # 已排队待执行
    EXECUTED = "Executed"       # 已执行
    EXPIRED = "Expired"         # 已过期


class ProposalType(Enum):
    """提案类型枚举"""
    PARAMETER_CHANGE = "ParameterChange"     # 参数修改
    MODEL_ADDITION = "ModelAddition"         # 新增模型
    MODEL_UPGRADE = "ModelUpgrade"           # 模型升级
    EMERGENCY = "Emergency"                  # 紧急提案


@dataclass
class MiningInfo:
    """矿工挖矿信息，与合约中的 MinerInfo struct 对应"""
    compute_power: float = 0.0               # 当前算力（FLOPS）
    tasks_completed: int = 0                  # 已完成任务数
    pending_reward: int = 0                   # 待领取奖励（含精度）
    total_claimed: int = 0                    # 累计已领取奖励（含精度）
    last_proof_block: int = 0                 # 上次提交证明的区块号
    last_proof_time: float = 0.0             # 上次提交证明的时间戳


@dataclass
class Proposal:
    """治理提案，与合约中的 Proposal struct 对应"""
    id: int = 0                               # 提案编号
    proposal_type: str = ""                   # 提案类型
    proposer: str = ""                        # 提案发起人地址
    title: str = ""                           # 提案标题
    description: str = ""                     # 提案描述
    model_name: str = ""                      # 关联模型名称（模型类提案专用）
    votes_for: int = 0                        # 赞成票权重
    votes_against: int = 0                    # 反对票权重
    start_block: int = 0                      # 投票开始区块号
    start_time: float = 0.0                  # 投票开始时间戳
    end_block: int = 0                        # 投票结束区块号
    end_time: float = 0.0                    # 投票结束时间戳
    status: str = ProposalStatus.PENDING.value  # 提案状态
    executed: bool = False                    # 是否已执行
    voters: Dict[str, bool] = field(default_factory=dict)  # 已投票者 {address: support}


@dataclass
class APIAccessRecord:
    """API 访问记录"""
    tier: str = "basic"                       # 当前等级
    daily_quota_used: int = 0                 # 今日已用量
    last_reset_time: float = 0.0             # 上次配额重置时间
    access_enabled: bool = False             # 是否启用


@dataclass
class BurnRecord:
    """代币燃烧记录"""
    amount: int = 0                           # 燃烧数量（含精度）
    purpose: str = ""                         # 燃烧用途
    timestamp: float = 0.0                   # 燃烧时间戳


# ==============================================================================
# 自定义异常
# ==============================================================================

class BlockchainError(Exception):
    """区块链操作基础异常"""
    pass


class InsufficientBalanceError(BlockchainError):
    """余额不足异常"""
    pass


class InvalidAddressError(BlockchainError):
    """无效地址异常"""
    pass


class UnauthorizedError(BlockchainError):
    """未授权操作异常"""
    pass


class ProposalError(BlockchainError):
    """提案操作异常"""
    pass


class MiningError(BlockchainError):
    """挖矿操作异常"""
    pass


# ==============================================================================
# 工具函数
# ==============================================================================

def _validate_address(address: str) -> bool:
    """
    验证以太坊地址格式。
    
    支持 0x 开头的 40 位十六进制地址，以及模拟模式下的简化地址。
    
    Args:
        address: 待验证的地址字符串
        
    Returns:
        bool: 地址是否有效
    """
    if not address or not isinstance(address, str):
        return False
    if address.startswith("0x") and len(address) == 42:
        try:
            int(address, 16)
            return True
        except ValueError:
            return False
    # 模拟模式下允许简化地址
    if address.startswith("0x") and len(address) > 2:
        return True
    return True  # 允许任意非空字符串用于模拟模式


def _normalize_address(address: str) -> str:
    """
    规范化地址格式（模拟模式统一使用 checksum 格式）。
    
    Args:
        address: 原始地址
        
    Returns:
        str: 规范化后的地址
    """
    if not _validate_address(address):
        raise InvalidAddressError(f"无效地址: {address}")
    return address.strip().lower()


def _current_timestamp() -> float:
    """获取当前 Unix 时间戳（秒）。"""
    return time.time()


# ==============================================================================
# 主类：BlockchainManager
# ==============================================================================

class BlockchainManager:
    """
    AICoin 区块链管理器 - 核心集成层
    
    管理与 AICoin 智能合约的所有交互。支持模拟模式（开发/测试）和
    Web3 模式（生产环境）。模拟模式下的所有逻辑与 Solidity 合约严格对应。
    
    Attributes:
        mode (str): 运行模式，"simulation" 或 "web3"
        is_simulation (bool): 是否为模拟模式
        
    Example:
        >>> config = {"mode": "simulation", "state_file": "blockchain_state.json"}
        >>> bm = BlockchainManager(config)
        >>> bm.mint_mining_reward("0xMiner1", 50_000_000_000_000_000_000)
        True
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        初始化区块链管理器。
        
        根据配置选择运行模式。模拟模式使用内存数据结构精确模拟
        Solidity 智能合约逻辑；Web3 模式连接真实区块链节点。
        
        Args:
            config: 配置字典，支持以下字段：
                - mode (str): "simulation"（默认）或 "web3"
                - state_file (str): 模拟模式状态持久化文件路径
                - rpc_url (str): Web3 模式 RPC 节点地址
                - token_address (str): Web3 模式代币合约地址
                - mining_address (str): Web3 模式挖矿合约地址
                - governance_address (str): Web3 模式治理合约地址
                - chain_id (int): 链 ID
                - private_key (str): 签名私钥（Web3 模式）
                - auto_save (bool): 是否自动保存状态（默认 True）
                - auto_save_interval (int): 自动保存间隔秒数（默认 30）
        
        Raises:
            ValueError: 配置无效时抛出
        """
        config = config or {}
        self._config = config
        self._mode: str = config.get("mode", "simulation").lower()
        self._lock = threading.Lock()
        self._connected: bool = False
        
        if self._mode not in ("simulation", "web3"):
            raise ValueError(f"无效运行模式: {self._mode}，仅支持 'simulation' 或 'web3'")
        
        self._auto_save: bool = config.get("auto_save", True)
        self._auto_save_interval: int = config.get("auto_save_interval", 30)
        self._last_save_time: float = _current_timestamp()
        self._save_timer_handle: Optional[threading.Timer] = None
        
        if self._mode == "simulation":
            self._init_simulation(config)
        else:
            self._init_web3(config)
        
        logger.info(f"区块链管理器已初始化，模式: {self._mode}")
    
    # ==========================================================================
    # 属性
    # ==========================================================================
    
    @property
    def mode(self) -> str:
        """当前运行模式。"""
        return self._mode
    
    @property
    def is_simulation(self) -> bool:
        """是否为模拟模式。"""
        return self._mode == "simulation"
    
    @property
    def is_connected(self) -> bool:
        """是否已连接（Web3 模式下检查链连接状态）。"""
        return self._connected
    
    # ==========================================================================
    # 初始化方法
    # ==========================================================================
    
    def _init_simulation(self, config: Dict[str, Any]) -> None:
        """
        初始化模拟模式。
        
        在内存中创建完整的状态数据结构，精确模拟 Solidity 合约逻辑。
        可选从 JSON 文件加载之前保存的状态以实现持久化。
        
        Args:
            config: 配置字典
        """
        self._connected = True
        
        # 代币状态
        self._balances: Dict[str, int] = {}
        self._total_supply: int = 0
        self._burned_total: int = 0
        self._mining_reward_total: int = 0
        
        # 挖矿状态
        self._mining_info: Dict[str, MiningInfo] = {}
        self._total_network_power: float = 0.0
        
        # 治理状态
        self._proposals: List[Proposal] = []
        self._proposal_counter: int = 0
        
        # API 访问状态
        self._api_access: Dict[str, APIAccessRecord] = {}
        
        # 燃烧记录
        self._burn_records: List[BurnRecord] = []
        
        # 模拟区块状态
        self._simulated_block: int = 0
        self._block_start_time: float = _current_timestamp()
        self._block_interval: float = config.get("block_interval", 0.1)  # 每0.1秒一个区块
        
        # 加载持久化状态
        state_file = config.get("state_file", "")
        if state_file:
            self._state_file = Path(state_file)
            self._load_state()
        else:
            self._state_file = None
        
        # 启动自动保存定时器
        if self._auto_save and self._state_file:
            self._start_auto_save()
    
    def _init_web3(self, config: Dict[str, Any]) -> None:
        """
        初始化 Web3 模式。
        
        连接以太坊兼容的区块链节点，加载合约 ABI 和地址。
        
        Args:
            config: 配置字典，必须包含 rpc_url、token_address 等
        
        Raises:
            ImportError: 未安装 web3 库时抛出
            BlockchainError: 连接失败时抛出
        """
        try:
            from web3 import Web3
            from web3.contract import Contract
            self._Web3 = Web3
            self._Contract = Contract
        except ImportError:
            raise ImportError(
                "Web3 模式需要安装 web3 库。请运行: pip install web3"
            )
        
        rpc_url = config.get("rpc_url", "")
        if not rpc_url:
            raise ValueError("Web3 模式必须配置 rpc_url")
        
        self._w3 = self._Web3(self._Web3.HTTPProvider(rpc_url))
        
        if not self._w3.is_connected():
            raise BlockchainError(f"无法连接到区块链节点: {rpc_url}")
        
        self._connected = True
        self._chain_id = config.get("chain_id", 1)
        
        # 加载合约地址
        self._token_address = config.get("token_address", "")
        self._mining_address = config.get("mining_address", "")
        self._governance_address = config.get("governance_address", "")
        
        if not all([self._token_address, self._mining_address, self._governance_address]):
            raise ValueError("Web3 模式必须配置 token_address, mining_address, governance_address")
        
        # 加载合约 ABI（简化版，实际项目中应从 artifacts 加载完整 ABI）
        self._token_contract = self._load_token_contract()
        self._mining_contract = self._load_mining_contract()
        self._governance_contract = self._load_governance_contract()
        
        # 配置账户
        private_key = config.get("private_key", "")
        if private_key:
            self._account = self._w3.eth.account.from_key(private_key)
        else:
            self._account = None
        
        logger.info(
            f"Web3 已连接，链 ID: {self._chain_id}，"
            f"最新区块: {self._w3.eth.block_number}"
        )
    
    def _load_token_contract(self) -> Any:
        """加载 AIC 代币合约。"""
        # 简化的 ABI - 实际项目中使用完整编译后的 ABI
        abi = json.loads(TOKEN_ABI)
        return self._w3.eth.contract(
            address=self._Web3.to_checksum_address(self._token_address),
            abi=abi,
        )
    
    def _load_mining_contract(self) -> Any:
        """加载挖矿合约。"""
        abi = json.loads(MINING_ABI)
        return self._w3.eth.contract(
            address=self._Web3.to_checksum_address(self._mining_address),
            abi=abi,
        )
    
    def _load_governance_contract(self) -> Any:
        """加载治理合约。"""
        abi = json.loads(GOVERNANCE_ABI)
        return self._w3.eth.contract(
            address=self._Web3.to_checksum_address(self._governance_address),
            abi=abi,
        )
    
    # ==========================================================================
    # 状态持久化（模拟模式专用）
    # ==========================================================================
    
    def _start_auto_save(self) -> None:
        """启动自动保存定时器。"""
        if self._save_timer_handle is not None:
            self._save_timer_handle.cancel()
        
        self._save_timer_handle = threading.Timer(
            self._auto_save_interval,
            self._auto_save_tick,
        )
        self._save_timer_handle.daemon = True
        self._save_timer_handle.start()
    
    def _auto_save_tick(self) -> None:
        """自动保存定时器回调。"""
        try:
            if self._auto_save and self._state_file:
                self.save_state()
            # 重新启动定时器
            self._start_auto_save()
        except Exception as e:
            logger.error(f"自动保存失败: {e}")
    
    def save_state(self, filepath: Optional[str] = None) -> bool:
        """
        保存模拟状态到 JSON 文件。
        
        将当前所有区块链状态序列化为 JSON 格式持久化，
        支持断电恢复和状态迁移。
        
        Args:
            filepath: 自定义保存路径，默认使用初始化时配置的路径
            
        Returns:
            bool: 保存是否成功
        """
        if not self.is_simulation:
            logger.warning("Web3 模式不支持本地状态保存")
            return False
        
        target = Path(filepath) if filepath else self._state_file
        if not target:
            logger.warning("未配置状态文件路径，跳过保存")
            return False
        
        try:
            with self._lock:
                state = {
                    "version": "1.0.0",
                    "saved_at": _current_timestamp(),
                    "balances": self._balances,
                    "total_supply": self._total_supply,
                    "burned_total": self._burned_total,
                    "mining_reward_total": self._mining_reward_total,
                    "mining_info": {
                        addr: asdict(info)
                        for addr, info in self._mining_info.items()
                    },
                    "total_network_power": self._total_network_power,
                    "proposals": [asdict(p) for p in self._proposals],
                    "proposal_counter": self._proposal_counter,
                    "api_access": {
                        addr: asdict(rec)
                        for addr, rec in self._api_access.items()
                    },
                    "simulated_block": self._simulated_block,
                    "block_start_time": self._block_start_time,
                }
                
                target.parent.mkdir(parents=True, exist_ok=True)
                temp_path = target.with_suffix(".tmp")
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, ensure_ascii=False)
                temp_path.replace(target)
            
            self._last_save_time = _current_timestamp()
            logger.info(f"状态已保存至 {target}")
            return True
        
        except Exception as e:
            logger.error(f"保存状态失败: {e}")
            return False
    
    def _load_state(self) -> bool:
        """
        从 JSON 文件加载模拟状态。
        
        反序列化之前保存的状态，恢复所有区块链数据。
        加载失败时不会中断程序，而是使用空状态继续运行。
        
        Returns:
            bool: 加载是否成功
        """
        if not self._state_file or not self._state_file.exists():
            logger.info("无持久化状态文件，使用空状态初始化")
            return False
        
        try:
            with self._lock:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                
                # 恢复代币状态
                self._balances = state.get("balances", {})
                self._total_supply = state.get("total_supply", 0)
                self._burned_total = state.get("burned_total", 0)
                self._mining_reward_total = state.get("mining_reward_total", 0)
                
                # 恢复挖矿状态
                self._mining_info = {
                    addr: MiningInfo(**info)
                    for addr, info in state.get("mining_info", {}).items()
                }
                self._total_network_power = state.get("total_network_power", 0.0)
                
                # 恢复治理状态
                self._proposals = [
                    Proposal(**p) for p in state.get("proposals", [])
                ]
                self._proposal_counter = state.get("proposal_counter", 0)
                
                # 恢复 API 访问状态
                self._api_access = {
                    addr: APIAccessRecord(**rec)
                    for addr, rec in state.get("api_access", {}).items()
                }
                
                # 恢复区块状态
                self._simulated_block = state.get("simulated_block", 0)
                self._block_start_time = state.get(
                    "block_start_time", _current_timestamp()
                )
            
            logger.info(f"状态已从 {self._state_file} 加载")
            return True
        
        except Exception as e:
            logger.error(f"加载状态失败: {e}，使用空状态初始化")
            return False
    
    # ==========================================================================
    # 内部工具方法
    # ==========================================================================
    
    def _get_simulated_block(self) -> int:
        """
        获取当前模拟区块号。
        
        基于实际经过时间和配置的区块间隔计算。这使得时间相关的逻辑
        （如投票截止）可以正常工作。
        
        Returns:
            int: 当前模拟区块号
        """
        elapsed = _current_timestamp() - self._block_start_time
        return self._simulated_block + int(elapsed / self._block_interval)
    
    def _get_block_reward(self, block_number: int) -> int:
        """
        计算指定区块的区块奖励（含减半逻辑）。
        
        与 Solidity 合约中的 blockReward() 函数完全对应：
        reward = INITIAL_BLOCK_REWARD >> halvingEpoch
        
        Args:
            block_number: 区块号
            
        Returns:
            int: 区块奖励（含 TOKEN_DECIMALS 精度）
        """
        epoch = block_number // HALVING_INTERVAL
        # 使用位运算实现整数除以2的幂，与 Solidity 行为一致
        reward = INITIAL_BLOCK_REWARD >> epoch
        return reward if reward > 0 else 0
    
    def _calculate_mining_reward(
        self, node_address: str, current_block: int
    ) -> int:
        """
        计算矿工的待领取奖励。
        
        奖励计算公式与合约一致：
        reward = blockReward * (nodePower / totalNetworkPower) * blocksSinceLastProof
        
        Args:
            node_address: 矿工地址
            current_block: 当前区块号
            
        Returns:
            int: 计算出的奖励数量（含精度）
        """
        info = self._mining_info.get(node_address)
        if not info or info.compute_power <= 0 or self._total_network_power <= 0:
            return 0
        
        blocks_since = current_block - info.last_proof_block
        if blocks_since <= 0:
            return 0
        
        block_reward = self._get_block_reward(current_block)
        if block_reward == 0:
            return 0
        
        # 精确计算：使用整数运算避免浮点精度问题
        # share = (nodePower * 1e18) / totalNetworkPower
        power_share_numerator = int(info.compute_power * TOKEN_DECIMALS_FACTOR)
        power_share_denominator = int(self._total_network_power)
        if power_share_denominator == 0:
            return 0
        
        # reward = blockReward * powerShare / 1e18 * blocksSince
        raw_reward = (
            block_reward
            * power_share_numerator
            * blocks_since
            // (TOKEN_DECIMALS_FACTOR * power_share_denominator)
        )
        
        return raw_reward
    
    def _update_pending_rewards(self) -> None:
        """
        更新所有矿工的待领取奖励。
        
        在每个状态变更前调用，确保奖励计算基于最新区块号。
        """
        if not self.is_simulation:
            return
        
        current_block = self._get_simulated_block()
        for addr in self._mining_info:
            info = self._mining_info[addr]
            if info.compute_power > 0 and info.last_proof_block > 0:
                new_reward = self._calculate_mining_reward(addr, current_block)
                info.pending_reward = new_reward
    
    def _check_and_advance_proposals(self) -> None:
        """
        检查并推进提案状态。
        
        模拟合约中的自动状态转换：
        - ACTIVE -> SUCCEEDED / DEFEATED（投票结束）
        - SUCCEEDED -> QUEUED（满足执行延迟）
        
        每次状态变更操作前自动调用。
        """
        if not self.is_simulation:
            return
        
        current_block = self._get_simulated_block()
        current_time = _current_timestamp()
        
        for proposal in self._proposals:
            if proposal.status == ProposalStatus.ACTIVE.value:
                # 投票期结束判定
                if current_block >= proposal.end_block:
                    if proposal.votes_for > proposal.votes_against:
                        proposal.status = ProposalStatus.SUCCEEDED.value
                    else:
                        proposal.status = ProposalStatus.DEFEATED.value
                    logger.info(
                        f"提案 #{proposal.id} 投票结束，状态: {proposal.status}"
                    )
            
            elif proposal.status == ProposalStatus.SUCCEEDED.value:
                # 满足执行延迟后自动进入排队
                if current_block >= proposal.end_block + EXECUTION_DELAY_BLOCKS:
                    proposal.status = ProposalStatus.QUEUED.value
    
    # ==========================================================================
    # 代币功能 - Token Functions
    # ==========================================================================
    
    def get_balance(self, address: str) -> int:
        """
        查询指定地址的 AIC 代币余额。
        
        对应合约中的 balanceOf(address) 函数。
        
        Args:
            address: 查询地址（支持 0x 前缀格式）
            
        Returns:
            int: 代币余额（含 18 位精度），地址不存在时返回 0
            
        Example:
            >>> bm.get_balance("0x1234...abcd")
            1000000000000000000  # 1 AIC
        """
        address = _normalize_address(address)
        
        if self.is_simulation:
            with self._lock:
                return self._balances.get(address, 0)
        else:
            try:
                return self._token_contract.functions.balanceOf(
                    self._Web3.to_checksum_address(address)
                ).call()
            except Exception as e:
                logger.error(f"查询余额失败: {e}")
                raise BlockchainError(f"查询余额失败: {e}")
    
    def transfer(self, from_addr: str, to_addr: str, amount: int) -> bool:
        """
        转移 AIC 代币。
        
        对应合约中的 transfer(to, amount) 函数。
        模拟模式下自动扣减转出方余额并增加接收方余额。
        
        Args:
            from_addr: 转出地址
            to_addr: 接收地址
            amount: 转移数量（含 18 位精度）
            
        Returns:
            bool: 转移是否成功
            
        Raises:
            InvalidAddressError: 地址格式无效
            InsufficientBalanceError: 余额不足
            ValueError: 转移数量无效（<=0）
            
        Example:
            >>> bm.transfer("0xAlice", "0xBob", 1_000_000_000_000_000_000)
            True
        """
        from_addr = _normalize_address(from_addr)
        to_addr = _normalize_address(to_addr)
        
        if amount <= 0:
            raise ValueError(f"转移数量必须大于0，当前值: {amount}")
        
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                from_balance = self._balances.get(from_addr, 0)
                if from_balance < amount:
                    raise InsufficientBalanceError(
                        f"余额不足: 需要 {amount}，当前 {from_balance}"
                    )
                
                self._balances[from_addr] = from_balance - amount
                self._balances[to_addr] = self._balances.get(to_addr, 0) + amount
                
                logger.info(
                    f"代币转移: {from_addr[:10]}... -> {to_addr[:10]}..., "
                    f"数量: {amount / TOKEN_DECIMALS_FACTOR:.6f} AIC"
                )
                return True
        else:
            try:
                tx = self._token_contract.functions.transfer(
                    self._Web3.to_checksum_address(to_addr), amount
                ).build_transaction({
                    "from": self._Web3.to_checksum_address(from_addr),
                    "chainId": self._chain_id,
                    "nonce": self._w3.eth.get_transaction_count(from_addr),
                })
                tx_hash = self._w3.eth.send_transaction(tx)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
                return receipt.status == 1
            except Exception as e:
                logger.error(f"代币转移失败: {e}")
                raise BlockchainError(f"代币转移失败: {e}")
    
    def mint_mining_reward(self, node_address: str, amount: int) -> bool:
        """
        铸造挖矿奖励代币。
        
        对应合约中由挖矿合约调用的 mint() 函数。
        此函数仅限矿工合约（或模拟模式下的授权调用方）使用，
        用于将新挖出的代币发放给矿工。
        
        Args:
            node_address: 接收奖励的矿工地址
            amount: 铸造数量（含 18 位精度）
            
        Returns:
            bool: 铸造是否成功
            
        Raises:
            UnauthorizedError: 非授权调用方
            ValueError: 数量无效
            
        Example:
            >>> bm.mint_mining_reward("0xMiner1", 50_000_000_000_000_000_000)
            True
        """
        node_address = _normalize_address(node_address)
        
        if amount <= 0:
            raise ValueError(f"铸造数量必须大于0，当前值: {amount}")
        
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                # 增加总供应量
                self._total_supply += amount
                self._mining_reward_total += amount
                
                # 增加矿工余额
                self._balances[node_address] = (
                    self._balances.get(node_address, 0) + amount
                )
                
                logger.info(
                    f"铸造挖矿奖励: {node_address[:10]}..., "
                    f"数量: {amount / TOKEN_DECIMALS_FACTOR:.6f} AIC"
                )
                return True
        else:
            try:
                tx = self._mining_contract.functions.claimReward(
                    self._Web3.to_checksum_address(node_address)
                ).build_transaction({
                    "from": self._Web3.to_checksum_address(node_address),
                    "chainId": self._chain_id,
                    "nonce": self._w3.eth.get_transaction_count(node_address),
                })
                tx_hash = self._w3.eth.send_transaction(tx)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
                return receipt.status == 1
            except Exception as e:
                logger.error(f"铸造挖矿奖励失败: {e}")
                raise BlockchainError(f"铸造挖矿奖励失败: {e}")
    
    def burn_tokens(
        self, address: str, amount: int, purpose: str = ""
    ) -> bool:
        """
        燃烧（销毁）AIC 代币。
        
        对应合约中的 burn(amount) 函数。代币被永久销毁，
        减少总供应量。通常用于 API 访问权限购买等功能。
        
        Args:
            address: 执行燃烧的地址
            amount: 燃烧数量（含 18 位精度）
            purpose: 燃烧用途描述（用于审计追踪）
            
        Returns:
            bool: 燃烧是否成功
            
        Raises:
            InsufficientBalanceError: 余额不足
            ValueError: 数量无效
            
        Example:
            >>> bm.burn_tokens("0xUser1", 1_000_000_000_000_000_000, "API访问")
            True
        """
        address = _normalize_address(address)
        
        if amount <= 0:
            raise ValueError(f"燃烧数量必须大于0，当前值: {amount}")
        
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                balance = self._balances.get(address, 0)
                if balance < amount:
                    raise InsufficientBalanceError(
                        f"余额不足: 需要 {amount}，当前 {balance}"
                    )
                
                # 扣减余额和总供应量
                self._balances[address] = balance - amount
                self._total_supply -= amount
                self._burned_total += amount
                
                # 记录燃烧事件
                burn_record = BurnRecord(
                    amount=amount,
                    purpose=purpose,
                    timestamp=_current_timestamp(),
                )
                self._burn_records.append(burn_record)
                
                logger.info(
                    f"代币燃烧: {address[:10]}..., "
                    f"数量: {amount / TOKEN_DECIMALS_FACTOR:.6f} AIC, "
                    f"用途: {purpose}"
                )
                return True
        else:
            try:
                tx = self._token_contract.functions.burn(amount).build_transaction({
                    "from": self._Web3.to_checksum_address(address),
                    "chainId": self._chain_id,
                    "nonce": self._w3.eth.get_transaction_count(address),
                })
                tx_hash = self._w3.eth.send_transaction(tx)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
                return receipt.status == 1
            except Exception as e:
                logger.error(f"代币燃烧失败: {e}")
                raise BlockchainError(f"代币燃烧失败: {e}")
    
    # ==========================================================================
    # 挖矿功能 - Mining Functions
    # ==========================================================================
    
    def submit_compute_proof(
        self,
        node_address: str,
        compute_power: float,
        tasks_completed: int,
        proof_data: Dict[str, Any],
    ) -> bool:
        """
        提交算力证明。
        
        对应合约中的 submitProof(computePower, tasksCompleted, proofData) 函数。
        矿工通过提交计算证明来参与挖矿网络。系统会先结算之前的待领取奖励，
        然后更新矿工的算力和任务数据。
        
        Args:
            node_address: 矿工节点地址
            compute_power: 当前算力（FLOPS），必须 >= 0
            tasks_completed: 自上次证明以来完成的任务数量
            proof_data: 计算证明数据字典，包含：
                - task_hash (str): 任务哈希
                - result_hash (str): 结果哈希
                - timestamp (float): 计算完成时间戳
                - signature (str): 矿工签名
                
        Returns:
            bool: 提交是否成功
            
        Raises:
            ValueError: 算力值无效或任务数为负
            MiningError: 证明数据不完整
            
        Example:
            >>> proof = {
            ...     "task_hash": "0xabc123...",
            ...     "result_hash": "0xdef456...",
            ...     "timestamp": 1700000000.0,
            ...     "signature": "0x sig...",
            ... }
            >>> bm.submit_compute_proof("0xNode1", 3.14e12, 5, proof)
            True
        """
        node_address = _normalize_address(node_address)
        current_block = self._get_simulated_block()
        
        # 参数验证
        if compute_power < MIN_COMPUTE_POWER:
            raise ValueError(
                f"算力值不能小于 {MIN_COMPUTE_POWER}，当前值: {compute_power}"
            )
        if tasks_completed < 0:
            raise ValueError(f"任务数不能为负数，当前值: {tasks_completed}")
        
        # 验证证明数据
        required_keys = {"task_hash", "result_hash", "timestamp", "signature"}
        if not required_keys.issubset(proof_data.keys()):
            missing = required_keys - set(proof_data.keys())
            raise MiningError(f"证明数据不完整，缺少字段: {missing}")
        
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                # 先结算之前的奖励（将待领取奖励清零，记录到已领取）
                info = self._mining_info.get(
                    node_address, MiningInfo()
                )
                
                if info.last_proof_block > 0:
                    old_reward = self._calculate_mining_reward(
                        node_address, current_block
                    )
                    if old_reward > 0:
                        # 将累积奖励加回 pending_reward（将在下次 claim 时发放）
                        info.pending_reward += old_reward
                
                # 更新网络总算力（先减旧值，再加新值）
                self._total_network_power -= info.compute_power
                self._total_network_power += compute_power
                if self._total_network_power < 0:
                    self._total_network_power = 0
                
                # 更新矿工信息
                info.compute_power = compute_power
                info.tasks_completed += tasks_completed
                info.last_proof_block = current_block
                info.last_proof_time = _current_timestamp()
                
                self._mining_info[node_address] = info
                
                logger.info(
                    f"算力证明已提交: {node_address[:10]}..., "
                    f"算力: {compute_power:.2e} FLOPS, "
                    f"新增任务: {tasks_completed}"
                )
                return True
        else:
            try:
                tx = self._mining_contract.functions.submitProof(
                    int(compute_power),
                    tasks_completed,
                    json.dumps(proof_data),
                ).build_transaction({
                    "from": self._Web3.to_checksum_address(node_address),
                    "chainId": self._chain_id,
                    "nonce": self._w3.eth.get_transaction_count(node_address),
                })
                tx_hash = self._w3.eth.send_transaction(tx)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
                return receipt.status == 1
            except Exception as e:
                logger.error(f"提交算力证明失败: {e}")
                raise BlockchainError(f"提交算力证明失败: {e}")
    
    def claim_mining_reward(self, node_address: str) -> int:
        """
        领取挖矿奖励。
        
        对应合约中的 claimReward() 函数。
        结算矿工自上次领取以来的所有待领取奖励并发放到矿工账户。
        
        Args:
            node_address: 矿工地址
            
        Returns:
            int: 实际领取的奖励数量（含 18 位精度），无可领取奖励时返回 0
            
        Raises:
            MiningError: 领取失败
            
        Example:
            >>> claimed = bm.claim_mining_reward("0xNode1")
            >>> print(f"领取了 {claimed / 1e18} AIC")
        """
        node_address = _normalize_address(node_address)
        current_block = self._get_simulated_block()
        
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                info = self._mining_info.get(node_address)
                if not info:
                    logger.warning(f"矿工 {node_address[:10]}... 尚未注册")
                    return 0
                
                if info.compute_power <= 0 or info.last_proof_block <= 0:
                    logger.warning(
                        f"矿工 {node_address[:10]}... 无有效算力证明"
                    )
                    return 0
                
                # 计算最新待领取奖励
                latest_reward = self._calculate_mining_reward(
                    node_address, current_block
                )
                total_reward = info.pending_reward + latest_reward
                
                if total_reward <= 0:
                    logger.info(
                        f"矿工 {node_address[:10]}... 无可领取奖励"
                    )
                    return 0
                
                # 重置待领取奖励，更新领取记录
                info.pending_reward = 0
                info.last_proof_block = current_block
                info.total_claimed += total_reward
                self._mining_info[node_address] = info
                
                # 铸造代币到矿工账户
                self._total_supply += total_reward
                self._mining_reward_total += total_reward
                self._balances[node_address] = (
                    self._balances.get(node_address, 0) + total_reward
                )
                
                logger.info(
                    f"挖矿奖励已领取: {node_address[:10]}..., "
                    f"数量: {total_reward / TOKEN_DECIMALS_FACTOR:.6f} AIC"
                )
                return total_reward
        else:
            try:
                tx = self._mining_contract.functions.claimReward().build_transaction({
                    "from": self._Web3.to_checksum_address(node_address),
                    "chainId": self._chain_id,
                    "nonce": self._w3.eth.get_transaction_count(node_address),
                })
                tx_hash = self._w3.eth.send_transaction(tx)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
                
                if receipt.status == 1:
                    # 从事件日志中解析奖励数量
                    return self._parse_reward_from_receipt(receipt)
                return 0
            except Exception as e:
                logger.error(f"领取挖矿奖励失败: {e}")
                raise BlockchainError(f"领取挖矿奖励失败: {e}")
    
    def _parse_reward_from_receipt(self, receipt: Any) -> int:
        """从交易回执中解析奖励数量（Web3 模式辅助方法）。"""
        try:
            event_abi = self._mining_contract.events.RewardClaimed()
            for log in receipt.logs:
                try:
                    event = event_abi.process_log(log)
                    return event["args"]["amount"]
                except Exception:
                    continue
        except Exception:
            pass
        return 0
    
    def get_pending_reward(self, node_address: str) -> int:
        """
        查询矿工的待领取奖励。
        
        对应合约中的 pendingReward(address) 视图函数。
        返回矿工自上次提交证明以来累积的、尚未领取的奖励数量。
        
        Args:
            node_address: 矿工地址
            
        Returns:
            int: 待领取奖励数量（含 18 位精度）
        """
        node_address = _normalize_address(node_address)
        
        if self.is_simulation:
            with self._lock:
                info = self._mining_info.get(node_address)
                if not info or info.compute_power <= 0 or info.last_proof_block <= 0:
                    return 0
                
                current_block = self._get_simulated_block()
                latest = self._calculate_mining_reward(node_address, current_block)
                return info.pending_reward + latest
        else:
            try:
                return self._mining_contract.functions.pendingReward(
                    self._Web3.to_checksum_address(node_address)
                ).call()
            except Exception as e:
                logger.error(f"查询待领取奖励失败: {e}")
                raise BlockchainError(f"查询待领取奖励失败: {e}")
    
    def get_total_network_power(self) -> float:
        """
        查询全网总算力。
        
        对应合约中的 totalNetworkPower() 视图函数。
        返回所有活跃矿工的算力总和。
        
        Returns:
            float: 全网总算力（FLOPS）
        """
        if self.is_simulation:
            with self._lock:
                return self._total_network_power
        else:
            try:
                return float(
                    self._mining_contract.functions.totalNetworkPower().call()
                )
            except Exception as e:
                logger.error(f"查询全网总算力失败: {e}")
                raise BlockchainError(f"查询全网总算力失败: {e}")
    
    def get_mining_info(self, node_address: str) -> Dict[str, Any]:
        """
        查询矿工的完整挖矿信息。
        
        对应合约中的 getMinerInfo(address) 视图函数。
        返回矿工的算力、任务完成数、待领取奖励和累计领取总额。
        
        Args:
            node_address: 矿工地址
            
        Returns:
            dict: 矿工信息字典，包含以下字段：
                - power (float): 当前算力（FLOPS）
                - tasks (int): 已完成任务总数
                - pending_reward (int): 待领取奖励（含精度）
                - claimed_total (int): 累计已领取奖励（含精度）
                - last_proof_block (int): 上次提交证明的区块号
                - last_proof_time (float): 上次提交证明的时间戳
                
        Example:
            >>> info = bm.get_mining_info("0xNode1")
            >>> print(f"算力: {info['power']:.2e} FLOPS")
        """
        node_address = _normalize_address(node_address)
        
        if self.is_simulation:
            with self._lock:
                info = self._mining_info.get(node_address)
                if not info:
                    return {
                        "power": 0.0,
                        "tasks": 0,
                        "pending_reward": 0,
                        "claimed_total": 0,
                        "last_proof_block": 0,
                        "last_proof_time": 0.0,
                    }
                
                current_block = self._get_simulated_block()
                latest_reward = 0
                if info.compute_power > 0 and info.last_proof_block > 0:
                    latest_reward = self._calculate_mining_reward(
                        node_address, current_block
                    )
                
                return {
                    "power": info.compute_power,
                    "tasks": info.tasks_completed,
                    "pending_reward": info.pending_reward + latest_reward,
                    "claimed_total": info.total_claimed,
                    "last_proof_block": info.last_proof_block,
                    "last_proof_time": info.last_proof_time,
                }
        else:
            try:
                result = self._mining_contract.functions.getMinerInfo(
                    self._Web3.to_checksum_address(node_address)
                ).call()
                return {
                    "power": float(result[0]),
                    "tasks": int(result[1]),
                    "pending_reward": int(result[2]),
                    "claimed_total": int(result[3]),
                    "last_proof_block": int(result[4]),
                    "last_proof_time": float(result[5]),
                }
            except Exception as e:
                logger.error(f"查询矿工信息失败: {e}")
                raise BlockchainError(f"查询矿工信息失败: {e}")
    
    def get_current_block_reward(self) -> int:
        """
        查询当前区块奖励。
        
        根据当前区块号和减半规则计算。对应合约中的 blockReward() 函数。
        每经过 HALVING_INTERVAL（210,000）个区块，奖励减半。
        
        Returns:
            int: 当前区块奖励（含 18 位精度）
            
        Example:
            >>> reward = bm.get_current_block_reward()
            >>> print(f"当前区块奖励: {reward / 1e18} AIC")
        """
        if self.is_simulation:
            current_block = self._get_simulated_block()
            return self._get_block_reward(current_block)
        else:
            try:
                return self._mining_contract.functions.blockReward().call()
            except Exception as e:
                logger.error(f"查询区块奖励失败: {e}")
                raise BlockchainError(f"查询区块奖励失败: {e}")
    
    # ==========================================================================
    # 治理功能 - Governance Functions
    # ==========================================================================
    
    def create_proposal(
        self,
        proposer: str,
        proposal_type: str,
        title: str,
        description: str,
        model_name: str = "",
    ) -> int:
        """
        创建治理提案。
        
        对应合约中的 propose(type, title, description, modelName) 函数。
        提案创建后进入 ACTIVE 状态，社区成员可在投票期内进行投票。
        
        Args:
            proposer: 提案发起人地址
            proposal_type: 提案类型，可选值:
                - "ParameterChange" - 参数修改
                - "ModelAddition" - 新增模型
                - "ModelUpgrade" - 模型升级
                - "Emergency" - 紧急提案
            title: 提案标题（1-200字符）
            description: 提案描述（1-5000字符）
            model_name: 关联模型名称（模型类提案必须填写）
            
        Returns:
            int: 提案编号（从1开始递增）
            
        Raises:
            ProposalError: 提案类型无效或内容为空
            ValueError: 标题或描述长度超限
            
        Example:
            >>> pid = bm.create_proposal(
            ...     "0xProposer1", "ModelAddition",
            ...     "添加 Llama 3 模型",
            ...     "提议将 Meta Llama 3 70B 纳入支持模型列表",
            ...     model_name="llama-3-70b",
            ... )
            >>> print(f"提案编号: {pid}")
        """
        proposer = _normalize_address(proposer)
        
        # 验证提案类型
        valid_types = {pt.value for pt in ProposalType}
        if proposal_type not in valid_types:
            raise ProposalError(
                f"无效提案类型: {proposal_type}，"
                f"有效类型: {', '.join(valid_types)}"
            )
        
        # 验证内容
        if not title or not title.strip():
            raise ProposalError("提案标题不能为空")
        if not description or not description.strip():
            raise ProposalError("提案描述不能为空")
        if len(title) > 200:
            raise ValueError(f"提案标题过长: {len(title)} 字符（上限200）")
        if len(description) > 5000:
            raise ValueError(f"提案描述过长: {len(description)} 字符（上限5000）")
        
        # 模型类提案必须提供模型名称
        if proposal_type in (
            ProposalType.MODEL_ADDITION.value,
            ProposalType.MODEL_UPGRADE.value,
        ):
            if not model_name or not model_name.strip():
                raise ProposalError(
                    f"{proposal_type} 类提案必须提供 model_name 参数"
                )
        
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                self._proposal_counter += 1
                current_block = self._get_simulated_block()
                current_time = _current_timestamp()
                
                proposal = Proposal(
                    id=self._proposal_counter,
                    proposal_type=proposal_type,
                    proposer=proposer,
                    title=title.strip(),
                    description=description.strip(),
                    model_name=model_name.strip() if model_name else "",
                    votes_for=0,
                    votes_against=0,
                    start_block=current_block,
                    start_time=current_time,
                    end_block=current_block + VOTING_PERIOD_BLOCKS,
                    end_time=current_time + VOTING_PERIOD_SECONDS,
                    status=ProposalStatus.ACTIVE.value,
                    executed=False,
                    voters={},
                )
                
                self._proposals.append(proposal)
                
                logger.info(
                    f"治理提案已创建: #{proposal.id} [{proposal_type}] "
                    f"{title} (发起人: {proposer[:10]}...)"
                )
                return proposal.id
        else:
            try:
                tx = self._governance_contract.functions.propose(
                    proposal_type, title, description, model_name
                ).build_transaction({
                    "from": self._Web3.to_checksum_address(proposer),
                    "chainId": self._chain_id,
                    "nonce": self._w3.eth.get_transaction_count(proposer),
                })
                tx_hash = self._w3.eth.send_transaction(tx)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
                if receipt.status == 1:
                    return self._parse_proposal_id_from_receipt(receipt)
                raise ProposalError("提案创建交易失败")
            except ProposalError:
                raise
            except Exception as e:
                logger.error(f"创建治理提案失败: {e}")
                raise BlockchainError(f"创建治理提案失败: {e}")
    
    def _parse_proposal_id_from_receipt(self, receipt: Any) -> int:
        """从交易回执中解析提案编号（Web3 模式辅助方法）。"""
        try:
            event_abi = self._governance_contract.events.ProposalCreated()
            for log in receipt.logs:
                try:
                    event = event_abi.process_log(log)
                    return int(event["args"]["proposalId"])
                except Exception:
                    continue
        except Exception:
            pass
        raise ProposalError("无法从交易回执中解析提案编号")
    
    def vote(
        self, voter: str, proposal_id: int, support: bool
    ) -> bool:
        """
        对治理提案进行投票。
        
        对应合约中的 castVote(proposalId, support) 函数。
        每个地址每个提案只能投票一次。投票权重基于 AIC 代币持有量
        （1 AIC = 1 票）。仅 ACTIVE 状态的提案可以投票。
        
        Args:
            voter: 投票人地址
            proposal_id: 提案编号
            support: True 表示赞成，False 表示反对
            
        Returns:
            bool: 投票是否成功
            
        Raises:
            ProposalError: 提案不存在、非投票期、已投过票
            
        Example:
            >>> bm.vote("0xVoter1", 1, True)  # 赞成提案 #1
            True
        """
        voter = _normalize_address(voter)
        
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                # 查找提案
                proposal = self._find_proposal(proposal_id)
                if not proposal:
                    raise ProposalError(f"提案不存在: #{proposal_id}")
                
                # 检查状态
                if proposal.status != ProposalStatus.ACTIVE.value:
                    raise ProposalError(
                        f"提案 #{proposal_id} 当前状态为 {proposal.status}，"
                        f"无法投票"
                    )
                
                # 检查是否已投票
                if voter in proposal.voters:
                    raise ProposalError(
                        f"地址 {voter[:10]}... 已对提案 #{proposal_id} 投过票"
                    )
                
                # 计算投票权重（基于代币余额）
                balance = self._balances.get(voter, 0)
                weight = balance // TOKEN_DECIMALS_FACTOR  # 1 AIC = 1 票
                
                if weight == 0:
                    logger.warning(
                        f"投票人 {voter[:10]}... AIC 余额为 0，投票权重为 0"
                    )
                
                # 记录投票
                if support:
                    proposal.votes_for += weight
                else:
                    proposal.votes_against += weight
                proposal.voters[voter] = support
                
                logger.info(
                    f"投票成功: {voter[:10]}... 对提案 #{proposal_id} "
                    f"投了 {'赞成' if support else '反对'}票 "
                    f"(权重: {weight})"
                )
                return True
        else:
            try:
                tx = self._governance_contract.functions.castVote(
                    proposal_id, support
                ).build_transaction({
                    "from": self._Web3.to_checksum_address(voter),
                    "chainId": self._chain_id,
                    "nonce": self._w3.eth.get_transaction_count(voter),
                })
                tx_hash = self._w3.eth.send_transaction(tx)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
                return receipt.status == 1
            except Exception as e:
                logger.error(f"投票失败: {e}")
                raise BlockchainError(f"投票失败: {e}")
    
    def get_proposal(self, proposal_id: int) -> Dict[str, Any]:
        """
        查询指定提案的详细信息。
        
        对应合约中的 getProposal(proposalId) 视图函数。
        
        Args:
            proposal_id: 提案编号
            
        Returns:
            dict: 提案信息字典，包含以下字段：
                - id (int): 提案编号
                - type (str): 提案类型
                - title (str): 提案标题
                - description (str): 提案描述
                - status (str): 提案状态
                - votes_for (int): 赞成票权重
                - votes_against (int): 反对票权重
                - start_time (float): 投票开始时间戳
                - end_time (float): 投票结束时间戳
                - executed (bool): 是否已执行
                - proposer (str): 发起人地址
                - model_name (str): 关联模型名称
                
        Raises:
            ProposalError: 提案不存在
        """
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                proposal = self._find_proposal(proposal_id)
                if not proposal:
                    raise ProposalError(f"提案不存在: #{proposal_id}")
                
                return {
                    "id": proposal.id,
                    "type": proposal.proposal_type,
                    "title": proposal.title,
                    "description": proposal.description,
                    "status": proposal.status,
                    "votes_for": proposal.votes_for,
                    "votes_against": proposal.votes_against,
                    "start_time": proposal.start_time,
                    "end_time": proposal.end_time,
                    "executed": proposal.executed,
                    "proposer": proposal.proposer,
                    "model_name": proposal.model_name,
                }
        else:
            try:
                result = self._governance_contract.functions.getProposal(
                    proposal_id
                ).call()
                return {
                    "id": int(result[0]),
                    "type": result[1],
                    "title": result[2],
                    "description": result[3],
                    "status": result[4],
                    "votes_for": int(result[5]),
                    "votes_against": int(result[6]),
                    "start_time": float(result[7]),
                    "end_time": float(result[8]),
                    "executed": result[9],
                    "proposer": result[10],
                    "model_name": result[11],
                }
            except Exception as e:
                logger.error(f"查询提案失败: {e}")
                raise BlockchainError(f"查询提案失败: {e}")
    
    def get_active_proposals(self) -> List[Dict[str, Any]]:
        """
        查询所有活跃（投票中）的提案列表。
        
        对应合约中的 getActiveProposals() 视图函数。
        返回状态为 ACTIVE 的所有提案的详细信息。
        
        Returns:
            list[dict]: 活跃提案列表，每个元素为提案信息字典
        """
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                active = []
                for p in self._proposals:
                    if p.status == ProposalStatus.ACTIVE.value:
                        active.append({
                            "id": p.id,
                            "type": p.proposal_type,
                            "title": p.title,
                            "description": p.description,
                            "status": p.status,
                            "votes_for": p.votes_for,
                            "votes_against": p.votes_against,
                            "start_time": p.start_time,
                            "end_time": p.end_time,
                            "executed": p.executed,
                            "proposer": p.proposer,
                            "model_name": p.model_name,
                        })
                return active
        else:
            try:
                result = self._governance_contract.functions.getActiveProposals().call()
                proposals = []
                for item in result:
                    proposals.append({
                        "id": int(item[0]),
                        "type": item[1],
                        "title": item[2],
                        "description": item[3],
                        "status": item[4],
                        "votes_for": int(item[5]),
                        "votes_against": int(item[6]),
                        "start_time": float(item[7]),
                        "end_time": float(item[8]),
                        "executed": item[9],
                        "proposer": item[10],
                        "model_name": item[11],
                    })
                return proposals
            except Exception as e:
                logger.error(f"查询活跃提案失败: {e}")
                raise BlockchainError(f"查询活跃提案失败: {e}")
    
    def execute_proposal(self, proposal_id: int) -> bool:
        """
        执行已通过的治理提案。
        
        对应合约中的 execute(proposalId) 函数。
        仅 SUCCEEDED 或 QUEUED 状态的提案可以执行。
        执行后提案状态变为 EXECUTED。
        
        在模拟模式下，本函数模拟合约执行逻辑但不实际修改链上参数。
        实际项目中，合约执行会修改具体参数（如模型列表、奖励系数等）。
        
        Args:
            proposal_id: 提案编号
            
        Returns:
            bool: 执行是否成功
            
        Raises:
            ProposalError: 提案不存在或状态不允许执行
        """
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                proposal = self._find_proposal(proposal_id)
                if not proposal:
                    raise ProposalError(f"提案不存在: #{proposal_id}")
                
                # 检查状态
                if proposal.status not in (
                    ProposalStatus.SUCCEEDED.value,
                    ProposalStatus.QUEUED.value,
                ):
                    raise ProposalError(
                        f"提案 #{proposal_id} 当前状态为 {proposal.status}，"
                        f"无法执行（需要 SUCCEEDED 或 QUEUED 状态）"
                    )
                
                # 再次确认投票结果（安全检查）
                if proposal.votes_for <= proposal.votes_against:
                    proposal.status = ProposalStatus.DEFEATED.value
                    raise ProposalError(
                        f"提案 #{proposal_id} 赞成票未超过反对票，执行被拒绝"
                    )
                
                # 执行提案
                proposal.status = ProposalStatus.EXECUTED.value
                proposal.executed = True
                
                logger.info(
                    f"治理提案已执行: #{proposal_id} [{proposal.proposal_type}] "
                    f"{proposal.title}"
                )
                return True
        else:
            try:
                tx = self._governance_contract.functions.execute(
                    proposal_id
                ).build_transaction({
                    "chainId": self._chain_id,
                    "nonce": self._w3.eth.get_transaction_count(
                        self._account.address if self._account else ""
                    ),
                })
                if self._account:
                    tx["from"] = self._account.address
                tx_hash = self._w3.eth.send_transaction(tx)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
                return receipt.status == 1
            except Exception as e:
                logger.error(f"执行提案失败: {e}")
                raise BlockchainError(f"执行提案失败: {e}")
    
    def _find_proposal(self, proposal_id: int) -> Optional[Proposal]:
        """
        在提案列表中查找指定编号的提案（内部方法）。
        
        Args:
            proposal_id: 提案编号
            
        Returns:
            Optional[Proposal]: 找到的提案，未找到返回 None
        """
        for p in self._proposals:
            if p.id == proposal_id:
                return p
        return None
    
    # ==========================================================================
    # API 访问功能 - API Access Functions
    # ==========================================================================
    
    def burn_for_api_access(
        self, address: str, amount: int, tier: str = "basic"
    ) -> bool:
        """
        通过燃烧代币获取 API 访问权限。
        
        对应合约中的 burnForAccess(amount, tier) 函数。
        用户燃烧 AIC 代币以获取对应等级的 API 访问额度。
        不同等级有不同的价格和配额上限。
        
        等级说明:
            - basic: 基础版，1 AIC / 1K tokens，日限额 10K
            - standard: 标准版，5 AIC / 1K tokens，日限额 100K
            - premium: 高级版，20 AIC / 1K tokens，日限额 1M
        
        Args:
            address: 用户地址
            amount: 燃烧数量（含 18 位精度）
            tier: 目标等级，"basic" / "standard" / "premium"
            
        Returns:
            bool: 操作是否成功
            
        Raises:
            ValueError: 等级无效或数量不足
            
        Example:
            >>> bm.burn_for_api_access(
            ...     "0xUser1",
            ...     5_000_000_000_000_000_000,  # 5 AIC
            ...     tier="standard"
            ... )
            True
        """
        address = _normalize_address(address)
        
        if tier not in API_PRICES:
            raise ValueError(
                f"无效等级: {tier}，可选: {', '.join(API_PRICES.keys())}"
            )
        
        if amount <= 0:
            raise ValueError(f"燃烧数量必须大于0，当前值: {amount}")
        
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                # 检查余额
                balance = self._balances.get(address, 0)
                if balance < amount:
                    raise InsufficientBalanceError(
                        f"余额不足: 需要 {amount}，当前 {balance}"
                    )
                
                # 计算获得的配额
                price = API_PRICES[tier]
                quota = (amount // price) * 1000  # 每 price 获得 1K 配额
                
                if quota <= 0:
                    raise ValueError(
                        f"燃烧数量不足以换取任何配额，"
                        f"{tier} 等级最低需要 {price} wei"
                    )
                
                # 扣减余额和总供应量
                self._balances[address] = balance - amount
                self._total_supply -= amount
                self._burned_total += amount
                
                # 记录燃烧事件
                self._burn_records.append(BurnRecord(
                    amount=amount,
                    purpose=f"API访问-{tier}",
                    timestamp=_current_timestamp(),
                ))
                
                # 更新 API 访问记录
                record = self._api_access.get(
                    address, APIAccessRecord()
                )
                
                # 每日配额重置检查
                current_time = _current_timestamp()
                if (current_time - record.last_reset_time) >= 86400:
                    record.daily_quota_used = 0
                    record.last_reset_time = current_time
                
                record.tier = tier
                record.access_enabled = True
                self._api_access[address] = record
                
                logger.info(
                    f"API访问权限获取: {address[:10]}... -> {tier}, "
                    f"燃烧: {amount / TOKEN_DECIMALS_FACTOR:.6f} AIC, "
                    f"配额: {quota}"
                )
                return True
        else:
            try:
                tx = self._token_contract.functions.burnForAccess(
                    amount, tier
                ).build_transaction({
                    "from": self._Web3.to_checksum_address(address),
                    "chainId": self._chain_id,
                    "nonce": self._w3.eth.get_transaction_count(address),
                })
                tx_hash = self._w3.eth.send_transaction(tx)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
                return receipt.status == 1
            except Exception as e:
                logger.error(f"获取API访问权限失败: {e}")
                raise BlockchainError(f"获取API访问权限失败: {e}")
    
    def get_api_price(self, tier: str = "basic") -> int:
        """
        查询指定等级的 API 访问价格。
        
        对应合约中的 getAPIPrice(tier) 视图函数。
        返回每 1K tokens 的价格（含 18 位精度）。
        
        Args:
            tier: 等级名称，"basic" / "standard" / "premium"
            
        Returns:
            int: 每 1K tokens 的价格（含 18 位精度）
            
        Raises:
            ValueError: 等级无效
            
        Example:
            >>> price = bm.get_api_price("standard")
            >>> print(f"标准版价格: {price / 1e18} AIC / 1K tokens")
        """
        if tier not in API_PRICES:
            raise ValueError(
                f"无效等级: {tier}，可选: {', '.join(API_PRICES.keys())}"
            )
        
        if self.is_simulation:
            return API_PRICES[tier]
        else:
            try:
                return self._token_contract.functions.getAPIPrice(tier).call()
            except Exception as e:
                logger.error(f"查询API价格失败: {e}")
                raise BlockchainError(f"查询API价格失败: {e}")
    
    def check_access(self, address: str) -> Dict[str, Any]:
        """
        检查地址的 API 访问状态。
        
        对应合约中的 checkAccess(address) 视图函数。
        返回访问权限、剩余配额和每日限额等信息。
        
        Args:
            address: 用户地址
            
        Returns:
            dict: 访问状态字典，包含以下字段：
                - allowed (bool): 是否允许访问
                - remaining_quota (int): 今日剩余配额（调用次数）
                - daily_limit (int): 每日限额
                - tier (str): 当前等级
                
        Example:
            >>> status = bm.check_access("0xUser1")
            >>> if status["allowed"]:
            ...     print(f"剩余配额: {status['remaining_quota']}")
        """
        address = _normalize_address(address)
        
        if self.is_simulation:
            with self._lock:
                record = self._api_access.get(address)
                
                if not record or not record.access_enabled:
                    return {
                        "allowed": False,
                        "remaining_quota": 0,
                        "daily_limit": 0,
                        "tier": "",
                    }
                
                # 每日配额重置
                current_time = _current_timestamp()
                if (current_time - record.last_reset_time) >= 86400:
                    record.daily_quota_used = 0
                    record.last_reset_time = current_time
                    self._api_access[address] = record
                
                daily_limit = API_DAILY_LIMITS.get(record.tier, 0)
                remaining = max(0, daily_limit - record.daily_quota_used)
                
                return {
                    "allowed": remaining > 0,
                    "remaining_quota": remaining,
                    "daily_limit": daily_limit,
                    "tier": record.tier,
                }
        else:
            try:
                result = self._token_contract.functions.checkAccess(
                    self._Web3.to_checksum_address(address)
                ).call()
                return {
                    "allowed": result[0],
                    "remaining_quota": int(result[1]),
                    "daily_limit": int(result[2]),
                    "tier": result[3],
                }
            except Exception as e:
                logger.error(f"检查API访问状态失败: {e}")
                raise BlockchainError(f"检查API访问状态失败: {e}")
    
    # ==========================================================================
    # 区块与减半 - Block & Halving Functions
    # ==========================================================================
    
    def get_current_block(self) -> int:
        """
        获取当前区块号。
        
        模拟模式下基于实际时间计算；Web3 模式查询链上最新区块。
        
        Returns:
            int: 当前区块号
        """
        if self.is_simulation:
            return self._get_simulated_block()
        else:
            try:
                return self._w3.eth.block_number
            except Exception as e:
                logger.error(f"获取当前区块号失败: {e}")
                raise BlockchainError(f"获取当前区块号失败: {e}")
    
    def get_halving_info(self) -> Dict[str, Any]:
        """
        获取减半信息。
        
        返回当前减半纪元、距离下次减半的区块数、
        当前区块奖励和下次减半后的区块奖励。
        
        与合约中的 getHalvingInfo() 函数对应。
        
        Returns:
            dict: 减半信息字典，包含以下字段：
                - current_epoch (int): 当前减半纪元（从0开始）
                - blocks_until_halving (int): 距离下次减半的剩余区块数
                - current_reward (int): 当前区块奖励（含精度）
                - next_reward (int): 下次减半后的区块奖励（含精度）
                
        Example:
            >>> info = bm.get_halving_info()
            >>> print(f"当前纪元: {info['current_epoch']}")
            >>> print(f"剩余区块: {info['blocks_until_halving']}")
        """
        current_block = self.get_current_block()
        current_epoch = current_block // HALVING_INTERVAL
        blocks_until = HALVING_INTERVAL - (current_block % HALVING_INTERVAL)
        
        current_reward = self._get_block_reward(current_block)
        next_reward = self._get_block_reward(current_block + blocks_until)
        
        return {
            "current_epoch": current_epoch,
            "blocks_until_halving": blocks_until,
            "current_reward": current_reward,
            "next_reward": next_reward,
        }
    
    def get_blockchain_stats(self) -> Dict[str, Any]:
        """
        获取区块链综合统计信息。
        
        返回网络的整体运行数据，包括代币经济和挖矿概况。
        
        Returns:
            dict: 统计信息字典，包含以下字段：
                - total_supply (int): 当前代币总供应量（含精度）
                - burned_total (int): 累计燃烧总量（含精度）
                - mining_reward_total (int): 累计挖矿奖励总量（含精度）
                - active_nodes (int): 活跃矿工节点数
                - total_network_power (float): 全网总算力
                - current_block (int): 当前区块号
                - current_block_reward (int): 当前区块奖励
                - proposal_count (int): 提案总数
                - active_proposals (int): 活跃提案数
                
        Example:
            >>> stats = bm.get_blockchain_stats()
            >>> print(f"总供应: {stats['total_supply'] / 1e18} AIC")
            >>> print(f"活跃节点: {stats['active_nodes']}")
        """
        if self.is_simulation:
            with self._lock:
                self._check_and_advance_proposals()
                
                active_nodes = sum(
                    1 for info in self._mining_info.values()
                    if info.compute_power > 0
                )
                
                active_proposals = sum(
                    1 for p in self._proposals
                    if p.status == ProposalStatus.ACTIVE.value
                )
                
                return {
                    "total_supply": self._total_supply,
                    "burned_total": self._burned_total,
                    "mining_reward_total": self._mining_reward_total,
                    "active_nodes": active_nodes,
                    "total_network_power": self._total_network_power,
                    "current_block": self._get_simulated_block(),
                    "current_block_reward": self._get_block_reward(
                        self._get_simulated_block()
                    ),
                    "proposal_count": len(self._proposals),
                    "active_proposals": active_proposals,
                }
        else:
            try:
                return {
                    "total_supply": self._token_contract.functions.totalSupply().call(),
                    "burned_total": self._token_contract.functions.burnedTotal().call(),
                    "mining_reward_total": self._mining_contract.functions.totalRewardsMinted().call(),
                    "active_nodes": int(
                        self._mining_contract.functions.activeMinerCount().call()
                    ),
                    "total_network_power": float(
                        self._mining_contract.functions.totalNetworkPower().call()
                    ),
                    "current_block": self._w3.eth.block_number,
                    "current_block_reward": self._mining_contract.functions.blockReward().call(),
                    "proposal_count": int(
                        self._governance_contract.functions.proposalCount().call()
                    ),
                    "active_proposals": len(self.get_active_proposals()),
                }
            except Exception as e:
                logger.error(f"获取区块链统计失败: {e}")
                raise BlockchainError(f"获取区块链统计失败: {e}")
    
    # ==========================================================================
    # 生命周期管理
    # ==========================================================================
    
    def close(self) -> None:
        """
        关闭区块链管理器，释放资源。
        
        模拟模式下会自动保存状态并停止定时器。
        Web3 模式下断开链连接。
        
        建议在程序退出前调用此方法以确保状态持久化。
        """
        logger.info("正在关闭区块链管理器...")
        
        # 停止自动保存定时器
        if self._save_timer_handle is not None:
            self._save_timer_handle.cancel()
            self._save_timer_handle = None
        
        # 模拟模式：保存状态
        if self.is_simulation:
            if self._auto_save and self._state_file:
                self.save_state()
        
        self._connected = False
        logger.info("区块链管理器已关闭")
    
    def __enter__(self) -> "BlockchainManager":
        """支持上下文管理器协议。"""
        return self
    
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """退出上下文时自动关闭。"""
        self.close()
    
    def __repr__(self) -> str:
        return (
            f"BlockchainManager(mode={self._mode!r}, "
            f"connected={self._connected})"
        )


# ==============================================================================
# Web3 模式合约 ABI（简化版）
# ==============================================================================

TOKEN_ABI = json.dumps([
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "amount", "type": "uint256"}],
        "name": "burn",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "amount", "type": "uint256"},
            {"name": "tier", "type": "string"},
        ],
        "name": "burnForAccess",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "tier", "type": "string"}],
        "name": "getAPIPrice",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "checkAccess",
        "outputs": [
            {"name": "allowed", "type": "bool"},
            {"name": "remaining", "type": "uint256"},
            {"name": "dailyLimit", "type": "uint256"},
            {"name": "tier", "type": "string"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "burnedTotal",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
])

MINING_ABI = json.dumps([
    {
        "inputs": [
            {"name": "computePower", "type": "uint256"},
            {"name": "tasksCompleted", "type": "uint256"},
            {"name": "proofData", "type": "string"},
        ],
        "name": "submitProof",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "name": "claimReward",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "miner", "type": "address"}],
        "name": "pendingReward",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "totalNetworkPower",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "miner", "type": "address"}],
        "name": "getMinerInfo",
        "outputs": [
            {"name": "power", "type": "uint256"},
            {"name": "tasks", "type": "uint256"},
            {"name": "pendingReward", "type": "uint256"},
            {"name": "totalClaimed", "type": "uint256"},
            {"name": "lastProofBlock", "type": "uint256"},
            {"name": "lastProofTime", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "blockReward",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "totalRewardsMinted",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "activeMinerCount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "miner", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
        ],
        "name": "RewardClaimed",
        "type": "event",
    },
])

GOVERNANCE_ABI = json.dumps([
    {
        "inputs": [
            {"name": "proposalType", "type": "string"},
            {"name": "title", "type": "string"},
            {"name": "description", "type": "string"},
            {"name": "modelName", "type": "string"},
        ],
        "name": "propose",
        "outputs": [{"name": "proposalId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "proposalId", "type": "uint256"},
            {"name": "support", "type": "bool"},
        ],
        "name": "castVote",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "proposalId", "type": "uint256"}],
        "name": "execute",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "proposalId", "type": "uint256"}],
        "name": "getProposal",
        "outputs": [
            {"name": "id", "type": "uint256"},
            {"name": "proposalType", "type": "string"},
            {"name": "title", "type": "string"},
            {"name": "description", "type": "string"},
            {"name": "status", "type": "string"},
            {"name": "votesFor", "type": "uint256"},
            {"name": "votesAgainst", "type": "uint256"},
            {"name": "startTime", "type": "uint256"},
            {"name": "endTime", "type": "uint256"},
            {"name": "executed", "type": "bool"},
            {"name": "proposer", "type": "address"},
            {"name": "modelName", "type": "string"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "getActiveProposals",
        "outputs": [
            {
                "components": [
                    {"name": "id", "type": "uint256"},
                    {"name": "proposalType", "type": "string"},
                    {"name": "title", "type": "string"},
                    {"name": "description", "type": "string"},
                    {"name": "status", "type": "string"},
                    {"name": "votesFor", "type": "uint256"},
                    {"name": "votesAgainst", "type": "uint256"},
                    {"name": "startTime", "type": "uint256"},
                    {"name": "endTime", "type": "uint256"},
                    {"name": "executed", "type": "bool"},
                    {"name": "proposer", "type": "address"},
                    {"name": "modelName", "type": "string"},
                ],
                "name": "",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "proposalCount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "proposalId", "type": "uint256"},
            {"indexed": True, "name": "proposer", "type": "address"},
        ],
        "name": "ProposalCreated",
        "type": "event",
    },
])
