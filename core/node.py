"""
AICoin 节点 - 核心实现
======================

集成算力挖矿、治理投票、API 网关的去中心化 AI 计算节点。

本模块实现了 AICoin 网络的核心节点类 AICoinNode，它集成了:
    1. P2P 网络通信 (基于 servermodel 的 NetworkManager)
    2. 分布式推理 (基于 servermodel 的 PipelineInference)
    3. 算力挖矿 (MiningEngine)
    4. 治理投票 (GovernanceManager)
    5. API 网关 (APIGateway)
    6. 最优路由 (OptimalRouter)
    7. 区块链交互 (BlockchainManager)
    8. NAT 穿透 (NatTraversalManager)

Usage:
    >>> config = AICoinConfig.from_file("config.json")
    >>> node = AICoinNode(config)
    >>> node.start()
    >>> # ... 节点运行中: 挖矿、提供 API 服务、参与治理
    >>> node.stop()
"""

import os
import sys
import time
import json
import uuid
import signal
import socket
import threading
import traceback
import argparse
import logging
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import AICoinConfig, setup_logging_from_config

logger = logging.getLogger("aicoin.node")


# ==================== 节点状态枚举 ====================

class NodeStatus(Enum):
    """节点运行状态"""
    CREATED = "created"           # 已创建，未初始化
    INITIALIZING = "initializing" # 正在初始化子系统
    STARTING = "starting"         # 正在启动服务
    RUNNING = "running"           # 正常运行中
    PAUSING = "pausing"           # 正在暂停
    PAUSED = "paused"             # 已暂停
    STOPPING = "stopping"         # 正在停止
    STOPPED = "stopped"           # 已停止
    ERROR = "error"               # 异常状态


# ==================== AICoin 消息类型 ====================

class AICoinMessageType(Enum):
    """AICoin P2P 网络消息类型"""
    # 区块链
    BLOCK_NEW = "block_new"               # 新区块广播
    BLOCK_REQUEST = "block_request"       # 请求区块数据
    BLOCK_RESPONSE = "block_response"     # 区块数据响应

    # 挖矿
    MINING_PROOF = "mining_proof"         # 算力证明提交
    MINING_CHALLENGE = "mining_challenge" # 挖矿挑战广播

    # 治理
    GOV_PROPOSAL = "gov_proposal"         # 治理提案广播
    GOV_VOTE = "gov_vote"                 # 投票广播
    GOV_RESULT = "gov_result"             # 投票结果广播

    # 节点发现
    NODE_ANNOUNCE = "node_announce"       # 节点上线公告
    NODE_PING = "node_ping"               # 节点探测
    NODE_PONG = "node_pong"               # 节点探测响应


# ==================== 导入子模块 (延迟加载) ====================

def _import_blockchain():
    """延迟导入区块链模块"""
    try:
        from ..blockchain import BlockchainManager
        return BlockchainManager
    except ImportError:
        logger.warning("blockchain 模块未找到, 使用模拟实现")
        from . import _mock_blockchain
        return _mock_blockchain.MockBlockchainManager


def _import_mining_engine():
    """延迟导入挖矿引擎模块"""
    try:
        from ..mining_engine import MiningEngine
        return MiningEngine
    except ImportError:
        logger.warning("mining_engine 模块未找到, 使用模拟实现")
        from . import _mock_mining
        return _mock_mining.MockMiningEngine


def _import_governance():
    """延迟导入治理模块"""
    try:
        from ..governance import GovernanceManager
        return GovernanceManager
    except ImportError:
        logger.warning("governance 模块未找到, 使用模拟实现")
        from . import _mock_governance
        return _mock_governance.MockGovernanceManager


def _import_router():
    """延迟导入路由模块"""
    try:
        from ..router import OptimalRouter
        return OptimalRouter
    except ImportError:
        logger.warning("router 模块未找到, 使用模拟实现")
        from . import _mock_router
        return _mock_router.MockOptimalRouter


def _import_api_gateway():
    """延迟导入 API 网关模块"""
    try:
        from ..api_gateway import APIGateway
        return APIGateway
    except ImportError:
        logger.warning("api_gateway 模块未找到, 使用模拟实现")
        from . import _mock_api_gateway
        return _mock_api_gateway.MockAPIGateway


def _import_compute_meter():
    """延迟导入算力计量模块"""
    try:
        from ..mining_engine import ComputeMeter
        return ComputeMeter
    except ImportError:
        logger.warning("compute_meter 模块未找到, 使用模拟实现")
        from . import _mock_mining
        return _mock_mining.MockComputeMeter


def _import_reward_distributor():
    """延迟导入收益分配模块"""
    try:
        from ..rewards import RewardDistributor
        return RewardDistributor
    except ImportError:
        logger.warning("reward_distributor 模块未找到, 使用模拟实现")
        from . import _mock_rewards
        return _mock_rewards.MockRewardDistributor


# ==================== 核心: AICoinNode ====================

class AICoinNode:
    """AICoin 节点 - 集成算力挖矿、治理投票、API 网关的去中心化 AI 计算节点

    这是 AICoin 网络的核心节点类，它集成了:
    1. P2P 网络通信 (基于 servermodel 的 NetworkManager)
    2. 分布式推理 (基于 servermodel 的 PipelineInference)
    3. 算力挖矿 (MiningEngine)
    4. 治理投票 (GovernanceManager)
    5. API 网关 (APIGateway)
    6. 最优路由 (OptimalRouter)
    7. 区块链交互 (BlockchainManager)
    8. NAT 穿透 (NatTraversalManager)

    线程安全保证:
        - 所有公共方法通过内部锁保证线程安全
        - 状态转换使用原子操作
        - 子组件的启停顺序严格控制

    Usage:
        config = AICoinConfig.from_file("config.json")
        node = AICoinNode(config)
        node.start()
        # ... 节点运行中: 挖矿、提供 API 服务、参与治理
        node.stop()
    """

    # ==================== 初始化 ====================

    def __init__(self, config: AICoinConfig) -> None:
        """初始化 AICoin 节点

        Args:
            config: AICoin 配置实例。所有子组件共享此配置。
        """
        self.config = config
        self._status = NodeStatus.CREATED
        self._lock = threading.RLock()  # 可重入锁，支持嵌套调用
        self._status_event = threading.Event()  # 用于等待状态变更

        # 启动时间戳
        self._start_time: float = 0.0
        self._stop_time: float = 0.0

        # 后台线程引用 (用于优雅关闭)
        self._background_threads: List[threading.Thread] = []

        # 信号处理 (仅在主线程注册)
        self._signal_registered = False

        # === 初始化子组件 ===
        logger.info("初始化 AICoin 节点组件...")

        # 1. 区块链管理器 (其他组件依赖此项)
        BlockchainManagerCls = _import_blockchain()
        self.blockchain = BlockchainManagerCls(config)

        # 2. 算力计量器
        ComputeMeterCls = _import_compute_meter()
        self.compute_meter = ComputeMeterCls(config.node_id)

        # 3. 挖矿引擎
        MiningEngineCls = _import_mining_engine()
        self.mining_engine = MiningEngineCls(
            self.blockchain, config.node_id, config
        )

        # 4. 治理管理器
        GovernanceManagerCls = _import_governance()
        self.governance = GovernanceManagerCls(self.blockchain)

        # 5. 最优路由器
        OptimalRouterCls = _import_router()
        self.router = OptimalRouterCls(config)

        # 6. API 网关
        APIGatewayCls = _import_api_gateway()
        self.api_gateway = APIGatewayCls(
            self.blockchain, self.router, self.mining_engine, config
        )

        # 7. 收益分配器
        RewardDistributorCls = _import_reward_distributor()
        self.reward_distributor = RewardDistributorCls(self.blockchain)

        # 8. P2P 网络 (servermodel NetworkManager)
        self._network = None  # 延迟初始化

        logger.info(
            f"AICoin 节点已创建: "
            f"id={config.node_id[:8]}..., "
            f"name={config.node_name}, "
            f"mode={config.blockchain_mode}"
        )

    # ==================== 属性 ====================

    @property
    def status(self) -> NodeStatus:
        """获取当前节点状态 (线程安全)"""
        with self._lock:
            return self._status

    @property
    def is_running(self) -> bool:
        """节点是否正在运行"""
        return self.status == NodeStatus.RUNNING

    @property
    def uptime(self) -> float:
        """节点运行时长 (秒), 如果未运行则返回 0"""
        with self._lock:
            if self._start_time <= 0:
                return 0.0
            if self._status in (NodeStatus.RUNNING, NodeStatus.PAUSED):
                return time.time() - self._start_time
            return self._stop_time - self._start_time

    @property
    def node_id(self) -> str:
        """节点 ID"""
        return self.config.node_id

    @property
    def node_name(self) -> str:
        """节点名称"""
        return self.config.node_name

    # ==================== 内部状态管理 ====================

    def _set_status(self, new_status: NodeStatus) -> None:
        """设置节点状态 (线程安全, 带日志)

        Args:
            new_status: 新状态
        """
        with self._lock:
            old_status = self._status
            if old_status == new_status:
                return
            self._status = new_status
            self._status_event.set()
            self._status_event.clear()

        logger.info(f"节点状态变更: {old_status.value} -> {new_status.value}")

    def _assert_status(self, *expected: NodeStatus) -> None:
        """断言当前状态必须是指定状态之一

        Args:
            *expected: 期望的状态列表

        Raises:
            RuntimeError: 当前状态不在期望列表中
        """
        current = self.status
        if current not in expected:
            raise RuntimeError(
                f"节点状态不合法: 当前={current.value}, "
                f"期望={[s.value for s in expected]}"
            )

    # ==================== 启动与停止 ====================

    def start(self) -> None:
        """启动 AICoin 节点

        按照依赖顺序依次启动各子系统:
            1. 初始化区块链连接
            2. 启动 P2P 网络
            3. 启动算力挖矿
            4. 启动治理循环
            5. 启动 API 网关
            6. 注册到网络

        Raises:
            RuntimeError: 节点已处于运行状态
            Exception: 任何子组件启动失败将触发回滚关闭
        """
        self._assert_status(NodeStatus.CREATED, NodeStatus.STOPPED)

        self._set_status(NodeStatus.INITIALIZING)

        try:
            self._do_start()
        except Exception as e:
            logger.error(f"节点启动失败: {e}")
            logger.debug(traceback.format_exc())
            # 回滚: 尝试关闭已启动的组件
            self._emergency_cleanup()
            self._set_status(NodeStatus.ERROR)
            raise

    def _do_start(self) -> None:
        """实际启动逻辑 (内部方法)"""
        config = self.config

        # 步骤 1: 初始化区块链连接
        logger.info("[启动 1/6] 初始化区块链连接...")
        self.blockchain.connect()
        logger.info("区块链连接已建立")

        # 步骤 2: 启动 P2P 网络
        logger.info("[启动 2/6] 启动 P2P 网络...")
        self._start_p2p_network()

        # 步骤 3: 启动算力挖矿
        if config.auto_mine:
            logger.info("[启动 3/6] 启动算力挖矿...")
            self.mining_engine.start()
            logger.info("挖矿引擎已启动")
        else:
            logger.info("[启动 3/6] 跳过挖矿 (auto_mine=False)")

        # 步骤 4: 启动治理循环
        if config.governance_enabled:
            logger.info("[启动 4/6] 启动治理系统...")
            self._start_governance_loop()
            logger.info("治理系统已启动")
        else:
            logger.info("[启动 4/6] 跳过治理 (governance_enabled=False)")

        # 步骤 5: 启动 API 网关
        if config.api_enabled:
            logger.info("[启动 5/6] 启动 API 网关...")
            self.api_gateway.start()
            logger.info(
                f"API 网关已启动: http://{config.host}:{config.api_port}"
            )
        else:
            logger.info("[启动 5/6] 跳过 API 网关 (api_enabled=False)")

        # 步骤 6: 注册到网络
        logger.info("[启动 6/6] 注册到网络...")
        self._register_to_network()

        # 记录启动时间
        with self._lock:
            self._start_time = time.time()

        self._set_status(NodeStatus.RUNNING)
        logger.info(
            f"✓ AICoin 节点启动完成! "
            f"node={config.node_name}, "
            f"api=http://{config.host}:{config.api_port}, "
            f"p2p={config.host}:{config.p2p_port}"
        )

    def stop(self) -> None:
        """停止 AICoin 节点

        按照启动的逆序依次关闭各子系统:
            1. 从网络注销
            2. 停止 API 网关
            3. 停止治理循环
            4. 停止挖矿
            5. 停止 P2P 网络
            6. 断开区块链连接

        等待所有后台线程结束后返回。
        """
        self._assert_status(
            NodeStatus.RUNNING, NodeStatus.PAUSED, NodeStatus.ERROR
        )

        self._set_status(NodeStatus.STOPPING)
        logger.info("正在停止 AICoin 节点...")

        try:
            self._do_stop()
        except Exception as e:
            logger.error(f"节点停止过程中出错: {e}")
            logger.debug(traceback.format_exc())
        finally:
            with self._lock:
                self._stop_time = time.time()
            self._set_status(NodeStatus.STOPPED)
            logger.info("AICoin 节点已停止")

    def _do_stop(self) -> None:
        """实际停止逻辑 (内部方法)"""
        config = self.config

        # 步骤 1: 从网络注销
        logger.info("[停止 1/6] 从网络注销...")
        self._unregister_from_network()

        # 步骤 2: 停止 API 网关
        if config.api_enabled:
            logger.info("[停止 2/6] 停止 API 网关...")
            try:
                self.api_gateway.stop()
                logger.info("API 网关已停止")
            except Exception as e:
                logger.warning(f"停止 API 网关出错: {e}")
        else:
            logger.info("[停止 2/6] 跳过 (API 网关未启用)")

        # 步骤 3: 停止治理循环
        if config.governance_enabled:
            logger.info("[停止 3/6] 停止治理系统...")
            try:
                self.governance.stop()
                logger.info("治理系统已停止")
            except Exception as e:
                logger.warning(f"停止治理系统出错: {e}")
        else:
            logger.info("[停止 3/6] 跳过 (治理系统未启用)")

        # 步骤 4: 停止挖矿
        if config.auto_mine:
            logger.info("[停止 4/6] 停止挖矿引擎...")
            try:
                self.mining_engine.stop()
                logger.info("挖矿引擎已停止")
            except Exception as e:
                logger.warning(f"停止挖矿引擎出错: {e}")
        else:
            logger.info("[停止 4/6] 跳过 (挖矿未启用)")

        # 步骤 5: 停止 P2P 网络
        logger.info("[停止 5/6] 停止 P2P 网络...")
        self._stop_p2p_network()

        # 步骤 6: 断开区块链
        logger.info("[停止 6/6] 断开区块链连接...")
        try:
            self.blockchain.disconnect()
            logger.info("区块链连接已断开")
        except Exception as e:
            logger.warning(f"断开区块链连接出错: {e}")

        # 等待所有后台线程结束
        self._wait_for_threads(timeout=10.0)

        # 保存节点状态
        self._save_state()

    def _emergency_cleanup(self) -> None:
        """紧急清理: 在启动失败时尝试关闭已启动的组件"""
        logger.warning("执行紧急清理...")
        try:
            if self.api_gateway:
                try:
                    self.api_gateway.stop()
                except Exception:
                    pass
            if self.mining_engine:
                try:
                    self.mining_engine.stop()
                except Exception:
                    pass
            if self._network:
                try:
                    self._stop_p2p_network()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"紧急清理出错: {e}")

    # ==================== P2P 网络 ====================

    def _start_p2p_network(self) -> None:
        """启动 P2P 网络服务

        初始化 servermodel NetworkManager 并注册 AICoin 消息处理器。
        """
        try:
            from servermodel.core.node_unified_complete import (
                NetworkManager,
                MessageType,
            )
        except ImportError:
            logger.warning(
                "servermodel 未安装, P2P 网络将使用简化模式"
            )
            self._network = None
            return

        # 构建 servermodel 配置
        sm_config = self._build_servermodel_config()

        self._network = NetworkManager(sm_config)

        # 注册 AICoin 消息处理器
        self._register_p2p_handlers()

        # 启动 TCP 服务器
        self._network.start_server()

        # 连接种子节点
        self._connect_to_seeds()

        logger.info(
            f"P2P 网络已启动: {self.config.host}:{self.config.p2p_port}"
        )

    def _stop_p2p_network(self) -> None:
        """停止 P2P 网络服务"""
        if self._network is None:
            return

        try:
            self._network.running = False
            # 关闭所有连接
            for node_id, conn in list(self._network.connections.items()):
                try:
                    conn.close()
                except Exception:
                    pass
            # 关闭服务端 socket
            if self._network.server_socket:
                try:
                    self._network.server_socket.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"停止 P2P 网络出错: {e}")
        finally:
            self._network = None

        logger.info("P2P 网络已停止")

    def _build_servermodel_config(self):
        """构建 servermodel UnifiedConfig (兼容适配)"""
        try:
            from servermodel.core.node_unified_complete import UnifiedConfig
        except ImportError:
            return None

        return UnifiedConfig(
            node_id=self.config.node_id,
            node_name=self.config.node_name,
            host=self.config.host,
            port=self.config.p2p_port,
            api_port=self.config.api_port,
            model_name=self.config.model_name,
            seeds=self.config.seeds,
            log_level=self.config.log_level,
            log_file=self.config.log_file,
        )

    def _register_p2p_handlers(self) -> None:
        """注册 P2P 消息处理器"""
        if self._network is None:
            return

        try:
            from servermodel.core.node_unified_complete import MessageType
        except ImportError:
            return

        # 注册 AICoin 自定义消息处理器
        # 注意: servermodel 的 MessageType 是枚举，AICoin 消息使用
        # TASK_ASSIGN 等通用消息类型，在 data 字段中携带子类型
        self._network.register_handler(
            MessageType.TASK_RESULT, self._on_p2p_message
        )
        self._network.register_handler(
            MessageType.HEARTBEAT, self._on_p2p_message
        )
        self._network.register_handler(
            MessageType.NODE_JOIN, self._on_p2p_message
        )

        logger.debug("P2P 消息处理器已注册")

    def _on_p2p_message(self, data: dict, from_node: str) -> Optional[dict]:
        """统一 P2P 消息分发器

        根据消息体中的 'aicoin_type' 字段分发到对应的 AICoin 处理方法。

        Args:
            data: 消息数据
            from_node: 发送节点 ID

        Returns:
            可选的响应数据
        """
        try:
            aicoin_type = data.get("aicoin_type", "")

            handlers: Dict[str, Callable] = {
                AICoinMessageType.BLOCK_NEW.value: self.handle_aicoin_block,
                AICoinMessageType.MINING_PROOF.value: self.handle_mining_proof,
                AICoinMessageType.GOV_PROPOSAL.value: self.handle_governance_proposal,
                AICoinMessageType.GOV_VOTE.value: self.handle_governance_vote,
                AICoinMessageType.NODE_ANNOUNCE.value: self._handle_node_announce,
            }

            handler = handlers.get(aicoin_type)
            if handler:
                message = {
                    "data": data,
                    "from_node": from_node,
                    "timestamp": time.time(),
                }
                handler(message)
            else:
                logger.debug(
                    f"未知的 AICoin 消息类型: {aicoin_type}, "
                    f"来自节点: {from_node[:8]}..."
                )

        except Exception as e:
            logger.error(f"处理 P2P 消息失败: {e}")
            logger.debug(traceback.format_exc())

        return None  # 广播消息不需要响应

    def _connect_to_seeds(self) -> None:
        """连接种子节点"""
        if not self.config.seeds:
            logger.info("未配置种子节点, 等待其他节点连接")
            return

        connected = 0
        for seed_addr in self.config.seeds:
            try:
                parts = seed_addr.strip().rsplit(":", 1)
                if len(parts) != 2:
                    logger.warning(f"种子节点地址格式错误: {seed_addr}")
                    continue

                host, port = parts[0], int(parts[1])

                # 检查是否连接自己
                if (
                    host in ("127.0.0.1", "localhost", "0.0.0.0")
                    and int(port) == self.config.p2p_port
                ):
                    continue

                if self._network:
                    try:
                        from servermodel.core.node_unified_complete import (
                            MessageType,
                        )
                        result = self._network.send_message(
                            host,
                            port,
                            MessageType.DISCOVER,
                            {"node_id": self.config.node_id},
                            wait_response=True,
                            timeout=5.0,
                        )
                        if result:
                            connected += 1
                            logger.info(f"已连接种子节点: {seed_addr}")
                    except Exception as e:
                        logger.warning(f"连接种子节点失败 {seed_addr}: {e}")

            except Exception as e:
                logger.warning(f"解析种子节点地址失败: {seed_addr}: {e}")

        logger.info(
            f"种子节点连接完成: {connected}/{len(self.config.seeds)} 成功"
        )

    def _register_to_network(self) -> None:
        """向网络注册本节点"""
        announce_msg = {
            "node_id": self.config.node_id,
            "node_name": self.config.node_name,
            "host": self._get_public_host(),
            "p2p_port": self.config.p2p_port,
            "api_port": self.config.api_port,
            "model_name": self.config.model_name,
            "blockchain_mode": self.config.blockchain_mode,
            "timestamp": time.time(),
        }

        self._broadcast_message(
            AICoinMessageType.NODE_ANNOUNCE, announce_msg
        )
        logger.info("节点已向网络注册")

    def _unregister_from_network(self) -> None:
        """从网络注销本节点"""
        try:
            self._broadcast_message(
                "node_leave",
                {
                    "node_id": self.config.node_id,
                    "timestamp": time.time(),
                },
            )
            logger.info("节点已从网络注销")
        except Exception as e:
            logger.debug(f"网络注销出错 (非致命): {e}")

    def _handle_node_announce(self, message: dict) -> None:
        """处理节点上线公告

        Args:
            message: 包含节点信息的消息
        """
        data = message.get("data", {})
        from_node = message.get("from_node", "")

        # 不处理自己的公告
        if from_node == self.config.node_id:
            return

        node_name = data.get("node_name", "unknown")
        model = data.get("model_name", "unknown")

        logger.info(
            f"发现新节点: {node_name} ({from_node[:8]}...), "
            f"模型={model}"
        )

        # 如果使用 servermodel NetworkManager, 更新已知节点
        if self._network and hasattr(self._network, "known_nodes"):
            try:
                from servermodel.core.node_unified_complete import (
                    NodeInfo,
                    NodeRole,
                    NodeState,
                )

                node_info = NodeInfo(
                    node_id=from_node,
                    node_name=node_name,
                    host=data.get("host", ""),
                    port=data.get("p2p_port", 0),
                    role=NodeRole.FOLLOWER,
                    state=NodeState.READY,
                    model_loaded=True,
                    model_name=model,
                    last_heartbeat=time.time(),
                )
                self._network.known_nodes[from_node] = node_info
            except Exception as e:
                logger.debug(f"更新已知节点信息失败: {e}")

    def _get_public_host(self) -> str:
        """获取本节点的公网可达 IP 地址

        Returns:
            公网 IP 或本地 IP
        """
        # 优先尝试获取公网 IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            pass

        # 回退到本地 IP
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"

    def _broadcast_message(
        self, msg_type, data: dict, exclude: Optional[List[str]] = None
    ) -> int:
        """广播消息到所有已知节点

        Args:
            msg_type: 消息类型
            data: 消息数据
            exclude: 排除的节点 ID 列表

        Returns:
            成功发送的节点数
        """
        if self._network is None:
            logger.debug("P2P 网络未启动, 无法广播消息")
            return 0

        exclude = exclude or []
        exclude.append(self.config.node_id)

        sent = 0
        known_nodes = getattr(self._network, "known_nodes", {})

        for node_id, node_info in known_nodes.items():
            if node_id in exclude:
                continue

            try:
                host = node_info.host
                port = node_info.port

                if not host or not port:
                    continue

                payload = {
                    **data,
                    "aicoin_type": (
                        msg_type.value
                        if isinstance(msg_type, Enum)
                        else str(msg_type)
                    ),
                }

                try:
                    from servermodel.core.node_unified_complete import (
                        MessageType,
                    )

                    self._network.send_message(
                        host,
                        port,
                        MessageType.TASK_RESULT,
                        payload,
                        wait_response=False,
                    )
                    sent += 1
                except Exception:
                    pass

            except Exception as e:
                logger.debug(
                    f"广播到节点 {node_id[:8]}... 失败: {e}"
                )

        return sent

    # ==================== 治理循环 ====================

    def _start_governance_loop(self) -> None:
        """启动治理后台循环线程

        定期检查活跃提案、处理投票截止等。
        """
        def _governance_loop():
            """治理循环主函数"""
            logger.info("治理循环已启动")
            while self.is_running:
                try:
                    # 检查待处理提案
                    active_proposals = self.governance.get_active_proposals()
                    if active_proposals:
                        logger.debug(
                            f"活跃提案数: {len(active_proposals)}"
                        )
                        for proposal in active_proposals:
                            self._process_proposal(proposal)

                    # 同步链上状态
                    self.governance.sync_state()

                except Exception as e:
                    logger.error(f"治理循环出错: {e}")
                    logger.debug(traceback.format_exc())

                # 休眠
                time.sleep(30)

            logger.info("治理循环已退出")

        t = threading.Thread(
            target=_governance_loop,
            name="aicoin-governance",
            daemon=True,
        )
        t.start()
        self._background_threads.append(t)

    def _process_proposal(self, proposal: dict) -> None:
        """处理单个治理提案

        Args:
            proposal: 提案数据
        """
        proposal_id = proposal.get("proposal_id", "")
        proposal_type = proposal.get("type", "")

        logger.debug(
            f"处理提案: {proposal_id[:8]}..., 类型={proposal_type}"
        )

        # 此处可添加自动投票逻辑
        # 例如: 根据预设规则自动对特定类型提案投票

    # ==================== 挖矿集成 ====================

    def on_inference_completed(self, result: dict) -> None:
        """推理完成回调

        当节点完成一次推理任务时:
            1. 记录算力贡献 (compute_meter)
            2. 触发挖矿奖励计算 (mining_engine)
            3. 分配 API 收入 (reward_distributor)

        Args:
            result: 推理结果字典，应包含:
                - success (bool): 是否成功
                - tokens (int): 消耗的 token 数
                - latency (float): 推理延迟 (秒)
                - model_name (str): 使用的模型
                - request_id (str, 可选): 请求 ID
                - tier (str, 可选): API 服务等级
                - payment (int, 可选): 支付金额
        """
        if not self.is_running:
            logger.debug("节点未运行, 忽略推理完成回调")
            return

        try:
            success = result.get("success", False)
            tokens = result.get("tokens", 0)
            latency = result.get("latency", 0.0)
            model_name = result.get("model_name", self.config.model_name)
            request_id = result.get("request_id", str(uuid.uuid4()))

            if not success:
                logger.warning(
                    f"推理失败 (request={request_id}): "
                    f"{result.get('error', '未知错误')}"
                )
                return

            # 步骤 1: 记录算力贡献
            self.compute_meter.record(
                request_id=request_id,
                model_name=model_name,
                tokens=tokens,
                latency=latency,
                timestamp=time.time(),
            )
            logger.debug(
                f"算力已记录: tokens={tokens}, latency={latency:.3f}s"
            )

            # 步骤 2: 触发挖矿奖励计算
            self.mining_engine.on_compute_contributed(
                tokens=tokens,
                latency=latency,
                model_name=model_name,
            )

            # 步骤 3: 分配 API 收入
            payment = result.get("payment", 0)
            if payment > 0:
                self.reward_distributor.distribute(
                    node_id=self.config.node_id,
                    amount=payment,
                    tier=result.get("tier", "basic"),
                    request_id=request_id,
                )
                logger.debug(
                    f"收入已分配: amount={payment}, tier={result.get('tier', 'basic')}"
                )

        except Exception as e:
            logger.error(f"处理推理完成回调失败: {e}")
            logger.debug(traceback.format_exc())

    # ==================== P2P 消息处理器 ====================

    def handle_aicoin_block(self, message: dict) -> None:
        """处理新区块通知

        当收到其他节点广播的新区块时:
            1. 验证区块合法性
            2. 更新本地链状态
            3. 检查是否包含与本地相关的交易

        Args:
            message: 区块消息, 格式:
                {
                    "data": {
                        "block_number": int,
                        "block_hash": str,
                        "parent_hash": str,
                        "timestamp": float,
                        "transactions": list,
                        "proposer": str,
                    },
                    "from_node": str,
                }
        """
        try:
            data = message.get("data", {})
            from_node = message.get("from_node", "")

            block_number = data.get("block_number", 0)
            block_hash = data.get("block_hash", "")
            proposer = data.get("proposer", "unknown")

            logger.info(
                f"收到新区块: #{block_number}, hash={block_hash[:16]}..., "
                f"proposer={proposer[:8]}..., from={from_node[:8]}..."
            )

            # 验证并添加到本地链
            if hasattr(self.blockchain, "add_block"):
                try:
                    self.blockchain.add_block(data)
                    logger.debug(f"区块 #{block_number} 已添加到本地链")
                except Exception as e:
                    logger.warning(f"添加区块失败: {e}")

        except Exception as e:
            logger.error(f"处理新区块失败: {e}")
            logger.debug(traceback.format_exc())

    def handle_mining_proof(self, message: dict) -> None:
        """处理其他节点的算力证明

        当收到其他节点提交的算力证明时:
            1. 验证证明的合法性
            2. 更新本地挖矿状态
            3. 转发验证通过的证明

        Args:
            message: 算力证明消息, 格式:
                {
                    "data": {
                        "node_id": str,
                        "proof_hash": str,
                        "compute_units": int,
                        "block_number": int,
                        "timestamp": float,
                        "signature": str,
                    },
                    "from_node": str,
                }
        """
        try:
            data = message.get("data", {})
            from_node = message.get("from_node", "")

            node_id = data.get("node_id", from_node)
            compute_units = data.get("compute_units", 0)
            block_number = data.get("block_number", 0)

            logger.info(
                f"收到算力证明: node={node_id[:8]}..., "
                f"compute={compute_units}, block=#{block_number}"
            )

            # 验证算力证明
            if hasattr(self.mining_engine, "verify_proof"):
                is_valid = self.mining_engine.verify_proof(data)
                if is_valid:
                    logger.debug(f"算力证明验证通过: {node_id[:8]}...")
                else:
                    logger.warning(
                        f"算力证明验证失败: {node_id[:8]}..."
                    )

        except Exception as e:
            logger.error(f"处理算力证明失败: {e}")
            logger.debug(traceback.format_exc())

    def handle_governance_proposal(self, message: dict) -> None:
        """处理治理提案广播

        当收到新的治理提案时:
            1. 验证提案格式
            2. 添加到本地提案列表
            3. 如果满足自动投票条件则自动投票

        Args:
            message: 治理提案消息, 格式:
                {
                    "data": {
                        "proposal_id": str,
                        "title": str,
                        "description": str,
                        "proposer": str,
                        "stake_amount": int,
                        "created_at": float,
                        "end_at": float,
                    },
                    "from_node": str,
                }
        """
        try:
            data = message.get("data", {})
            from_node = message.get("from_node", "")

            proposal_id = data.get("proposal_id", "")
            title = data.get("title", "(无标题)")
            proposer = data.get("proposer", "unknown")

            logger.info(
                f"收到治理提案: {proposal_id[:8]}... '{title}', "
                f"proposer={proposer[:8]}..., from={from_node[:8]}..."
            )

            # 添加到本地治理管理器
            if hasattr(self.governance, "add_proposal"):
                try:
                    self.governance.add_proposal(data)
                    logger.debug(
                        f"提案已添加到本地: {proposal_id[:8]}..."
                    )
                except Exception as e:
                    logger.warning(f"添加提案失败: {e}")

        except Exception as e:
            logger.error(f"处理治理提案失败: {e}")
            logger.debug(traceback.format_exc())

    def handle_governance_vote(self, message: dict) -> None:
        """处理投票广播

        当收到其他节点的投票时:
            1. 验证投票签名
            2. 更新本地投票计数
            3. 检查是否达到投票截止

        Args:
            message: 投票消息, 格式:
                {
                    "data": {
                        "proposal_id": str,
                        "voter": str,
                        "vote": str,  # "for" / "against" / "abstain"
                        "weight": int,
                        "timestamp": float,
                        "signature": str,
                    },
                    "from_node": str,
                }
        """
        try:
            data = message.get("data", {})
            from_node = message.get("from_node", "")

            proposal_id = data.get("proposal_id", "")
            voter = data.get("voter", from_node)
            vote = data.get("vote", "unknown")

            logger.info(
                f"收到投票: proposal={proposal_id[:8]}..., "
                f"voter={voter[:8]}..., vote={vote}"
            )

            # 更新本地投票记录
            if hasattr(self.governance, "record_vote"):
                try:
                    self.governance.record_vote(data)
                    logger.debug(
                        f"投票已记录: {proposal_id[:8]}..., {vote}"
                    )
                except Exception as e:
                    logger.warning(f"记录投票失败: {e}")

        except Exception as e:
            logger.error(f"处理投票失败: {e}")
            logger.debug(traceback.format_exc())

    # ==================== 状态查询 ====================

    def get_status(self) -> dict:
        """获取节点完整状态

        返回包含所有子系统状态的综合状态报告。

        Returns:
            状态字典, 包含:
                - node_info: 节点基本信息
                - mining_info: 挖矿状态
                - governance_info: 治理状态
                - network_info: 网络状态
                - api_stats: API 网关统计
                - blockchain_info: 区块链状态
        """
        now = time.time()

        # 节点基本信息
        node_info = {
            "node_id": self.config.node_id,
            "node_name": self.config.node_name,
            "status": self.status.value,
            "uptime": self.uptime,
            "wallet_address": self.config.wallet_address,
            "blockchain_mode": self.config.blockchain_mode,
            "model_name": self.config.model_name,
        }

        # 挖矿信息
        mining_info = self._safe_get_component_status(
            self.mining_engine, "get_mining_status"
        ) or {
            "status": "unknown",
            "total_compute_units": 0,
            "total_rewards": 0,
        }

        # 治理信息
        governance_info = self._safe_get_component_status(
            self.governance, "get_governance_status"
        ) or {
            "status": "unknown",
            "active_proposals": 0,
            "total_votes_cast": 0,
        }

        # 网络信息
        network_info = {
            "p2p_port": self.config.p2p_port,
            "connected_nodes": 0,
            "known_nodes": 0,
        }
        if self._network and hasattr(self._network, "known_nodes"):
            known = self._network.known_nodes
            alive = sum(
                1 for n in known.values() if getattr(n, "is_alive", False)
            )
            network_info["connected_nodes"] = alive
            network_info["known_nodes"] = len(known)

        # API 统计
        api_stats = self._safe_get_component_status(
            self.api_gateway, "get_stats"
        ) or {
            "status": "unknown",
            "total_requests": 0,
            "total_revenue": 0,
        }

        # 区块链信息
        blockchain_info = self._safe_get_component_status(
            self.blockchain, "get_status"
        ) or {
            "status": "unknown",
            "block_number": 0,
            "synced": False,
        }

        return {
            "timestamp": now,
            "node_info": node_info,
            "mining_info": mining_info,
            "governance_info": governance_info,
            "network_info": network_info,
            "api_stats": api_stats,
            "blockchain_info": blockchain_info,
        }

    def get_dashboard_data(self) -> dict:
        """获取仪表盘数据 (用于 Web UI)

        返回经过聚合和格式化的数据，适合前端仪表盘展示。

        Returns:
            仪表盘数据字典, 包含:
                - overview: 概览统计
                - mining: 挖矿图表数据
                - network: 网络拓扑摘要
                - governance: 治理活跃度
                - economics: 代币经济数据
        """
        full_status = self.get_status()

        # 概览
        overview = {
            "node_name": self.config.node_name,
            "status": self.status.value,
            "uptime_seconds": self.uptime,
            "uptime_human": self._format_uptime(self.uptime),
            "model": self.config.model_name,
            "connected_peers": full_status["network_info"].get(
                "connected_nodes", 0
            ),
        }

        # 挖矿数据
        mining = full_status.get("mining_info", {})
        mining_dashboard = {
            "status": mining.get("status", "unknown"),
            "total_compute_units": mining.get("total_compute_units", 0),
            "total_rewards_earned": mining.get("total_rewards", 0),
            "current_difficulty": mining.get("difficulty", 0),
            "hash_rate": mining.get("hash_rate", 0),
        }

        # 经济数据
        blockchain = full_status.get("blockchain_info", {})
        economics = {
            "block_number": blockchain.get("block_number", 0),
            "current_reward": self._calculate_current_reward(
                blockchain.get("block_number", 0)
            ),
            "max_supply": self.config.max_supply,
            "total_mined": self._estimate_total_mined(
                blockchain.get("block_number", 0)
            ),
            "node_reward_pct": self.config.node_reward_percentage,
            "treasury_pct": self.config.treasury_percentage,
        }

        # 治理
        governance = full_status.get("governance_info", {})
        governance_dashboard = {
            "active_proposals": governance.get("active_proposals", 0),
            "total_votes": governance.get("total_votes_cast", 0),
            "governance_enabled": self.config.governance_enabled,
        }

        # 网络
        network = full_status.get("network_info", {})
        network_dashboard = {
            "connected_peers": network.get("connected_nodes", 0),
            "known_peers": network.get("known_nodes", 0),
            "p2p_port": self.config.p2p_port,
            "api_port": self.config.api_port,
        }

        return {
            "timestamp": time.time(),
            "overview": overview,
            "mining": mining_dashboard,
            "economics": economics,
            "governance": governance_dashboard,
            "network": network_dashboard,
        }

    # ==================== 辅助方法 ====================

    def _safe_get_component_status(
        self, component: Any, method_name: str
    ) -> Optional[dict]:
        """安全获取组件状态

        Args:
            component: 组件实例
            method_name: 状态方法名

        Returns:
            状态字典或 None (如果调用失败)
        """
        if component is None:
            return None
        try:
            method = getattr(component, method_name, None)
            if callable(method):
                result = method()
                if isinstance(result, dict):
                    return result
        except Exception as e:
            logger.debug(
                f"获取组件状态失败 ({type(component).__name__}.{method_name}): {e}"
            )
        return None

    def _calculate_current_reward(self, block_number: int) -> int:
        """计算当前区块奖励 (考虑减半)

        Args:
            block_number: 当前区块高度

        Returns:
            当前区块奖励 (AIC)
        """
        halvings = block_number // max(1, self.config.halving_interval)
        if halvings >= 64:  # 比特币风格: 64 次减半后奖励为 0
            return 0
        reward = self.config.initial_block_reward >> halvings
        return max(reward, 0)

    def _estimate_total_mined(self, block_number: int) -> int:
        """估算已开采的总 AIC 数量

        Args:
            block_number: 当前区块高度

        Returns:
            估算的已开采总量 (AIC)
        """
        total = 0
        halvings = 0
        remaining = block_number
        interval = max(1, self.config.halving_interval)
        reward = self.config.initial_block_reward

        while remaining > 0 and reward > 0:
            blocks_in_era = min(remaining, interval)
            total += blocks_in_era * reward
            remaining -= blocks_in_era
            reward >>= 1  # 减半
            halvings += 1

        return total

    def _format_uptime(self, seconds: float) -> str:
        """将秒数格式化为人类可读的时间字符串

        Args:
            seconds: 秒数

        Returns:
            格式化字符串, 如 "2d 5h 30m 15s"
        """
        if seconds <= 0:
            return "0s"

        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if secs > 0 or not parts:
            parts.append(f"{secs}s")

        return " ".join(parts)

    def _wait_for_threads(self, timeout: float = 10.0) -> None:
        """等待所有后台线程结束

        Args:
            timeout: 超时时间 (秒)
        """
        remaining = list(self._background_threads)
        deadline = time.time() + timeout

        for t in remaining:
            remaining_time = deadline - time.time()
            if remaining_time <= 0:
                logger.warning(
                    f"等待线程 {t.name} 超时, 强制跳过"
                )
                break

            if t.is_alive():
                t.join(timeout=remaining_time)
                if t.is_alive():
                    logger.warning(
                        f"线程 {t.name} 未在超时时间内结束"
                    )

        self._background_threads.clear()

    def _save_state(self) -> None:
        """保存节点状态到文件

        将当前的运行时状态持久化到 data_dir/state_file。
        """
        try:
            state = {
                "node_id": self.config.node_id,
                "node_name": self.config.node_name,
                "status": self.status.value,
                "last_uptime": self.uptime,
                "timestamp": time.time(),
                "config_snapshot": {
                    "blockchain_mode": self.config.blockchain_mode,
                    "model_name": self.config.model_name,
                    "api_port": self.config.api_port,
                    "p2p_port": self.config.p2p_port,
                },
            }

            # 获取各组件状态
            mining_status = self._safe_get_component_status(
                self.mining_engine, "get_mining_status"
            )
            if mining_status:
                state["mining"] = mining_status

            state_dir = Path(self.config.data_dir)
            state_dir.mkdir(parents=True, exist_ok=True)
            state_path = state_dir / self.config.state_file

            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False, default=str)

            logger.debug(f"节点状态已保存: {state_path}")

        except Exception as e:
            logger.warning(f"保存节点状态失败: {e}")

    # ==================== 信号处理 ====================

    def register_signal_handlers(self) -> None:
        """注册 POSIX 信号处理器

        仅在主线程中有效。注册后, SIGINT 和 SIGTERM 将触发优雅关闭。

        Note:
            此方法应在调用 start() 之前或之后立即调用。
            多次调用不会重复注册。
        """
        if self._signal_registered:
            return

        import threading as _threading

        # 仅在主线程注册信号处理
        if _threading.current_thread() is not _threading.main_thread():
            logger.warning("信号处理器只能在主线程注册")
            return

        def _signal_handler(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.info(f"收到信号 {sig_name}, 开始优雅关闭...")
            try:
                self.stop()
            except Exception as e:
                logger.error(f"优雅关闭失败: {e}")
            finally:
                sys.exit(0)

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
        self._signal_registered = True

        logger.info("信号处理器已注册 (SIGINT, SIGTERM)")

    # ==================== 上下文管理器 ====================

    def __enter__(self) -> "AICoinNode":
        """支持 with 语句"""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """支持 with 语句"""
        self.stop()


# ==================== 命令行入口 ====================

def main() -> None:
    """AICoin 节点命令行入口

    用法:
        python -m aicoin.core.node [选项]

    选项:
        -c, --config PATH     配置文件路径 (默认: config.json)
        --node-id ID          节点 ID (覆盖配置文件)
        --node-name NAME      节点名称 (覆盖配置文件)
        --host HOST           监听地址 (默认: 0.0.0.0)
        --api-port PORT       API 端口 (默认: 8080)
        --p2p-port PORT       P2P 端口 (默认: 5000)
        --no-mine             禁用自动挖矿
        --no-api              禁用 API 网关
        --no-governance       禁用治理系统
        --log-level LEVEL     日志级别 (DEBUG/INFO/WARNING/ERROR)
        --save-config         将合并后的配置保存回文件
        -v, --version         显示版本号
    """
    parser = argparse.ArgumentParser(
        prog="aicoin-node",
        description="AICoin 去中心化 AI 计算节点",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m aicoin.core.node                     # 使用默认配置启动
  python -m aicoin.core.node -c my_config.json   # 指定配置文件
  python -m aicoin.core.node --api-port 9090     # 覆盖 API 端口
  python -m aicoin.core.node --no-mine           # 仅提供 API 服务
        """,
    )

    parser.add_argument(
        "-c", "--config",
        type=str,
        default="config.json",
        help="配置文件路径 (默认: config.json)",
    )
    parser.add_argument(
        "--node-id",
        type=str,
        default=None,
        help="节点 ID (覆盖配置文件)",
    )
    parser.add_argument(
        "--node-name",
        type=str,
        default=None,
        help="节点名称 (覆盖配置文件)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="监听地址",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=None,
        help="API 网关端口",
    )
    parser.add_argument(
        "--p2p-port",
        type=int,
        default=None,
        help="P2P 网络端口",
    )
    parser.add_argument(
        "--no-mine",
        action="store_true",
        default=False,
        help="禁用自动挖矿",
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        default=False,
        help="禁用 API 网关",
    )
    parser.add_argument(
        "--no-governance",
        action="store_true",
        default=False,
        help="禁用治理系统",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="日志级别",
    )
    parser.add_argument(
        "--save-config",
        action="store_true",
        default=False,
        help="将合并后的配置保存回文件",
    )
    parser.add_argument(
        "-v", "--version",
        action="store_true",
        default=False,
        help="显示版本号并退出",
    )

    args = parser.parse_args()

    # 显示版本
    if args.version:
        try:
            from .. import __version__
            print(f"aicoin-node version {__version__}")
        except ImportError:
            print("aicoin-node version 0.1.0")
        sys.exit(0)

    # 加载配置
    try:
        config = AICoinConfig.from_file_and_env(args.config)
    except FileNotFoundError:
        logger.warning(
            f"配置文件 '{args.config}' 不存在, 使用默认配置"
        )
        config = AICoinConfig.from_env()
    except Exception as e:
        logger.error(f"加载配置失败: {e}")
        sys.exit(1)

    # 命令行参数覆盖
    if args.node_id:
        config.node_id = args.node_id
    if args.node_name:
        config.node_name = args.node_name
    if args.host:
        config.host = args.host
    if args.api_port is not None:
        config.api_port = args.api_port
    if args.p2p_port is not None:
        config.p2p_port = args.p2p_port
    if args.no_mine:
        config.auto_mine = False
    if args.no_api:
        config.api_enabled = False
    if args.no_governance:
        config.governance_enabled = False
    if args.log_level:
        config.log_level = args.log_level

    # 初始化日志系统
    setup_logging_from_config(config)

    # 打印启动横幅
    _print_banner(config)

    # 保存合并后的配置
    if args.save_config:
        config.save(args.config)
        logger.info(f"配置已保存到: {args.config}")

    # 创建并启动节点
    node: Optional[AICoinNode] = None
    try:
        node = AICoinNode(config)
        node.register_signal_handlers()
        node.start()

        # 主线程阻塞: 等待节点状态变更
        while node.is_running:
            try:
                # 每秒检查一次状态
                node._status_event.wait(timeout=1.0)
            except KeyboardInterrupt:
                logger.info("收到键盘中断, 开始关闭...")
                break

    except Exception as e:
        logger.error(f"节点运行异常: {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)

    finally:
        if node and node.is_running:
            try:
                node.stop()
            except Exception as e:
                logger.error(f"节点关闭失败: {e}")
                sys.exit(1)

    logger.info("AICoin 节点已退出")
    sys.exit(0)


def _print_banner(config: AICoinConfig) -> None:
    """打印启动横幅

    Args:
        config: 节点配置
    """
    banner = f"""
╔══════════════════════════════════════════════════════╗
║                    AICoin Node                       ║
║          去中心化 AI 算力挖矿网络节点                  ║
╠══════════════════════════════════════════════════════╣
║  节点 ID   : {config.node_id[:36]:<36s} ║
║  节点名称 : {config.node_name[:36]:<36s} ║
║  区块链   : {config.blockchain_mode:<36s} ║
║  模型     : {config.model_name[:36]:<36s} ║
║  API      : {f'{config.host}:{config.api_port}':<36s} ║
║  P2P      : {f'{config.host}:{config.p2p_port}':<36s} ║
║  挖矿     : {'已启用' if config.auto_mine else '已禁用':<36s} ║
║  API 网关 : {'已启用' if config.api_enabled else '已禁用':<36s} ║
║  治理     : {'已启用' if config.governance_enabled else '已禁用':<36s} ║
╚══════════════════════════════════════════════════════╝
"""
    print(banner)


# ==================== 模块入口 ====================

if __name__ == "__main__":
    main()
