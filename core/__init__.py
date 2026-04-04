"""
AICoin Core - 核心模块
======================

本包包含 AICoin 去中心化 AI 算力挖矿网络的核心功能模块。

主要导出:
    - BlockchainManager: 区块链管理器，支持模拟模式和 Web3 模式
    - ProposalStatus: 提案状态枚举
    - ProposalType: 提案类型枚举
    - MiningInfo: 矿工挖矿信息数据类
    - Proposal: 治理提案数据类
    - BlockchainError: 区块链操作基础异常

使用示例:
    >>> from aicoin.core import BlockchainManager
    >>> bm = BlockchainManager({"mode": "simulation"})
    >>> bm.get_blockchain_stats()
"""

from .router import (
    RoutingConfig,
    RoutingStrategy,
    NodeStatus,
    RequestPriority,
    NodeProfile,
    NodeRegistry,
    LatencyProbe,
    OptimalRouter,
    RequestTracker,
    create_routing_system,
)

from .blockchain import (
    # 主类
    BlockchainManager,
    # 枚举
    ProposalStatus,
    ProposalType,
    # 数据类
    MiningInfo,
    Proposal,
    APIAccessRecord,
    BurnRecord,
    # 异常
    BlockchainError,
    InsufficientBalanceError,
    InvalidAddressError,
    UnauthorizedError,
    ProposalError,
    MiningError,
    # 常量
    TOKEN_DECIMALS,
    TOKEN_DECIMALS_FACTOR,
    INITIAL_BLOCK_REWARD,
    HALVING_INTERVAL,
    API_PRICES,
    BURN_ADDRESS,
)

__all__ = [
    # 路由模块
    "RoutingConfig",
    "RoutingStrategy",
    "NodeStatus",
    "RequestPriority",
    "NodeProfile",
    "NodeRegistry",
    "LatencyProbe",
    "OptimalRouter",
    "RequestTracker",
    "create_routing_system",
    # 区块链模块
    "BlockchainManager",
    "ProposalStatus",
    "ProposalType",
    "MiningInfo",
    "Proposal",
    "APIAccessRecord",
    "BurnRecord",
    "BlockchainError",
    "InsufficientBalanceError",
    "InvalidAddressError",
    "UnauthorizedError",
    "ProposalError",
    "MiningError",
    "TOKEN_DECIMALS",
    "TOKEN_DECIMALS_FACTOR",
    "INITIAL_BLOCK_REWARD",
    "HALVING_INTERVAL",
    "API_PRICES",
    "BURN_ADDRESS",
]

__version__ = "1.0.0"
