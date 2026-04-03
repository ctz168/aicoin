// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./AICoinToken.sol";

/**
 * @title Governance
 * @notice 治理投票合约 - AICoin DAO 的核心治理模块
 * @dev 本合约实现了去中心化治理投票机制，支持提案创建、投票、委托和执行。
 *      提案类型包括：运行模型选择（RUN_MODEL）、参数变更（PARAM_CHANGE）、紧急提案（EMERGENCY）。
 *      投票采用 1 token = 1 vote 机制，支持委托投票。
 *
 * 投票规则：
 * - 创建提案最低质押：1,000 AIC
 * - 投票周期：7 天
 * - 法定人数：总供应量的 10% 必须参与投票
 * - 通过阈值：投票中 51% 赞成即为通过
 * - 通过后可自动执行或由管理员手动执行
 */
contract Governance {

    // ============================================================
    // 常量
    // ============================================================

    /// @notice 投票周期：7 天（秒）
    uint256 public constant VOTING_PERIOD = 7 days;

    /// @notice 延迟执行时间：1 天（通过后等待执行的时间窗口）
    uint256 public constant EXECUTION_DELAY = 1 days;

    /// @notice 法定人数比例：10%（BPS 1000/10000）
    uint256 public constant QUORUM_BPS = 1000;

    /// @notice 通过阈值：51%（BPS 5100/10000）
    uint256 public constant APPROVAL_THRESHOLD_BPS = 5100;

    /// @notice 最小提案质押：1,000 AIC
    uint256 public constant MIN_PROPOSAL_STAKE = 1_000 * 10 ** 18;

    // ============================================================
    // 枚举
    // ============================================================

    /// @notice 提案类型枚举
    enum ProposalType {
        RUN_MODEL,       // 运行模型选择：决定网络运行哪个 AI 模型
        PARAM_CHANGE,    // 参数变更：修改网络参数（如奖励率、费率等）
        EMERGENCY        // 紧急提案：紧急安全修复或关键变更
    }

    /// @notice 提案状态枚举
    enum ProposalState {
        Pending,         // 待激活（等待延迟开始投票）
        Active,          // 投票中
        Succeeded,       // 投票通过（已达到法定人数和通过阈值）
        Defeated,        // 投票未通过
        Queued,          // 已排队等待执行
        Executed,        // 已执行
        Cancelled,       // 已取消
        Expired          // 已过期（超时未执行）
    }

    /// @notice 投票类型枚举
    enum VoteType {
        Against,         // 反对
        For,             // 赞成
        Abstain          // 弃权
    }

    // ============================================================
    // 数据结构
    // ============================================================

    /// @notice 提案数据结构
    struct Proposal {
        uint256 id;                     // 提案 ID
        ProposalType proposalType;      // 提案类型
        address proposer;               // 提案人地址
        string title;                   // 提案标题
        string description;             // 提案描述
        bytes callData;                 // 执行调用数据（目标合约 + 函数签名 + 参数）
        address[] targets;              // 目标合约地址列表
        uint256[] values;               // 发送的 ETH 金额列表
        uint256[] callDataLengths;      // 每个 callData 的长度
        bytes[] calldatas;              // 执行数据列表
        uint256 stake;                  // 提案质押金额
        uint256 forVotes;               // 赞成票数
        uint256 againstVotes;           // 反对票数
        uint256 abstainVotes;           // 弃权票数
        uint256 totalVotes;             // 总投票数（赞成 + 反对 + 弃权）
        uint256 startBlock;             // 投票开始区块号
        uint256 endBlock;               // 投票结束区块号
        uint256 executionBlock;         // 最早可执行区块号
        uint256 createdBlock;           // 创建区块号
        uint256 executedBlock;          // 实际执行区块号（0 = 未执行）
        bool cancelled;                 // 是否已取消
        bool executed;                  // 是否已执行
        mapping(address => bool) hasVoted;     // 记录各地址是否已投票
        mapping(address => VoteType) voteChoice; // 记录各地址的投票选择
    }

    /// @notice 投票记录
    struct VoteReceipt {
        bool hasVoted;          // 是否已投票
        VoteType voteType;      // 投票选择
        uint256 weight;         // 投票权重（代币数量）
        address delegate;       // 委托地址
    }

    // ============================================================
    // 状态变量
    // ============================================================

    /// @notice AICoin 代币合约
    AICoinToken public token;

    /// @notice 管理员地址
    address public admin;

    /// @notice 是否暂停
    bool public paused;

    /// @notice 提案创建延迟（防止抢跑）
    uint256 public proposalDelay = 1;

    /// @notice 提案计数器
    uint256 public proposalCount;

    /// @notice 提案执行器地址（可执行已通过的提案）
    address public executor;

    // ============================================================
    // 映射
    // ============================================================

    /// @notice 提案 ID => 提案数据
    mapping(uint256 => Proposal) public proposals;

    /// @notice 地址 => 投票记录
    mapping(address => VoteReceipt) public voteReceipts;

    /// @notice 地址 => 被委托人
    mapping(address => address) public delegates;

    /// @notice 被委托人 => 委托人列表
    mapping(address => address[]) public delegators;

    /// @notice 提案 ID => 投票地址列表（用于遍历）
    mapping(uint256 => address[]) public proposalVoters;

    // ============================================================
    // 事件
    // ============================================================

    /// @notice 提案创建事件
    event ProposalCreated(
        uint256 indexed proposalId,
        address indexed proposer,
        ProposalType proposalType,
        string title
    );

    /// @notice 投票事件
    event Voted(
        uint256 indexed proposalId,
        address indexed voter,
        VoteType voteType,
        uint256 weight
    );

    /// @notice 提案执行事件
    event ProposalExecuted(uint256 indexed proposalId, address indexed executor);

    /// @notice 提案取消事件
    event ProposalCancelled(uint256 indexed proposalId);

    /// @notice 委托投票事件
    event Delegated(address indexed delegator, address indexed delegatee);

    /// @notice 取消委托事件
    event Undelegated(address indexed delegator, address indexed delegatee);

    /// @notice 提案状态变更事件
    event ProposalStateChanged(uint256 indexed proposalId, ProposalState newState);

    // ============================================================
    // 错误
    // ============================================================

    error NotAdmin();
    error NotExecutor();
    error InsufficientBalance();
    error AlreadyVoted();
    error VotingPeriodEnded();
    error VotingPeriodNotEnded();
    error ProposalNotActive();
    error ProposalNotSucceeded();
    error ProposalAlreadyExecuted();
    error ProposalAlreadyCancelled();
    error ExecutionDelayNotElapsed();
    error CannotDelegateToSelf();
    error AlreadyDelegated();
    error NotDelegated();
    error InvalidProposalId();
    error ContractPaused();
    error QuorumNotReached();
    error ProposalExpired();
    error InvalidTarget();

    // ============================================================
    // 修饰符
    // ============================================================

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    modifier onlyExecutor() {
        if (msg.sender != executor && msg.sender != admin) revert NotExecutor();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    modifier validProposalId(uint256 _proposalId) {
        if (_proposalId == 0 || _proposalId > proposalCount) revert InvalidProposalId();
        _;
    }

    // ============================================================
    // 构造函数
    // ============================================================

    /**
     * @notice 构造函数 - 初始化治理合约
     * @param _token AICoin 代币合约地址
     * @param _admin 管理员地址
     * @param _executor 执行器地址
     */
    constructor(
        address _token,
        address _admin,
        address _executor
    ) {
        if (_token == address(0) || _admin == address(0)) revert InvalidTarget();

        token = AICoinToken(_token);
        admin = _admin;
        executor = _executor == address(0) ? _admin : _executor;
    }

    // ============================================================
    // 提案创建
    // ============================================================

    /**
     * @notice 创建新提案
     * @param _proposalType 提案类型
     * @param _title 提案标题
     * @param _description 提案描述
     * @param _targets 目标合约地址列表
     * @param _values 发送 ETH 金额列表
     * @param _calldatas 执行数据列表
     * @return proposalId 提案 ID
     * @dev 创建提案需质押至少 MIN_PROPOSAL_STAKE 数量的代币
     *      提案创建后需等待 proposalDelay 个区块后才开始投票
     */
    function createProposal(
        ProposalType _proposalType,
        string calldata _title,
        string calldata _description,
        address[] calldata _targets,
        uint256[] calldata _values,
        bytes[] calldata _calldatas
    ) external whenNotPaused returns (uint256 proposalId) {
        // 检查质押
        uint256 stakerBalance = token.balanceOf(msg.sender);
        if (stakerBalance < MIN_PROPOSAL_STAKE) revert InsufficientBalance();

        // 紧急提案可跳过延迟直接开始投票
        uint256 startDelay = _proposalType == ProposalType.EMERGENCY ? 0 : proposalDelay;

        proposalId = ++proposalCount;

        Proposal storage proposal = proposals[proposalId];
        proposal.id = proposalId;
        proposal.proposalType = _proposalType;
        proposal.proposer = msg.sender;
        proposal.title = _title;
        proposal.description = _description;
        proposal.stake = stakerBalance;
        proposal.createdBlock = block.number;
        proposal.startBlock = block.number + startDelay + 1;
        proposal.endBlock = block.startBlock + VOTING_PERIOD / 12; // 约 7 天的区块数（假设 12 秒/区块）
        proposal.executionBlock = proposal.endBlock + EXECUTION_DELAY / 12;

        // 存储执行数据
        proposal.targets = _targets;
        proposal.values = _values;
        proposal.calldatas = _calldatas;

        emit ProposalCreated(proposalId, msg.sender, _proposalType, _title);
    }

    // ============================================================
    // 投票
    // ============================================================

    /**
     * @notice 对提案进行投票
     * @param proposalId 提案 ID
     * @param voteType 投票类型（For / Against / Abstain）
     * @dev 1 token = 1 vote，支持委托投票权重
     *      弃权票计入法定人数但不影响通过阈值
     */
    function vote(uint256 proposalId, VoteType voteType)
        external
        whenNotPaused
        validProposalId(proposalId)
    {
        Proposal storage proposal = proposals[proposalId];

        // 验证投票窗口
        if (block.number < proposal.startBlock) revert VotingPeriodEnded();
        if (block.number >= proposal.endBlock) revert VotingPeriodEnded();
        if (proposal.cancelled || proposal.executed) revert ProposalNotActive();

        // 确定投票地址（自己或委托人）
        address voter = msg.sender;
        VoteReceipt storage receipt = voteReceipts[voter];

        if (receipt.hasVoted) revert AlreadyVoted();

        // 计算投票权重：自身代币 + 所有委托给自己的权重
        uint256 weight = _getVotes(voter);

        if (weight == 0) revert InsufficientBalance();

        // 记录投票
        receipt.hasVoted = true;
        receipt.voteType = voteType;
        receipt.weight = weight;

        proposal.hasVoted[voter] = true;
        proposal.voteChoice[voter] = voteType;

        // 更新票数统计
        if (voteType == VoteType.For) {
            proposal.forVotes += weight;
        } else if (voteType == VoteType.Against) {
            proposal.againstVotes += weight;
        } else {
            proposal.abstainVotes += weight;
        }
        proposal.totalVotes += weight;

        // 记录投票者
        proposalVoters[proposalId].push(voter);

        emit Voted(proposalId, voter, voteType, weight);
    }

    // ============================================================
    // 委托投票
    // ============================================================

    /**
     * @notice 将投票权委托给指定地址
     * @param delegatee 被委托人地址
     * @dev 委托后，被委托人投票时的权重 = 自身代币 + 所有委托给他的代币总和
     *      不能委托给自己
     */
    function delegate(address delegatee) external whenNotPaused {
        if (delegatee == msg.sender) revert CannotDelegateToSelf();
        if (delegates[msg.sender] != address(0)) revert AlreadyDelegated();
        if (delegatee == address(0)) revert InvalidTarget();

        // 检查被委托人是否有代币
        if (token.balanceOf(delegatee) == 0) revert InsufficientBalance();

        delegates[msg.sender] = delegatee;
        voteReceipts[msg.sender].delegate = delegatee;
        delegators[delegatee].push(msg.sender);

        emit Delegated(msg.sender, delegatee);
    }

    /**
     * @notice 取消投票委托
     */
    function undelegate() external whenNotPaused {
        address currentDelegate = delegates[msg.sender];
        if (currentDelegate == address(0)) revert NotDelegated();

        // 从被委托人的委托人列表中移除
        uint256 length = delegators[currentDelegate].length;
        for (uint256 i = 0; i < length; i++) {
            if (delegators[currentDelegate][i] == msg.sender) {
                delegators[currentDelegate][i] = delegators[currentDelegate][length - 1];
                delegators[currentDelegate].pop();
                break;
            }
        }

        delegates[msg.sender] = address(0);
        voteReceipts[msg.sender].delegate = address(0);

        emit Undelegated(msg.sender, currentDelegate);
    }

    // ============================================================
    // 提案执行
    // ============================================================

    /**
     * @notice 执行已通过的提案
     * @param proposalId 提案 ID
     * @dev 仅执行器可调用，提案必须已通过投票且满足执行延迟
     *      提案中指定的目标合约调用将被执行
     */
    function executeProposal(uint256 proposalId)
        external
        onlyExecutor
        whenNotPaused
        validProposalId(proposalId)
    {
        Proposal storage proposal = proposals[proposalId];

        if (proposal.cancelled || proposal.executed) revert ProposalAlreadyExecuted();
        if (proposal.endBlock >= block.number) revert VotingPeriodNotEnded();
        if (block.number < proposal.executionBlock) revert ExecutionDelayNotElapsed();

        ProposalState state = _getProposalState(proposalId);
        if (state != ProposalState.Succeeded) revert ProposalNotSucceeded();

        proposal.executed = true;
        proposal.executedBlock = block.number;

        // 执行提案中的调用
        uint256 numActions = proposal.targets.length;
        for (uint256 i = 0; i < numActions; i++) {
            (bool success, bytes memory result) = proposal.targets[i].call{
                value: proposal.values[i]
            }(proposal.calldatas[i]);

            // 紧急提案的执行失败会回滚整个交易
            if (!success) {
                if (proposal.proposalType == ProposalType.EMERGENCY) {
                    assembly {
                        revert(add(result, 32), mload(result))
                    }
                }
                // 非紧急提案允许部分执行失败（继续执行后续操作）
            }
        }

        emit ProposalExecuted(proposalId, msg.sender);
        emit ProposalStateChanged(proposalId, ProposalState.Executed);
    }

    // ============================================================
    // 查询函数
    // ============================================================

    /**
     * @notice 查询提案详细信息
     * @param proposalId 提案 ID
     * @return 提案的所有信息
     */
    function getProposal(uint256 proposalId)
        external
        view
        validProposalId(proposalId)
        returns (
            uint256 id,
            ProposalType proposalType,
            address proposer,
            string memory title,
            string memory description,
            uint256 forVotes,
            uint256 againstVotes,
            uint256 abstainVotes,
            uint256 totalVotes,
            uint256 startBlock,
            uint256 endBlock,
            uint256 executionBlock,
            ProposalState state
        )
    {
        Proposal storage p = proposals[proposalId];
        return (
            p.id,
            p.proposalType,
            p.proposer,
            p.title,
            p.description,
            p.forVotes,
            p.againstVotes,
            p.abstainVotes,
            p.totalVotes,
            p.startBlock,
            p.endBlock,
            p.executionBlock,
            _getProposalState(proposalId)
        );
    }

    /**
     * @notice 查询提案的票数统计
     * @param proposalId 提案 ID
     * @return forVotes 赞成票数
     * @return againstVotes 反对票数
     * @return abstainVotes 弃权票数
     * @return totalVotes 总投票数
     */
    function getVoteCount(uint256 proposalId)
        external
        view
        validProposalId(proposalId)
        returns (
            uint256 forVotes,
            uint256 againstVotes,
            uint256 abstainVotes,
            uint256 totalVotes
        )
    {
        Proposal storage p = proposals[proposalId];
        return (p.forVotes, p.againstVotes, p.abstainVotes, p.totalVotes);
    }

    /**
     * @notice 查询指定地址在某提案上的投票情况
     * @param proposalId 提案 ID
     * @param voter 投票人地址
     * @return hasVoted 是否已投票
     * @return voteType 投票类型
     */
    function getVote(uint256 proposalId, address voter)
        external
        view
        validProposalId(proposalId)
        returns (bool hasVoted, VoteType voteType)
    {
        Proposal storage p = proposals[proposalId];
        return (p.hasVoted[voter], p.voteChoice[voter]);
    }

    /**
     * @notice 查询指定地址的投票权重
     * @param voter 投票人地址
     * @return 投票权重（包含委托的权重）
     */
    function getVotes(address voter) external view returns (uint256) {
        return _getVotes(voter);
    }

    /**
     * @notice 查询提案状态
     * @param proposalId 提案 ID
     * @return 提案当前状态
     */
    function getProposalState(uint256 proposalId)
        external
        view
        validProposalId(proposalId)
        returns (ProposalState)
    {
        return _getProposalState(proposalId);
    }

    // ============================================================
    // 管理功能
    // ============================================================

    /**
     * @notice 取消提案（仅管理员或提案人）
     * @param proposalId 提案 ID
     * @dev 仅在投票未开始时可取消
     */
    function cancelProposal(uint256 proposalId)
        external
        validProposalId(proposalId)
    {
        Proposal storage proposal = proposals[proposalId];

        if (msg.sender != admin && msg.sender != proposal.proposer) revert NotAdmin();
        if (proposal.cancelled || proposal.executed) revert ProposalAlreadyCancelled();
        if (block.number >= proposal.startBlock) revert VotingPeriodEnded();

        proposal.cancelled = true;

        emit ProposalCancelled(proposalId);
        emit ProposalStateChanged(proposalId, ProposalState.Cancelled);
    }

    /**
     * @notice 暂停合约（仅管理员）
     */
    function pause() external onlyAdmin {
        paused = true;
    }

    /**
     * @notice 恢复合约（仅管理员）
     */
    function unpause() external onlyAdmin {
        paused = false;
    }

    /**
     * @notice 设置执行器地址（仅管理员）
     * @param _executor 新执行器地址
     */
    function setExecutor(address _executor) external onlyAdmin {
        if (_executor == address(0)) revert InvalidTarget();
        executor = _executor;
    }

    /**
     * @notice 设置提案延迟区块数（仅管理员）
     * @param _delay 延迟区块数
     */
    function setProposalDelay(uint256 _delay) external onlyAdmin {
        proposalDelay = _delay;
    }

    // ============================================================
    // 内部函数
    // ============================================================

    /**
     * @notice 获取提案当前状态
     * @param proposalId 提案 ID
     * @return 提案状态
     */
    function _getProposalState(uint256 proposalId) internal view returns (ProposalState) {
        Proposal storage proposal = proposals[proposalId];

        if (proposal.cancelled) return ProposalState.Cancelled;
        if (proposal.executed) return ProposalState.Executed;

        uint256 totalSupply = token.totalSupply();
        uint256 quorum = (totalSupply * QUORUM_BPS) / 10000;

        // 检查是否过期（超过执行区块号后 30 天未执行）
        if (block.number > proposal.executionBlock + (30 days / 12)) {
            return ProposalState.Expired;
        }

        // 投票尚未开始
        if (block.number < proposal.startBlock) {
            return ProposalState.Pending;
        }

        // 投票进行中
        if (block.number < proposal.endBlock) {
            return ProposalState.Active;
        }

        // 投票结束，检查是否达到法定人数
        if (proposal.totalVotes < quorum) {
            return ProposalState.Defeated;
        }

        // 检查是否通过（赞成票 > 总票数的 51%）
        if (proposal.forVotes * 10000 >= proposal.totalVotes * APPROVAL_THRESHOLD_BPS) {
            // 检查是否在执行延迟期内
            if (block.number < proposal.executionBlock) {
                return ProposalState.Queued;
            }
            return ProposalState.Succeeded;
        }

        return ProposalState.Defeated;
    }

    /**
     * @notice 计算投票权重（包含委托权重）
     * @param voter 投票人地址
     * @return 总投票权重
     */
    function _getVotes(address voter) internal view returns (uint256) {
        // 自身的代币余额
        uint256 weight = token.getVoteWeight(voter);

        // 加上所有委托给该地址的权重
        address[] storage _delegators = delegators[voter];
        for (uint256 i = 0; i < _delegators.length; i++) {
            weight += token.getVoteWeight(_delegators[i]);
        }

        return weight;
    }
}
