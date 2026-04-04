"""
AICoin 治理模块 - 去中心化AI算力网络的链上治理系统

AICoin代币持有者通过提案和投票来治理网络，关键决策包括：
- 选择运行的AI模型
- 修改网络参数
- 紧急安全操作
- 协议升级

投票规则：
- 1 AIC = 1 票
- 通过门槛：获得51%以上赞成票
- 法定人数：总供应量的10%参与投票
- 标准投票周期：7天
- 紧急投票周期：24小时
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("aicoin.governance")


# ============================================================================
# 常量配置
# ============================================================================

# 提案最低质押：1000 AIC
MIN_PROPOSAL_STAKE: int = 1000

# 标准投票周期：7天（秒）
STANDARD_VOTING_PERIOD: float = 7 * 24 * 3600

# 紧急投票周期：24小时（秒）
EMERGENCY_VOTING_PERIOD: float = 24 * 3600

# 法定人数比例：总供应量的10%
QUORUM_RATIO: float = 0.10

# 通过门槛：赞成票比例 ≥ 51%
APPROVAL_THRESHOLD: float = 0.51

# 治理循环检查间隔：60秒
GOVERNANCE_LOOP_INTERVAL: float = 60.0


# ============================================================================
# 枚举类型
# ============================================================================

class ProposalType(enum.Enum):
    """提案类型枚举"""
    RUN_MODEL = "RUN_MODEL"          # 提议运行指定AI模型
    ADD_MODEL = "ADD_MODEL"          # 提议注册新AI模型到网络
    REMOVE_MODEL = "REMOVE_MODEL"    # 提议从网络移除AI模型
    PARAM_CHANGE = "PARAM_CHANGE"    # 修改网络参数
    EMERGENCY = "EMERGENCY"          # 紧急操作
    UPGRADE = "UPGRADE"              # 协议升级


class ProposalStatus(enum.Enum):
    """提案状态枚举"""
    ACTIVE = "ACTIVE"      # 投票进行中
    PASSED = "PASSED"      # 投票通过
    REJECTED = "REJECTED"  # 投票未通过
    EXECUTED = "EXECUTED"  # 已执行
    EXPIRED = "EXPIRED"    # 已过期（未达法定人数）


# ============================================================================
# 提案数据类
# ============================================================================

@dataclass
class Proposal:
    """治理提案数据结构

    Attributes:
        id: 提案唯一标识
        proposer: 提案发起者地址
        proposal_type: 提案类型（RUN_MODEL / ADD_MODEL / REMOVE_MODEL / PARAM_CHANGE / EMERGENCY / UPGRADE）
        title: 提案标题
        description: 提案详细描述
        model_name: 目标模型名称（RUN_MODEL / REMOVE_MODEL 类型）
        model_info: 新模型注册信息（ADD_MODEL 类型）
        parameters: 参数修改字典（仅PARAM_CHANGE类型）
        status: 当前提案状态
        votes_for: 赞成票权重
        votes_against: 反对票权重
        voters: 投票记录 {地址: 是否赞成}
        created_at: 创建时间戳
        voting_start: 投票开始时间戳
        voting_end: 投票结束时间戳
        execution_result: 执行结果描述
        executed_at: 执行时间戳
    """
    id: int
    proposer: str
    proposal_type: str  # ProposalType value
    title: str
    description: str
    model_name: str = ""
    parameters: dict = field(default_factory=dict)
    # ADD_MODEL 提案用：新模型的注册信息
    model_info: dict = field(default_factory=dict)

    status: str = "ACTIVE"
    votes_for: int = 0
    votes_against: int = 0
    voters: dict = field(default_factory=dict)  # {address: support_bool}

    created_at: float = field(default_factory=time.time)
    voting_start: float = field(default_factory=time.time)
    voting_end: float = field(default_factory=lambda: time.time() + STANDARD_VOTING_PERIOD)

    execution_result: str = ""
    executed_at: float = 0

    def __post_init__(self) -> None:
        """初始化后校验提案类型"""
        if self.proposal_type not in [t.value for t in ProposalType]:
            raise ValueError(f"无效的提案类型: {self.proposal_type}，"
                             f"有效值: {[t.value for t in ProposalType]}")

    @property
    def total_votes(self) -> int:
        """总投票权重"""
        return self.votes_for + self.votes_against

    @property
    def approval_rate(self) -> float:
        """赞成率（0.0 ~ 1.0）"""
        if self.total_votes == 0:
            return 0.0
        return self.votes_for / self.total_votes

    @property
    def voter_count(self) -> int:
        """独立投票人数"""
        return len(self.voters)

    @property
    def is_voting_period_over(self) -> bool:
        """投票期是否已结束"""
        return time.time() >= self.voting_end

    @property
    def is_emergency(self) -> bool:
        """是否为紧急提案"""
        return self.proposal_type == ProposalType.EMERGENCY.value

    def to_dict(self) -> dict:
        """序列化为字典（用于JSON/API输出）"""
        return {
            "id": self.id,
            "proposer": self.proposer,
            "proposal_type": self.proposal_type,
            "title": self.title,
            "description": self.description,
            "model_name": self.model_name,
            "model_info": self.model_info,
            "parameters": self.parameters,
            "status": self.status,
            "votes_for": self.votes_for,
            "votes_against": self.votes_against,
            "voter_count": self.voter_count,
            "total_votes": self.total_votes,
            "approval_rate": round(self.approval_rate, 4),
            "created_at": self.created_at,
            "voting_start": self.voting_start,
            "voting_end": self.voting_end,
            "execution_result": self.execution_result,
            "executed_at": self.executed_at,
        }


# ============================================================================
# 提案执行器
# ============================================================================

class ProposalExecutor:
    """提案执行器 - 负责执行已通过的治理提案

    当提案投票通过后，ProposalExecutor 执行具体操作：
    - RUN_MODEL: 通知所有节点切换模型
    - ADD_MODEL: 注册新模型到网络模型注册表
    - REMOVE_MODEL: 从网络模型注册表移除模型
    - PARAM_CHANGE: 更新网络配置参数
    - EMERGENCY: 执行紧急安全操作
    - UPGRADE: 触发协议升级流程
    """

    def __init__(self, node_manager=None, model_registry=None) -> None:
        """
        Args:
            node_manager: 节点管理器引用（可选，用于通知节点）
            model_registry: 模型注册表引用（可选，用于 ADD_MODEL / REMOVE_MODEL）
        """
        self._node_manager = node_manager
        self._model_registry = model_registry
        # 已注册的参数变更回调 {param_key: callback}
        self._param_callbacks: Dict[str, Callable[[Any], bool]] = {}
        # 已注册的升级回调 {upgrade_id: callback}
        self._upgrade_callbacks: Dict[str, Callable[[dict], bool]] = {}
        # 执行历史记录
        self._execution_log: List[dict] = []
        self._lock = threading.Lock()

    def register_param_callback(self, param_key: str, callback: Callable[[Any], bool]) -> None:
        """注册参数变更回调函数

        当 PARAM_CHANGE 提案通过时，自动调用对应参数的回调函数。

        Args:
            param_key: 参数键名（如 "api_rate", "min_stake"）
            callback: 回调函数，接收新值，返回是否成功
        """
        with self._lock:
            self._param_callbacks[param_key] = callback
        logger.info("注册参数回调: %s", param_key)

    def register_upgrade_callback(self, upgrade_id: str,
                                   callback: Callable[[dict], bool]) -> None:
        """注册协议升级回调函数

        Args:
            upgrade_id: 升级标识符
            callback: 回调函数，接收升级参数字典，返回是否成功
        """
        with self._lock:
            self._upgrade_callbacks[upgrade_id] = callback
        logger.info("注册升级回调: %s", upgrade_id)

    def execute(self, proposal: Proposal) -> bool:
        """执行已通过的提案

        Args:
            proposal: 已通过的提案对象

        Returns:
            执行是否成功
        """
        logger.info("执行提案 #%d [%s]: %s", proposal.id, proposal.proposal_type, proposal.title)

        try:
            if proposal.proposal_type == ProposalType.RUN_MODEL.value:
                success = self._execute_model_switch(proposal)
            elif proposal.proposal_type == ProposalType.ADD_MODEL.value:
                success = self._execute_add_model(proposal)
            elif proposal.proposal_type == ProposalType.REMOVE_MODEL.value:
                success = self._execute_remove_model(proposal)
            elif proposal.proposal_type == ProposalType.PARAM_CHANGE.value:
                success = self._execute_param_change(proposal)
            elif proposal.proposal_type == ProposalType.EMERGENCY.value:
                success = self._execute_emergency(proposal)
            elif proposal.proposal_type == ProposalType.UPGRADE.value:
                success = self._execute_upgrade(proposal)
            else:
                logger.error("未知的提案类型: %s", proposal.proposal_type)
                return False

            # 记录执行结果
            execution_record = {
                "proposal_id": proposal.id,
                "proposal_type": proposal.proposal_type,
                "title": proposal.title,
                "success": success,
                "executed_at": time.time(),
            }
            with self._lock:
                self._execution_log.append(execution_record)

            if success:
                logger.info("提案 #%d 执行成功", proposal.id)
            else:
                logger.warning("提案 #%d 执行失败", proposal.id)

            return success

        except Exception as e:
            logger.error("执行提案 #%d 时发生异常: %s", proposal.id, e, exc_info=True)
            return False

    def _execute_model_switch(self, proposal: Proposal) -> bool:
        """执行模型切换

        通知所有节点切换到提案中指定的AI模型。

        Args:
            proposal: RUN_MODEL 类型的提案

        Returns:
            执行是否成功
        """
        model_name = proposal.model_name
        if not model_name:
            logger.error("提案 #%d 未指定模型名称", proposal.id)
            return False

        logger.info("切换活跃模型为: %s", model_name)

        # 如果有模型注册表，切换活跃模型
        if self._model_registry is not None:
            try:
                self._model_registry.set_active_model(model_name)
                logger.info("模型注册表活跃模型已切换: %s", model_name)
            except Exception as e:
                logger.error("切换模型注册表活跃模型失败: %s", e)

        # 如果有节点管理器，通知所有节点
        if self._node_manager is not None:
            try:
                notify_count = self._node_manager.broadcast_model_switch(model_name)
                logger.info("已通知 %d 个节点切换模型: %s", notify_count, model_name)
            except AttributeError:
                logger.warning("节点管理器不支持 broadcast_model_switch 方法")
            except Exception as e:
                logger.error("通知节点切换模型失败: %s", e)

        return True

    def _execute_add_model(self, proposal: Proposal) -> bool:
        """执行新增模型

        将提案中的新模型注册到网络模型注册表。
        提案通过后，该模型可供 RUN_MODEL 提案选择运行。

        Args:
            proposal: ADD_MODEL 类型的提案
                model_name: 新模型名称（HuggingFace格式，如 "deepseek/DeepSeek-V3"）
                model_info: 模型信息字典，需包含:
                    - min_memory_gb: 最小内存要求
                    - min_gpu_memory_gb: 最小GPU显存要求
                    - recommended_nodes: 推荐节点数
                    - category: 模型类别（chat/image/code/embedding等）
                    - description: 模型描述

        Returns:
            执行是否成功
        """
        model_name = proposal.model_name
        model_info = proposal.model_info

        if not model_name:
            logger.error("提案 #%d 未指定模型名称", proposal.id)
            return False

        if not model_info:
            logger.error("提案 #%d 未包含模型信息", proposal.id)
            return False

        logger.info("注册新模型: %s", model_name)

        if self._model_registry is not None:
            try:
                success = self._model_registry.register_model(model_name, model_info)
                if success:
                    logger.info("新模型已注册到网络: %s", model_name)

                    # 通知所有节点有新模型可用
                    if self._node_manager is not None:
                        try:
                            self._node_manager.broadcast_model_update(
                                action="add", model_name=model_name,
                                model_info=model_info
                            )
                        except Exception as e:
                            logger.warning("广播新模型通知失败: %s", e)
                else:
                    logger.error("模型注册失败: %s", model_name)
                return success
            except Exception as e:
                logger.error("注册新模型时异常: %s", e)
                return False
        else:
            logger.warning("模型注册表未初始化，无法注册模型")
            return False

    def _execute_remove_model(self, proposal: Proposal) -> bool:
        """执行移除模型

        从网络模型注册表中移除指定模型。
        注意：如果该模型当前是活跃模型，将阻止移除操作。

        Args:
            proposal: REMOVE_MODEL 类型的提案
                model_name: 要移除的模型名称

        Returns:
            执行是否成功
        """
        model_name = proposal.model_name
        if not model_name:
            logger.error("提案 #%d 未指定模型名称", proposal.id)
            return False

        logger.info("移除模型: %s", model_name)

        if self._model_registry is not None:
            try:
                # 检查是否为活跃模型，活跃模型不可移除
                active = self._model_registry.get_active_model()
                if model_name == active:
                    logger.error("无法移除当前活跃模型: %s，请先切换活跃模型", model_name)
                    return False

                # 移除模型
                if hasattr(self._model_registry, 'remove_model'):
                    success = self._model_registry.remove_model(model_name)
                    if not success:
                        logger.error("模型移除失败: %s", model_name)
                    return success

                # 通知所有节点
                if self._node_manager is not None:
                    try:
                        self._node_manager.broadcast_model_update(
                            action="remove", model_name=model_name
                        )
                    except Exception as e:
                        logger.warning("广播模型移除通知失败: %s", e)

                return True
            except Exception as e:
                logger.error("移除模型时异常: %s", e)
                return False
        else:
            logger.warning("模型注册表未初始化，无法移除模型")
            return False

    def _execute_param_change(self, proposal: Proposal) -> bool:
        """执行参数变更

        遍历提案中的参数字典，调用已注册的回调函数更新每个参数。

        Args:
            proposal: PARAM_CHANGE 类型的提案

        Returns:
            所有参数是否都成功更新
        """
        parameters = proposal.parameters
        if not parameters:
            logger.warning("提案 #%d 未包含任何参数", proposal.id)
            return False

        logger.info("执行参数变更: %s", list(parameters.keys()))

        all_success = True
        with self._lock:
            callbacks = dict(self._param_callbacks)

        for param_key, new_value in parameters.items():
            callback = callbacks.get(param_key)
            if callback is None:
                logger.warning("参数 '%s' 无注册的回调函数，跳过", param_key)
                all_success = False
                continue

            try:
                result = callback(new_value)
                if result:
                    logger.info("参数 '%s' 已更新为: %s", param_key, new_value)
                else:
                    logger.warning("参数 '%s' 更新失败 (回调返回False)", param_key)
                    all_success = False
            except Exception as e:
                logger.error("更新参数 '%s' 时异常: %s", param_key, e)
                all_success = False

        return all_success

    def _execute_emergency(self, proposal: Proposal) -> bool:
        """执行紧急操作

        紧急提案通常涉及安全漏洞修复、网络攻击响应等。
        具体操作由 parameters 字段中的 action 键指定。

        Args:
            proposal: EMERGENCY 类型的提案

        Returns:
            执行是否成功
        """
        action = proposal.parameters.get("action", "unknown")
        logger.info("执行紧急操作: %s (提案 #%d)", action, proposal.id)

        if action == "pause_network":
            return self._emergency_pause_network(proposal)
        elif action == "freeze_contracts":
            return self._emergency_freeze_contracts(proposal)
        elif action == "rollback":
            return self._emergency_rollback(proposal)
        else:
            logger.warning("未知的紧急操作类型: %s", action)
            return False

    def _emergency_pause_network(self, proposal: Proposal) -> bool:
        """紧急暂停网络"""
        logger.warning("紧急操作: 暂停整个网络 (提案 #%d)", proposal.id)
        if self._node_manager is not None:
            try:
                self._node_manager.broadcast_pause()
                logger.info("已通知所有节点暂停")
            except Exception as e:
                logger.error("通知节点暂停失败: %s", e)
                return False
        return True

    def _emergency_freeze_contracts(self, proposal: Proposal) -> bool:
        """紧急冻结合约"""
        logger.warning("紧急操作: 冻结智能合约 (提案 #%d)", proposal.id)
        # 合约冻结逻辑由区块链层实现
        return True

    def _emergency_rollback(self, proposal: Proposal) -> bool:
        """紧急回滚"""
        target_block = proposal.parameters.get("target_block", 0)
        logger.warning("紧急操作: 回滚到区块 %d (提案 #%d)", target_block, proposal.id)
        # 回滚逻辑由区块链层实现
        return True

    def _execute_upgrade(self, proposal: Proposal) -> bool:
        """执行协议升级

        Args:
            proposal: UPGRADE 类型的提案

        Returns:
            执行是否成功
        """
        upgrade_version = proposal.parameters.get("version", "unknown")
        upgrade_id = proposal.parameters.get("upgrade_id", "")

        logger.info("执行协议升级到版本: %s (提案 #%d)", upgrade_version, proposal.id)

        with self._lock:
            callbacks = dict(self._upgrade_callbacks)

        if upgrade_id and upgrade_id in callbacks:
            try:
                return callbacks[upgrade_id](proposal.parameters)
            except Exception as e:
                logger.error("执行升级回调失败: %s", e)
                return False

        # 默认：标记升级已批准，等待节点自愿升级
        logger.info("协议升级 v%s 已获批准，等待节点升级", upgrade_version)
        return True

    def get_execution_log(self, limit: int = 50) -> List[dict]:
        """获取执行日志

        Args:
            limit: 返回的最大记录数

        Returns:
            执行记录列表
        """
        with self._lock:
            return list(reversed(self._execution_log[-limit:]))


# ============================================================================
# 模型注册表
# ============================================================================

class ModelRegistry:
    """模型注册表 - 管理网络可以运行的AI模型

    维护所有可用模型的元信息，包括硬件要求、推荐节点数等。
    当前活跃模型由治理投票决定。
    """

    def __init__(self) -> None:
        self._models: Dict[str, dict] = {
            "Qwen/Qwen2.5-0.5B-Instruct": {
                "min_memory_gb": 2,
                "min_gpu_memory_gb": 0,
                "recommended_nodes": 1,
                "category": "chat",
                "description": "Qwen2.5 0.5B 超轻量对话模型，适合低资源环境",
            },
            "Qwen/Qwen2.5-1.5B-Instruct": {
                "min_memory_gb": 4,
                "min_gpu_memory_gb": 0,
                "recommended_nodes": 1,
                "category": "chat",
                "description": "Qwen2.5 1.5B 轻量对话模型",
            },
            "Qwen/Qwen2.5-7B-Instruct": {
                "min_memory_gb": 16,
                "min_gpu_memory_gb": 8,
                "recommended_nodes": 1,
                "category": "chat",
                "description": "Qwen2.5 7B 中型对话模型，需GPU加速",
            },
            "Qwen/Qwen2.5-72B-Instruct": {
                "min_memory_gb": 144,
                "min_gpu_memory_gb": 72,
                "recommended_nodes": 4,
                "category": "chat",
                "description": "Qwen2.5 72B 大型对话模型，需要多节点分布式推理",
            },
            "meta-llama/Llama-3-70B": {
                "min_memory_gb": 140,
                "min_gpu_memory_gb": 70,
                "recommended_nodes": 4,
                "category": "chat",
                "description": "Meta Llama-3 70B 大型语言模型",
            },
            "stabilityai/stable-diffusion-xl-base-1.0": {
                "min_memory_gb": 12,
                "min_gpu_memory_gb": 8,
                "recommended_nodes": 1,
                "category": "image",
                "description": "Stable Diffusion XL 图像生成模型",
            },
        }
        # 当前活跃模型（由治理投票决定）
        self._active_model: str = "Qwen/Qwen2.5-7B-Instruct"
        # 模型切换历史
        self._switch_history: List[dict] = []
        self._lock = threading.Lock()

    def get_active_model(self) -> str:
        """获取当前活跃模型（由治理投票决定）

        Returns:
            当前活跃模型的名称
        """
        with self._lock:
            return self._active_model

    def set_active_model(self, model_name: str) -> bool:
        """设置当前活跃模型

        通常由 ProposalExecutor 在模型切换提案通过后调用。

        Args:
            model_name: 模型名称

        Returns:
            设置是否成功
        """
        with self._lock:
            if model_name not in self._models:
                logger.error("模型未注册: %s", model_name)
                return False

            old_model = self._active_model
            self._active_model = model_name
            self._switch_history.append({
                "from": old_model,
                "to": model_name,
                "timestamp": time.time(),
            })
            logger.info("活跃模型已切换: %s -> %s", old_model, model_name)
            return True

    def get_model_info(self, model_name: str) -> Optional[dict]:
        """获取模型信息

        Args:
            model_name: 模型名称

        Returns:
            模型信息字典，如果模型不存在则返回None
        """
        with self._lock:
            info = self._models.get(model_name)
            if info is None:
                return None
            # 返回副本，防止外部修改
            return {
                "name": model_name,
                **dict(info),
            }

    def get_all_models(self) -> Dict[str, dict]:
        """获取所有已注册模型的信息

        Returns:
            模型名称到模型信息的映射字典
        """
        with self._lock:
            return {name: dict(info) for name, info in self._models.items() }

    def register_model(self, model_name: str, info: dict) -> bool:
        """注册新模型

        Args:
            model_name: 模型名称
            info: 模型信息字典，需包含 min_memory_gb, category 等字段

        Returns:
            注册是否成功
        """
        required_keys = {"min_memory_gb", "min_gpu_memory_gb",
                         "recommended_nodes", "category"}
        if not required_keys.issubset(info.keys()):
            missing = required_keys - info.keys()
            logger.error("注册模型缺少必要字段: %s", missing)
            return False

        with self._lock:
            if model_name in self._models:
                logger.warning("模型已存在，将更新: %s", model_name)
            self._models[model_name] = dict(info)
            logger.info("模型已注册/更新: %s", model_name)
        return True

    def remove_model(self, model_name: str) -> bool:
        """移除已注册的模型

        Args:
            model_name: 要移除的模型名称

        Returns:
            移除是否成功

        Raises:
            ValueError: 尝试移除当前活跃模型时抛出
        """
        with self._lock:
            if model_name not in self._models:
                logger.error("模型不存在，无法移除: %s", model_name)
                return False

            if model_name == self._active_model:
                raise ValueError(f"不能移除当前活跃模型: {model_name}，请先切换活跃模型")

            del self._models[model_name]
            logger.info("模型已移除: %s", model_name)
        return True

    def can_network_run_model(self, model_name: str, total_resources: dict) -> bool:
        """检查网络是否有足够资源运行指定模型

        Args:
            model_name: 目标模型名称
            total_resources: 网络总资源字典，包含:
                - total_memory_gb: 总内存 (GB)
                - total_gpu_memory_gb: 总GPU显存 (GB)
                - total_nodes: 总节点数

        Returns:
            网络是否能运行该模型
        """
        model_info = self.get_model_info(model_name)
        if model_info is None:
            logger.warning("检查资源时模型不存在: %s", model_name)
            return False

        total_mem = total_resources.get("total_memory_gb", 0)
        total_gpu = total_resources.get("total_gpu_memory_gb", 0)
        total_nodes = total_resources.get("total_nodes", 0)

        if total_mem < model_info["min_memory_gb"]:
            logger.info("内存不足: 需要 %.1f GB, 可用 %.1f GB",
                        model_info["min_memory_gb"], total_mem)
            return False

        if total_gpu < model_info["min_gpu_memory_gb"]:
            logger.info("GPU显存不足: 需要 %.1f GB, 可用 %.1f GB",
                        model_info["min_gpu_memory_gb"], total_gpu)
            return False

        if total_nodes < model_info["recommended_nodes"]:
            logger.info("节点数不足: 推荐 %d 节点, 可用 %d 节点",
                        model_info["recommended_nodes"], total_nodes)
            return False

        return True

    def get_switch_history(self, limit: int = 20) -> List[dict]:
        """获取模型切换历史

        Args:
            limit: 返回的最大记录数

        Returns:
            切换记录列表
        """
        with self._lock:
            return list(reversed(self._switch_history[-limit:]))


# ============================================================================
# 治理管理器
# ============================================================================

class GovernanceManager:
    """AICoin治理管理器 - 管理提案创建、投票和执行

    核心职责：
    - 管理治理提案的生命周期（创建 → 投票 → 结算 → 执行）
    - 处理代币加权投票（1 AIC = 1 票）
    - 支持投票委托机制
    - 自动检查提案到期并执行通过的提案
    - 维护完整的治理历史记录

    使用示例:
        gm = GovernanceManager(blockchain_manager)
        proposal_id = gm.create_model_proposal("0xABC...", "Qwen/Qwen2.5-7B-Instruct", "运行7B模型")
        gm.vote("0xDEF...", proposal_id, support=True)
        gm.start_governance_loop()
    """

    def __init__(self, blockchain_manager=None, node_manager=None) -> None:
        """
        Args:
            blockchain_manager: 区块链管理器引用，用于查询代币余额和总供应量
            node_manager: 节点管理器引用，用于执行提案时通知节点
        """
        self._blockchain_manager = blockchain_manager
        self._model_registry = ModelRegistry()
        self._executor = ProposalExecutor(
            node_manager=node_manager,
            model_registry=self._model_registry
        )

        # 提案存储 {proposal_id: Proposal}
        self._proposals: Dict[int, Proposal] = {}
        # 投票委托 {delegator: delegate}
        self._delegations: Dict[str, str] = {}
        # 地址投票历史 {address: [proposal_id, ...]}
        self._voter_history: Dict[str, List[int]] = {}

        # 自增提案ID
        self._next_proposal_id: int = 1
        # 治理循环控制
        self._governance_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 线程锁
        self._lock = threading.RLock()

        # 网络配置参数（可通过 PARAM_CHANGE 提案修改）
        self._network_params: Dict[str, Any] = {
            "api_rate_per_1k_tokens": 0.01,  # API调用费率 (AIC/1K tokens)
            "min_stake": 100,                  # 最低质押量
            "reward_rate": 0.1,               # 奖励比率
            "max_proposals_per_day": 10,       # 每日最大提案数
            "block_reward": 50,                # 出块奖励
        }

        # 注册参数变更回调，使治理提案可以直接修改网络参数
        self._executor.register_param_callback(
            "api_rate_per_1k_tokens",
            lambda v: self._set_param("api_rate_per_1k_tokens", v)
        )
        self._executor.register_param_callback(
            "min_stake",
            lambda v: self._set_param("min_stake", v)
        )
        self._executor.register_param_callback(
            "reward_rate",
            lambda v: self._set_param("reward_rate", v)
        )
        self._executor.register_param_callback(
            "max_proposals_per_day",
            lambda v: self._set_param("max_proposals_per_day", v)
        )
        self._executor.register_param_callback(
            "block_reward",
            lambda v: self._set_param("block_reward", v)
        )

        # 注册模型切换回调，使 RUN_MODEL 提案可以切换活跃模型
        self._executor.register_param_callback(
            "__model_switch__",
            lambda v: self._model_registry.set_active_model(v)
        )

        logger.info("治理管理器初始化完成")

    # ---- 内部辅助方法 ----

    def _get_balance(self, address: str) -> int:
        """获取地址的AIC代币余额

        Args:
            address: 钱包地址

        Returns:
            AIC代币余额（整数）
        """
        if self._blockchain_manager is not None:
            try:
                return self._blockchain_manager.get_balance(address)
            except (AttributeError, Exception) as e:
                logger.debug("从区块链获取余额失败: %s，使用默认值0", e)
        return 0

    def _get_total_supply(self) -> int:
        """获取AIC代币总供应量

        Returns:
            总供应量（整数）
        """
        if self._blockchain_manager is not None:
            try:
                return self._blockchain_manager.get_total_supply()
            except (AttributeError, Exception) as e:
                logger.debug("从区块链获取总供应量失败: %s，使用默认值100000000", e)
        # 默认总供应量：1亿 AIC
        return 100_000_000

    def _set_param(self, key: str, value: Any) -> bool:
        """内部方法：设置网络参数"""
        with self._lock:
            old_value = self._network_params.get(key)
            self._network_params[key] = value
            logger.info("网络参数已更新: %s = %s (原值: %s)", key, value, old_value)
        return True

    def _generate_proposal_id(self) -> int:
        """生成下一个提案ID（线程安全）"""
        with self._lock:
            pid = self._next_proposal_id
            self._next_proposal_id += 1
            return pid

    def _get_quorum_threshold(self) -> int:
        """计算法定人数阈值

        Returns:
            达到法定人数所需的最小投票权重
        """
        return int(self._get_total_supply() * QUORUM_RATIO)

    def _finalize_proposal(self, proposal: Proposal) -> None:
        """结算提案 - 判断提案通过或拒绝

        根据投票结果判断提案是否通过：
        1. 检查是否达到法定人数（总供应量的10%）
        2. 检查赞成率是否达到51%

        Args:
            proposal: 到期的提案
        """
        quorum = self._get_quorum_threshold()
        total_votes = proposal.total_votes

        if total_votes < quorum:
            proposal.status = ProposalStatus.EXPIRED.value
            logger.info("提案 #%d 未达法定人数: %d < %d, 标记为EXPIRED",
                        proposal.id, total_votes, quorum)
        elif proposal.approval_rate >= APPROVAL_THRESHOLD:
            proposal.status = ProposalStatus.PASSED.value
            logger.info("提案 #%d 通过: 赞成率=%.2f%%, 总票数=%d",
                        proposal.id, proposal.approval_rate * 100, total_votes)
        else:
            proposal.status = ProposalStatus.REJECTED.value
            logger.info("提案 #%d 被拒绝: 赞成率=%.2f%% < %.2f%%",
                        proposal.id, proposal.approval_rate * 100,
                        APPROVAL_THRESHOLD * 100)

    # ==== 提案创建 ====

    def create_model_proposal(self, proposer: str, model_name: str,
                              description: str) -> int:
        """创建模型运行提案

        提议网络运行指定的AI模型。提案通过后，所有节点将切换到该模型。

        Args:
            proposer: 提案发起者地址
            model_name: 目标模型名称（必须在ModelRegistry中注册）
            description: 提案详细描述

        Returns:
            提案ID，如果创建失败返回-1

        示例:
            >>> pid = gm.create_model_proposal(
            ...     "0xABC...",
            ...     "Qwen/Qwen2.5-7B-Instruct",
            ...     "提议运行 Qwen2.5-7B 模型以提供高质量对话服务"
            ... )
        """
        # 校验模型是否已注册
        if self._model_registry.get_model_info(model_name) is None:
            logger.error("模型未注册，无法创建提案: %s", model_name)
            return -1

        # 校验提案者是否有足够代币
        proposer_balance = self._get_balance(proposer)
        if proposer_balance < MIN_PROPOSAL_STAKE:
            logger.error("提案者余额不足: %d < %d (MIN_PROPOSAL_STAKE)",
                         proposer_balance, MIN_PROPOSAL_STAKE)
            return -1

        with self._lock:
            proposal_id = self._generate_proposal_id()
            proposal = Proposal(
                id=proposal_id,
                proposer=proposer,
                proposal_type=ProposalType.RUN_MODEL.value,
                title=f"运行模型: {model_name}",
                description=description,
                model_name=model_name,
            )
            self._proposals[proposal_id] = proposal

        logger.info("模型运行提案已创建 #%d: %s (提案者: %s)",
                     proposal_id, model_name, proposer)
        return proposal_id

    def create_add_model_proposal(self, proposer: str, model_name: str,
                                   description: str, model_info: dict) -> int:
        """创建新增模型提案

        提议将新的AI模型注册到网络中。提案通过后，该模型将加入模型注册表，
        可供后续的 RUN_MODEL 提案选择运行。

        Args:
            proposer: 提案发起者地址
            model_name: 新模型名称（HuggingFace格式，如 "deepseek/DeepSeek-V3"）
            description: 提案详细描述
            model_info: 模型信息字典，需包含:
                - min_memory_gb (float): 最小内存要求 (GB)
                - min_gpu_memory_gb (float): 最小GPU显存要求 (GB)
                - recommended_nodes (int): 推荐节点数
                - category (str): 模型类别 (chat/image/code/embedding等)
                - description (str): 模型描述

        Returns:
            提案ID，如果创建失败返回-1

        示例:
            >>> pid = gm.create_add_model_proposal(
            ...     "0xABC...",
            ...     "deepseek/DeepSeek-V3",
            ...     "提议注册 DeepSeek-V3 模型，提供更高质量的中文对话能力",
            ...     {
            ...         "min_memory_gb": 240,
            ...         "min_gpu_memory_gb": 80,
            ...         "recommended_nodes": 4,
            ...         "category": "chat",
            ...         "description": "DeepSeek-V3 671B MoE 大型语言模型"
            ...     }
            ... )
        """
        # 校验模型名称格式
        if not model_name or len(model_name) < 3:
            logger.error("模型名称无效: %s", model_name)
            return -1

        # 校验模型是否已存在
        if self._model_registry.get_model_info(model_name) is not None:
            logger.error("模型已注册，无需重复添加: %s", model_name)
            return -1

        # 校验模型信息完整性
        required_keys = {"min_memory_gb", "min_gpu_memory_gb",
                         "recommended_nodes", "category"}
        if not required_keys.issubset(model_info.keys()):
            missing = required_keys - model_info.keys()
            logger.error("模型信息缺少必要字段: %s", missing)
            return -1

        # 校验提案者代币
        proposer_balance = self._get_balance(proposer)
        if proposer_balance < MIN_PROPOSAL_STAKE:
            logger.error("提案者余额不足: %d < %d (MIN_PROPOSAL_STAKE)",
                         proposer_balance, MIN_PROPOSAL_STAKE)
            return -1

        with self._lock:
            proposal_id = self._generate_proposal_id()
            proposal = Proposal(
                id=proposal_id,
                proposer=proposer,
                proposal_type=ProposalType.ADD_MODEL.value,
                title=f"新增模型: {model_name}",
                description=description,
                model_name=model_name,
                model_info=dict(model_info),
            )
            self._proposals[proposal_id] = proposal

        logger.info("新增模型提案已创建 #%d: %s (提案者: %s)",
                     proposal_id, model_name, proposer)
        return proposal_id

    def create_remove_model_proposal(self, proposer: str, model_name: str,
                                      description: str) -> int:
        """创建移除模型提案

        提议从网络中移除指定的AI模型。提案通过后，该模型将从注册表中删除。
        注意：当前活跃模型不能被移除。

        Args:
            proposer: 提案发起者地址
            model_name: 要移除的模型名称
            description: 提案详细描述（建议说明移除原因）

        Returns:
            提案ID，如果创建失败返回-1

        示例:
            >>> pid = gm.create_remove_model_proposal(
            ...     "0xABC...",
            ...     "Qwen/Qwen2.5-0.5B-Instruct",
            ...     "该模型性能已落后，建议移除以减少维护负担"
            ... )
        """
        # 校验模型是否存在
        if self._model_registry.get_model_info(model_name) is None:
            logger.error("模型不在注册表中，无法移除: %s", model_name)
            return -1

        # 校验是否为活跃模型（活跃模型不能直接移除）
        if model_name == self._model_registry.get_active_model():
            logger.error("不能移除当前活跃模型: %s，请先通过 RUN_MODEL 提案切换",
                         model_name)
            return -1

        # 校验提案者代币
        proposer_balance = self._get_balance(proposer)
        if proposer_balance < MIN_PROPOSAL_STAKE:
            logger.error("提案者余额不足: %d < %d (MIN_PROPOSAL_STAKE)",
                         proposer_balance, MIN_PROPOSAL_STAKE)
            return -1

        with self._lock:
            proposal_id = self._generate_proposal_id()
            proposal = Proposal(
                id=proposal_id,
                proposer=proposer,
                proposal_type=ProposalType.REMOVE_MODEL.value,
                title=f"移除模型: {model_name}",
                description=description,
                model_name=model_name,
            )
            self._proposals[proposal_id] = proposal

        logger.info("移除模型提案已创建 #%d: %s (提案者: %s)",
                     proposal_id, model_name, proposer)
        return proposal_id

    def create_param_proposal(self, proposer: str, title: str,
                              description: str, parameters: dict) -> int:
        """创建参数修改提案

        提议修改网络的配置参数，如API费率、质押要求、奖励比率等。
        提案通过后，参数将自动更新。

        Args:
            proposer: 提案发起者地址
            title: 提案标题
            description: 提案详细描述
            parameters: 参数字典，如 {"api_rate_per_1k_tokens": 0.02}

        Returns:
            提案ID，如果创建失败返回-1

        示例:
            >>> pid = gm.create_param_proposal(
            ...     "0xABC...",
            ...     "调整API调用费率",
            ...     "将API调用费率从0.01调整到0.02 AIC/1K tokens",
            ...     {"api_rate_per_1k_tokens": 0.02}
            ... )
        """
        if not parameters:
            logger.error("参数修改提案必须包含参数")
            return -1

        if not isinstance(parameters, dict):
            logger.error("参数必须是字典类型")
            return -1

        proposer_balance = self._get_balance(proposer)
        if proposer_balance < MIN_PROPOSAL_STAKE:
            logger.error("提案者余额不足: %d < %d",
                         proposer_balance, MIN_PROPOSAL_STAKE)
            return -1

        with self._lock:
            proposal_id = self._generate_proposal_id()
            proposal = Proposal(
                id=proposal_id,
                proposer=proposer,
                proposal_type=ProposalType.PARAM_CHANGE.value,
                title=title,
                description=description,
                parameters=dict(parameters),
            )
            self._proposals[proposal_id] = proposal

        logger.info("参数修改提案已创建 #%d: %s (提案者: %s, 参数: %s)",
                     proposal_id, title, proposer, list(parameters.keys()))
        return proposal_id

    def create_emergency_proposal(self, proposer: str, title: str,
                                  description: str) -> int:
        """创建紧急提案（24小时投票期）

        用于紧急情况，如安全漏洞修复、网络攻击响应等。
        紧急提案的投票期缩短为24小时。

        Args:
            proposer: 提案发起者地址
            title: 提案标题
            description: 紧急情况描述

        Returns:
            提案ID，如果创建失败返回-1

        示例:
            >>> pid = gm.create_emergency_proposal(
            ...     "0xABC...",
            ...     "紧急: 暂停网络",
            ...     "发现安全漏洞，需要立即暂停网络进行修复"
            ... )
        """
        proposer_balance = self._get_balance(proposer)
        if proposer_balance < MIN_PROPOSAL_STAKE:
            logger.error("提案者余额不足: %d < %d",
                         proposer_balance, MIN_PROPOSAL_STAKE)
            return -1

        now = time.time()
        with self._lock:
            proposal_id = self._generate_proposal_id()
            proposal = Proposal(
                id=proposal_id,
                proposer=proposer,
                proposal_type=ProposalType.EMERGENCY.value,
                title=title,
                description=description,
                voting_start=now,
                voting_end=now + EMERGENCY_VOTING_PERIOD,
                parameters={"action": "pause_network"},
            )
            self._proposals[proposal_id] = proposal

        logger.info("紧急提案已创建 #%d: %s (投票期: 24小时, 提案者: %s)",
                     proposal_id, title, proposer)
        return proposal_id

    def create_upgrade_proposal(self, proposer: str, title: str,
                                description: str, version: str,
                                upgrade_id: str = "",
                                extra_params: Optional[dict] = None) -> int:
        """创建协议升级提案

        Args:
            proposer: 提案发起者地址
            title: 提案标题
            description: 升级描述
            version: 目标版本号
            upgrade_id: 升级标识符
            extra_params: 额外升级参数

        Returns:
            提案ID，如果创建失败返回-1
        """
        proposer_balance = self._get_balance(proposer)
        if proposer_balance < MIN_PROPOSAL_STAKE:
            logger.error("提案者余额不足: %d < %d",
                         proposer_balance, MIN_PROPOSAL_STAKE)
            return -1

        parameters = {"version": version, "upgrade_id": upgrade_id}
        if extra_params:
            parameters.update(extra_params)

        with self._lock:
            proposal_id = self._generate_proposal_id()
            proposal = Proposal(
                id=proposal_id,
                proposer=proposer,
                proposal_type=ProposalType.UPGRADE.value,
                title=title,
                description=description,
                parameters=parameters,
            )
            self._proposals[proposal_id] = proposal

        logger.info("升级提案已创建 #%d: v%s (提案者: %s)",
                     proposal_id, version, proposer)
        return proposal_id

    # ==== 投票 ====

    def vote(self, voter: str, proposal_id: int, support: bool) -> bool:
        """对提案进行投票

        每个地址对每个提案只能投票一次。投票权重等于地址持有的AIC代币数量
        （包括委托给该地址的投票权）。

        Args:
            voter: 投票者地址
            proposal_id: 提案ID
            support: True=赞成, False=反对

        Returns:
            投票是否记录成功

        示例:
            >>> gm.vote("0xDEF...", proposal_id=1, support=True)
        """
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                logger.error("提案不存在: #%d", proposal_id)
                return False

            # 检查提案状态
            if proposal.status != ProposalStatus.ACTIVE.value:
                logger.error("提案 #%d 状态为 %s，无法投票",
                             proposal_id, proposal.status)
                return False

            # 检查投票期是否已结束
            if proposal.is_voting_period_over:
                logger.error("提案 #%d 投票期已结束", proposal_id)
                return False

            # 检查是否已投票
            if voter in proposal.voters:
                logger.warning("地址 %s 已对提案 #%d 投过票", voter, proposal_id)
                return False

            # 计算投票权重
            vote_weight = self.get_vote_weight(voter)
            if vote_weight <= 0:
                logger.error("地址 %s 的投票权重为0，无法投票", voter)
                return False

            # 记录投票
            proposal.voters[voter] = support
            if support:
                proposal.votes_for += vote_weight
            else:
                proposal.votes_against += vote_weight

            # 更新投票历史
            if voter not in self._voter_history:
                self._voter_history[voter] = []
            self._voter_history[voter].append(proposal_id)

        logger.info("投票记录成功: %s 对提案 #%d 投了 %s 票 (权重: %d AIC)",
                     voter, proposal_id, "赞成" if support else "反对",
                     vote_weight)
        return True

    def delegate_vote(self, delegator: str, delegate: str) -> bool:
        """委托投票权

        将自己的投票权委托给另一个地址。被委托者将在投票时获得
        委托者的代币权重。

        Args:
            delegator: 委托者地址
            delegate: 被委托者地址

        Returns:
            委托是否成功

        注意:
            - 不能委托给自己
            - 新委托将覆盖旧委托
            - 委托不会影响已完成的投票
        """
        if delegator == delegate:
            logger.error("不能委托给自己: %s", delegator)
            return False

        # 校验双方都有余额
        delegator_balance = self._get_balance(delegator)
        if delegator_balance <= 0:
            logger.error("委托者 %s 余额为0，无法委托", delegator)
            return False

        delegate_balance = self._get_balance(delegate)
        if delegate_balance <= 0:
            logger.error("被委托者 %s 余额为0", delegate)
            return False

        with self._lock:
            self._delegations[delegator] = delegate

        logger.info("投票权委托: %s -> %s (委托权重: %d AIC)",
                     delegator, delegate, delegator_balance)
        return True

    def revoke_delegation(self, delegator: str) -> bool:
        """撤销投票委托

        Args:
            delegator: 要撤销委托的地址

        Returns:
            撤销是否成功
        """
        with self._lock:
            if delegator not in self._delegations:
                logger.warning("地址 %s 没有活跃的委托", delegator)
                return False
            delegate = self._delegations.pop(delegator)

        logger.info("投票委托已撤销: %s -> %s", delegator, delegate)
        return True

    def get_vote_weight(self, address: str) -> int:
        """获取地址的投票权重

        投票权重 = 地址自身持有的AIC数量 + 所有委托给该地址的投票权重

        Args:
            address: 钱包地址

        Returns:
            总投票权重（AIC数量）
        """
        weight = self._get_balance(address)

        # 加上所有委托给该地址的投票权
        with self._lock:
            for delegator, delegate in self._delegations.items():
                if delegate == address:
                    weight += self._get_balance(delegator)

        return weight

    def get_delegations_to(self, address: str) -> List[str]:
        """获取委托给指定地址的所有委托者

        Args:
            address: 被委托者地址

        Returns:
            委托者地址列表
        """
        with self._lock:
            return [d for d, t in self._delegations.items() if t == address]

    def get_delegation(self, delegator: str) -> Optional[str]:
        """获取地址的委托目标

        Args:
            delegator: 委托者地址

        Returns:
            被委托者地址，如果没有委托则返回None
        """
        with self._lock:
            return self._delegations.get(delegator)

    # ==== 提案执行 ====

    def check_and_execute_proposals(self) -> List[int]:
        """检查所有到期提案并自动执行通过的提案

        遍历所有ACTIVE状态的提案，对已到期的提案进行结算。
        结算通过的提案将自动执行。

        Returns:
            已执行的提案ID列表

        注意:
            此方法由治理循环定期调用，也可手动调用
        """
        executed_ids: List[int] = []

        with self._lock:
            for proposal in list(self._proposals.values()):
                if proposal.status != ProposalStatus.ACTIVE.value:
                    continue
                if not proposal.is_voting_period_over:
                    continue

                # 结算提案
                self._finalize_proposal(proposal)

                # 如果通过，尝试执行
                if proposal.status == ProposalStatus.PASSED.value:
                    success = self._execute_passed_proposal(proposal)
                    if success:
                        executed_ids.append(proposal.id)

        return executed_ids

    def _execute_passed_proposal(self, proposal: Proposal) -> bool:
        """执行已通过的提案

        内部方法，在持有锁的情况下调用。
        对于 RUN_MODEL 提案，先通过executor执行模型切换回调。

        Args:
            proposal: 已通过的提案

        Returns:
            执行是否成功
        """
        # RUN_MODEL 提案需要设置模型切换参数
        if proposal.proposal_type == ProposalType.RUN_MODEL.value:
            proposal.parameters["__model_switch__"] = proposal.model_name

        # 通过执行器执行
        success = self._executor.execute(proposal)

        if success:
            proposal.status = ProposalStatus.EXECUTED.value
            proposal.execution_result = "执行成功"
            proposal.executed_at = time.time()
            logger.info("提案 #%d 已成功执行", proposal.id)
        else:
            proposal.execution_result = "执行失败"
            logger.warning("提案 #%d 执行失败，保持PASSED状态待重试", proposal.id)

        return success

    def execute_proposal(self, proposal_id: int) -> bool:
        """手动执行已通过的提案

        用于重新执行之前失败的已通过提案。

        Args:
            proposal_id: 提案ID

        Returns:
            执行是否成功

        Raises:
            ValueError: 如果提案不存在或状态不允许执行
        """
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise ValueError(f"提案不存在: #{proposal_id}")

            if proposal.status == ProposalStatus.EXECUTED.value:
                logger.warning("提案 #%d 已经执行过", proposal_id)
                return False

            if proposal.status != ProposalStatus.PASSED.value:
                raise ValueError(f"提案 #{proposal_id} 状态为 {proposal.status}，"
                                 f"只有PASSED状态的提案可以执行")

            success = self._execute_passed_proposal(proposal)

        return success

    def get_proposal_result(self, proposal_id: int) -> dict:
        """获取提案的投票结果详情

        Args:
            proposal_id: 提案ID

        Returns:
            包含投票结果详细信息的字典:
            {
                "status": 提案状态,
                "votes_for": 赞成票权重,
                "votes_against": 反对票权重,
                "voter_count": 投票人数,
                "total_votes": 总票数,
                "quorum_reached": 是否达到法定人数,
                "approval_rate": 赞成率,
                "top_voters": 前10大投票者列表,
                "time_remaining": 剩余投票时间(秒),
            }

        Raises:
            ValueError: 如果提案不存在
        """
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise ValueError(f"提案不存在: #{proposal_id}")

            quorum = self._get_quorum_threshold()

            # 获取前10大投票者（按投票权重降序）
            voter_weights = [
                (addr, self._get_balance(addr), support)
                for addr, support in proposal.voters.items()
            ]
            voter_weights.sort(key=lambda x: x[1], reverse=True)
            top_voters = [
                {
                    "address": addr,
                    "weight": weight,
                    "vote": "FOR" if support else "AGAINST",
                }
                for addr, weight, support in voter_weights[:10]
            ]

            # 计算剩余投票时间
            now = time.time()
            time_remaining = max(0, proposal.voting_end - now)

            result = {
                "proposal_id": proposal.id,
                "title": proposal.title,
                "proposal_type": proposal.proposal_type,
                "status": proposal.status,
                "votes_for": proposal.votes_for,
                "votes_against": proposal.votes_against,
                "voter_count": proposal.voter_count,
                "total_votes": proposal.total_votes,
                "quorum_threshold": quorum,
                "quorum_reached": proposal.total_votes >= quorum,
                "approval_rate": round(proposal.approval_rate, 4),
                "top_voters": top_voters,
                "time_remaining": time_remaining,
                "created_at": proposal.created_at,
                "voting_end": proposal.voting_end,
                "execution_result": proposal.execution_result,
                "executed_at": proposal.executed_at,
            }

        return result

    # ==== 查询方法 ====

    def get_proposal(self, proposal_id: int) -> Optional[Proposal]:
        """获取指定提案

        Args:
            proposal_id: 提案ID

        Returns:
            Proposal对象，如果不存在则返回None
        """
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                return None
            # 返回基本信息的字典副本，避免外部直接修改
            return proposal

    def get_active_proposals(self) -> List[dict]:
        """获取所有活跃（投票中）的提案

        Returns:
            活跃提案的字典列表，按创建时间降序排列
        """
        with self._lock:
            active = [
                p.to_dict()
                for p in self._proposals.values()
                if p.status == ProposalStatus.ACTIVE.value
            ]
        active.sort(key=lambda x: x["created_at"], reverse=True)
        return active

    def get_proposal_history(self, limit: int = 50) -> List[dict]:
        """获取历史提案（已完成投票的提案）

        Args:
            limit: 返回的最大记录数

        Returns:
            历史提案的字典列表，按创建时间降序排列
        """
        with self._lock:
            history = [
                p.to_dict()
                for p in self._proposals.values()
                if p.status != ProposalStatus.ACTIVE.value
            ]
        history.sort(key=lambda x: x["created_at"], reverse=True)
        return history[:limit]

    def get_voter_history(self, address: str) -> List[dict]:
        """获取地址的投票历史

        Args:
            address: 钱包地址

        Returns:
            投票记录列表，每条记录包含提案ID、投票方向等信息
        """
        with self._lock:
            proposal_ids = self._voter_history.get(address, [])

        records = []
        for pid in reversed(proposal_ids):
            proposal = self._proposals.get(pid)
            if proposal is None:
                continue
            support = proposal.voters.get(address, None)
            records.append({
                "proposal_id": pid,
                "title": proposal.title,
                "proposal_type": proposal.proposal_type,
                "vote": "FOR" if support else "AGAINST",
                "status": proposal.status,
                "voted_at": None,  # 投票时间可扩展
            })

        return records

    def get_network_params(self) -> dict:
        """获取当前网络参数

        Returns:
            网络参数字典的副本
        """
        with self._lock:
            return dict(self._network_params)

    def get_governance_stats(self) -> dict:
        """获取治理统计概览

        Returns:
            统计信息字典，包含提案数、投票率等
        """
        with self._lock:
            total = len(self._proposals)
            active_count = sum(
                1 for p in self._proposals.values()
                if p.status == ProposalStatus.ACTIVE.value
            )
            passed_count = sum(
                1 for p in self._proposals.values()
                if p.status == ProposalStatus.PASSED.value
            )
            executed_count = sum(
                1 for p in self._proposals.values()
                if p.status == ProposalStatus.EXECUTED.value
            )
            rejected_count = sum(
                1 for p in self._proposals.values()
                if p.status == ProposalStatus.REJECTED.value
            )
            expired_count = sum(
                1 for p in self._proposals.values()
                if p.status == ProposalStatus.EXPIRED.value
            )
            total_voters = len(self._voter_history)
            total_delegations = len(self._delegations)
            active_model = self._model_registry.get_active_model()

        return {
            "total_proposals": total,
            "active_proposals": active_count,
            "passed_proposals": passed_count,
            "executed_proposals": executed_count,
            "rejected_proposals": rejected_count,
            "expired_proposals": expired_count,
            "total_unique_voters": total_voters,
            "active_delegations": total_delegations,
            "active_model": active_model,
            "quorum_threshold": self._get_quorum_threshold(),
            "approval_threshold": APPROVAL_THRESHOLD,
        }

    @property
    def model_registry(self) -> ModelRegistry:
        """获取模型注册表实例"""
        return self._model_registry

    @property
    def executor(self) -> ProposalExecutor:
        """获取提案执行器实例"""
        return self._executor

    # ==== 后台治理循环 ====

    def start_governance_loop(self) -> None:
        """启动治理循环（后台线程）

        启动一个后台线程，定期检查提案到期并自动执行通过的提案。
        默认每60秒检查一次。
        """
        if self._governance_thread is not None and self._governance_thread.is_alive():
            logger.warning("治理循环已在运行")
            return

        self._stop_event.clear()
        self._governance_thread = threading.Thread(
            target=self._governance_loop_worker,
            name="governance-loop",
            daemon=True,
        )
        self._governance_thread.start()
        logger.info("治理循环已启动 (间隔: %.0f秒)", GOVERNANCE_LOOP_INTERVAL)

    def stop_governance_loop(self) -> None:
        """停止治理循环

        优雅地停止后台治理循环线程。
        """
        self._stop_event.set()
        if self._governance_thread is not None:
            self._governance_thread.join(timeout=10)
            self._governance_thread = None
        logger.info("治理循环已停止")

    def _governance_loop_worker(self) -> None:
        """治理循环工作线程

        定期调用 check_and_execute_proposals() 检查并执行到期提案。
        """
        logger.info("治理循环工作线程启动")
        while not self._stop_event.is_set():
            try:
                executed = self.check_and_execute_proposals()
                if executed:
                    logger.info("治理循环自动执行了 %d 个提案: %s",
                                len(executed), executed)
            except Exception as e:
                logger.error("治理循环执行出错: %s", e, exc_info=True)

            # 等待下一个检查周期，同时响应停止信号
            self._stop_event.wait(timeout=GOVERNANCE_LOOP_INTERVAL)

        logger.info("治理循环工作线程退出")


# ============================================================================
# 便捷函数
# ============================================================================

def create_governance_manager(blockchain_manager=None,
                               node_manager=None) -> GovernanceManager:
    """创建并配置治理管理器的便捷函数

    Args:
        blockchain_manager: 区块链管理器引用
        node_manager: 节点管理器引用

    Returns:
        配置好的GovernanceManager实例
    """
    gm = GovernanceManager(
        blockchain_manager=blockchain_manager,
        node_manager=node_manager,
    )
    return gm
