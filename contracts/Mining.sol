// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./AICoinToken.sol";

/**
 * @title Mining
 * @notice 算力挖矿合约 - 管理节点算力贡献、奖励分配和惩罚机制
 * @dev 本合约实现了类似比特币的减半机制，同时引入了基于算力贡献的比例奖励分配。
 *      矿工节点通过提交计算证明（compute proof）来获取挖矿奖励。
 *      支持难度自动调整、质押惩罚（slashing）和奖励池累积分配。
 *
 * 核心机制：
 * - 初始区块奖励：50 AIC，每 210,000 个区块（或约 1 年时间）减半
 * - 奖励按算力贡献比例分配
 * - 难度根据全网总算力自动调整
 * - 虚假计算证明将被质押惩罚
 */
contract Mining {

    // ============================================================
    // 常量
    // ============================================================

    /// @notice 初始区块奖励：50 AIC
    uint256 public constant INITIAL_BLOCK_REWARD = 50 * 10 ** 18;

    /// @notice 减半周期：210,000 个区块
    uint256 public constant HALVING_BLOCKS = 210_000;

    /// @notice 最小奖励：0.000001 AIC（避免除零）
    uint256 public constant MIN_REWARD = 10 ** 12;

    /// @notice 基础质押金额：100 AIC（成为矿工节点所需的最低质押）
    uint256 public constant MIN_STAKE = 100 * 10 ** 18;

    /// @notice 惩罚比例（质押的 10%）
    uint256 public constant SLASH_RATE_BPS = 1000; // 1000 / 10000 = 10%

    /// @notice 难度调整系数（BPS，万分之一）
    uint256 public constant DIFFICULTY_ADJUSTMENT_FACTOR = 1500; // 15%

    /// @notice 初始难度
    uint256 public constant INITIAL_DIFFICULTY = 1 * 10 ** 18;

    /// @notice 目标总算力（用于难度调整的基准值）
    uint256 public constant TARGET_TOTAL_POWER = 1_000_000 * 10 ** 18;

    // ============================================================
    // 数据结构
    // ============================================================

    /// @notice 节点信息
    struct NodeInfo {
        bool isActive;              // 节点是否处于活跃状态
        bool isAuthorized;          // 节点是否已被授权
        uint256 stake;              // 质押金额
        uint256 hashPower;          // 当前有效算力
        uint256 totalHashPower;     // 历史累计算力
        uint256 tasksCompleted;     // 已完成任务数
        uint256 lastProofBlock;     // 上次提交证明的区块号
        uint256 lastProofTime;      // 上次提交证明的时间戳
        uint256 uptimeStart;        // 在线起始时间
        uint256 totalUptime;        // 累计在线时长（秒）
        uint256 pendingRewards;     // 待领取奖励
        uint256 slashedAmount;      // 已被惩罚金额
        uint256 validProofs;        // 有效证明数
        uint256 invalidProofs;      // 无效证明数
    }

    /// @notice 计算证明
    struct ComputeProof {
        address node;               // 提交证明的节点地址
        uint256 blockNumber;        // 提交证明的区块号
        uint256 timestamp;          // 提交时间戳
        uint256 hashPower;          // 声称的算力
        bytes32 taskHash;           // 任务哈希
        bytes32 resultHash;         // 结果哈希
        bool verified;              // 是否已验证
        bool valid;                 // 验证结果是否有效
    }

    // ============================================================
    // 状态变量
    // ============================================================

    /// @notice AICoin 代币合约地址
    AICoinToken public token;

    /// @notice 合约管理员
    address public admin;

    /// @notice 当前挖矿难度
    uint256 public currentDifficulty;

    /// @notice 当前区块奖励（随减半调整）
    uint256 public currentBlockReward;

    /// @notice 挖矿起始区块号
    uint256 public startBlock;

    /// @notice 全网总算力
    uint256 public totalNetworkPower;

    /// @notice 活跃节点总数
    uint256 public activeNodeCount;

    /// @notice 累计奖励池余额
    uint256 public rewardPoolBalance;

    /// @notice 上次难度调整的区块号
    uint256 public lastDifficultyAdjustBlock;

    /// @notice 当前周期（用于减半计算）
    uint256 public currentHalvingEpoch;

    /// @notice 记录是否已暂停
    bool public paused;

    /// @notice 记录是否已暂停提交证明
    bool public proofSubmissionPaused;

    // ============================================================
    // 映射
    // ============================================================

    /// @notice 节点信息映射
    mapping(address => NodeInfo) public nodes;

    /// @notice 所有节点地址列表
    address[] public nodeAddresses;

    /// @notice 计算证明映射（证明 ID => 证明数据）
    mapping(uint256 => ComputeProof) public proofs;

    /// @notice 下一个证明 ID
    uint256 public nextProofId;

    /// @notice 待验证的证明列表
    uint256[] public pendingProofIds;

    // ============================================================
    // 事件
    // ============================================================

    /// @notice 节点注册事件
    event NodeRegistered(address indexed node, uint256 stake);

    /// @notice 节点取消注册事件
    event NodeDeregistered(address indexed node);

    /// @notice 节点授权事件
    event NodeAuthorized(address indexed node);

    /// @notice 节点撤销授权事件
    event NodeRevoked(address indexed node);

    /// @notice 节点质押增加事件
    event StakeIncreased(address indexed node, uint256 amount);

    /// @notice 节点质押提取事件
    event StakeWithdrawn(address indexed node, uint256 amount);

    /// @notice 计算证明已提交事件
    event ProofSubmitted(
        address indexed node,
        uint256 indexed proofId,
        bytes32 taskHash,
        uint256 hashPower
    );

    /// @notice 计算证明已验证事件
    event ProofVerified(uint256 indexed proofId, bool valid);

    /// @notice 奖励已领取事件
    event RewardClaimed(address indexed node, uint256 amount);

    /// @notice 节点被惩罚事件
    event NodeSlashed(address indexed node, uint256 amount, string reason);

    /// @notice 难度已调整事件
    event DifficultyAdjusted(uint256 oldDifficulty, uint256 newDifficulty, uint256 blockNumber);

    /// @notice 奖励减半事件
    event RewardHalved(uint256 epoch, uint256 newReward);

    /// @notice 奖励已分配到奖励池事件
    event RewardsDistributed(uint256 totalAmount, uint256 nodeCount);

    /// @notice 暂停事件
    event MiningPaused(address account);

    /// @notice 恢复事件
    event MiningUnpaused(address account);

    // ============================================================
    // 错误
    // ============================================================

    error NodeNotRegistered();
    error NodeNotAuthorized();
    error NodeAlreadyRegistered();
    error NodeNotActive();
    error InsufficientStake();
    error InvalidProofData();
    error ProofAlreadyVerified();
    error NoPendingRewards();
    error SlashingExceedsStake();
    error NotAdmin();
    error ContractPaused();
    error AlreadyPaused();
    error NotPaused();
    error ZeroHashPower();
    error InvalidDifficulty();
    error CooldownNotElapsed();

    // ============================================================
    // 修饰符
    // ============================================================

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    modifier whenNotProofPaused() {
        if (paused || proofSubmissionPaused) revert ContractPaused();
        _;
    }

    modifier onlyRegisteredNode() {
        if (!nodes[msg.sender].isAuthorized) revert NodeNotRegistered();
        _;
    }

    modifier onlyActiveNode() {
        if (!nodes[msg.sender].isActive) revert NodeNotActive();
        _;
    }

    // ============================================================
    // 构造函数
    // ============================================================

    /**
     * @notice 构造函数 - 初始化挖矿合约
     * @param _token AICoin 代币合约地址
     * @param _admin 管理员地址
     * @param _startBlock 挖矿起始区块号
     */
    constructor(
        address _token,
        address _admin,
        uint256 _startBlock
    ) {
        if (_token == address(0) || _admin == address(0)) revert ZeroHashPower();

        token = AICoinToken(_token);
        admin = _admin;
        startBlock = _startBlock;
        lastDifficultyAdjustBlock = _startBlock;
        currentDifficulty = INITIAL_DIFFICULTY;
        currentBlockReward = INITIAL_BLOCK_REWARD;
        currentHalvingEpoch = 0;
    }

    // ============================================================
    // 节点管理
    // ============================================================

    /**
     * @notice 注册为矿工节点
     * @dev 调用前需先向代币合约 approve 本合约地址的质押金额
     *      节点注册后处于非活跃状态，需管理员授权后方可提交证明
     */
    function registerNode() external whenNotPaused {
        if (nodes[msg.sender].isAuthorized) revert NodeAlreadyRegistered();

        // 从调用者转入质押代币
        uint256 stakeAmount = MIN_STAKE;
        if (token.balanceOf(msg.sender) < stakeAmount) revert InsufficientStake();

        // 转入质押
        bool success = token.transferFrom(msg.sender, address(this), stakeAmount);
        require(success, "Stake transfer failed");

        // 初始化节点信息
        nodes[msg.sender] = NodeInfo({
            isActive: false,
            isAuthorized: false,
            stake: stakeAmount,
            hashPower: 0,
            totalHashPower: 0,
            tasksCompleted: 0,
            lastProofBlock: 0,
            lastProofTime: 0,
            uptimeStart: 0,
            totalUptime: 0,
            pendingRewards: 0,
            slashedAmount: 0,
            validProofs: 0,
            invalidProofs: 0
        });

        nodeAddresses.push(msg.sender);
        emit NodeRegistered(msg.sender, stakeAmount);
    }

    /**
     * @notice 授权节点（仅管理员）
     * @param node 待授权的节点地址
     */
    function authorizeNode(address node) external onlyAdmin {
        if (!nodes[node].isAuthorized) revert NodeNotRegistered();

        nodes[node].isAuthorized = true;
        nodes[node].isActive = true;
        nodes[node].uptimeStart = block.timestamp;
        activeNodeCount++;

        emit NodeAuthorized(node);
    }

    /**
     * @notice 撤销节点授权（仅管理员）
     * @param node 待撤销的节点地址
     * @dev 撤销后节点将无法提交计算证明，但可提取已质押的代币
     */
    function revokeNode(address node) external onlyAdmin {
        if (!nodes[node].isAuthorized) revert NodeNotRegistered();

        if (nodes[node].isActive) {
            nodes[node].isActive = false;
            activeNodeCount--;
            _updateUptime(node);
        }

        nodes[node].isAuthorized = false;
        // 从全网算力中减去该节点的算力
        if (totalNetworkPower >= nodes[node].hashPower) {
            totalNetworkPower -= nodes[node].hashPower;
        } else {
            totalNetworkPower = 0;
        }
        nodes[node].hashPower = 0;

        emit NodeRevoked(node);
    }

    /**
     * @notice 增加质押
     * @param amount 增加的质押金额
     */
    function increaseStake(uint256 amount) external onlyRegisteredNode whenNotPaused {
        if (amount == 0) revert InsufficientStake();
        if (token.balanceOf(msg.sender) < amount) revert InsufficientStake();

        bool success = token.transferFrom(msg.sender, address(this), amount);
        require(success, "Stake transfer failed");

        nodes[msg.sender].stake += amount;
        emit StakeIncreased(msg.sender, amount);
    }

    /**
     * @notice 提取质押
     * @param amount 提取的质押金额
     * @dev 提取后质押不能低于最小质押金额
     */
    function withdrawStake(uint256 amount) external onlyRegisteredNode whenNotPaused {
        if (nodes[msg.sender].stake < amount) revert InsufficientStake();
        if (nodes[msg.sender].stake - amount < MIN_STAKE) revert InsufficientStake();

        nodes[msg.sender].stake -= amount;
        bool success = token.transfer(msg.sender, amount);
        require(success, "Stake transfer failed");

        emit StakeWithdrawn(msg.sender, amount);
    }

    // ============================================================
    // 计算证明提交
    // ============================================================

    /**
     * @notice 提交计算证明
     * @param _hashPower 本次计算的算力贡献量
     * @param _taskHash 任务数据哈希
     * @param _resultHash 计算结果哈希
     * @return proofId 证明 ID
     * @dev 仅已授权且活跃的节点可调用，需保证算力非零
     */
    function submitProof(
        uint256 _hashPower,
        bytes32 _taskHash,
        bytes32 _resultHash
    ) external onlyActiveNode whenNotProofPaused returns (uint256 proofId) {
        if (_hashPower == 0) revert ZeroHashPower();
        if (_taskHash == bytes32(0) || _resultHash == bytes32(0)) revert InvalidProofData();

        NodeInfo storage node = nodes[msg.sender];

        // 更新在线时间
        _updateUptime(msg.sender);

        // 根据难度调整有效算力
        uint256 effectivePower = _hashPower * 10 ** 18 / currentDifficulty;

        // 更新节点算力信息
        node.hashPower = effectivePower;
        node.totalHashPower += effectivePower;
        node.tasksCompleted++;
        node.lastProofBlock = block.number;
        node.lastProofTime = block.timestamp;

        // 更新全网总算力
        totalNetworkPower += effectivePower;

        // 创建计算证明
        proofId = nextProofId++;
        proofs[proofId] = ComputeProof({
            node: msg.sender,
            blockNumber: block.number,
            timestamp: block.timestamp,
            hashPower: effectivePower,
            taskHash: _taskHash,
            resultHash: _resultHash,
            verified: false,
            valid: false
        });

        // 添加到待验证队列
        pendingProofIds.push(proofId);

        // 检查是否需要减半或难度调整
        _checkHalving();
        _checkDifficultyAdjustment();

        emit ProofSubmitted(msg.sender, proofId, _taskHash, effectivePower);
    }

    /**
     * @notice 验证计算证明（仅管理员或预言机）
     * @param proofId 要验证的证明 ID
     * @param valid 验证结果
     * @dev 验证后根据结果更新奖励或执行惩罚
     */
    function verifyProof(uint256 proofId, bool valid) external onlyAdmin {
        ComputeProof storage proof = proofs[proofId];
        if (proof.verified) revert ProofAlreadyVerified();

        proof.verified = true;
        proof.valid = valid;

        NodeInfo storage node = nodes[proof.node];

        if (valid) {
            node.validProofs++;
            // 计算本次奖励并累积
            uint256 reward = _calculateReward(proof.node, proof.hashPower);
            node.pendingRewards += reward;
        } else {
            node.invalidProofs++;
            // 虚假证明惩罚
            _slashNode(proof.node, "Invalid compute proof");
        }

        // 从待验证队列中移除（通过标记）
        emit ProofVerified(proofId, valid);
    }

    // ============================================================
    // 奖励系统
    // ============================================================

    /**
     * @notice 计算单个节点的奖励
     * @param nodeAddress 节点地址
     * @param contributedPower 本次贡献的算力
     * @return 应得奖励金额
     * @dev 奖励 = 当前区块奖励 × （节点算力 / 全网总算力）
     */
    function _calculateReward(address nodeAddress, uint256 contributedPower) internal view returns (uint256) {
        if (totalNetworkPower == 0) return 0;

        // 按算力比例计算奖励
        uint256 reward = (currentBlockReward * contributedPower) / totalNetworkPower;

        // 确保奖励不低于最小值
        if (reward < MIN_REWARD && contributedPower > 0) {
            reward = MIN_REWARD;
        }

        return reward;
    }

    /**
     * @notice 领取待领取奖励
     * @dev 节点调用此函数将累积奖励铸造到自己的账户
     */
    function claimReward() external onlyRegisteredNode whenNotPaused {
        uint256 reward = nodes[msg.sender].pendingRewards;
        if (reward == 0) revert NoPendingRewards();

        nodes[msg.sender].pendingRewards = 0;

        // 通过代币合约铸造奖励
        token.mint(msg.sender, reward);

        emit RewardClaimed(msg.sender, reward);
    }

    /**
     * @notice 查询待领取奖励
     * @param nodeAddress 节点地址
     * @return 待领取的奖励金额
     */
    function getPendingReward(address nodeAddress) external view returns (uint256) {
        return nodes[nodeAddress].pendingRewards;
    }

    /**
     * @notice 查询全网总算力
     * @return 当前全网总算力
     */
    function getTotalNetworkPower() external view returns (uint256) {
        return totalNetworkPower;
    }

    // ============================================================
    // 惩罚机制（Slashing）
    // ============================================================

    /**
     * @notice 惩罚节点
     * @param nodeAddress 节点地址
     * @param reason 惩罚原因
     * @dev 惩罚金额 = 节点质押 × SLASH_RATE_BPS / 10000
     */
    function _slashNode(address nodeAddress, string memory reason) internal {
        NodeInfo storage node = nodes[nodeAddress];
        uint256 slashAmount = (node.stake * SLASH_RATE_BPS) / 10000;

        if (slashAmount == 0) return;

        // 扣减质押
        node.stake -= slashAmount;
        node.slashedAmount += slashAmount;

        // 扣减全网算力
        if (totalNetworkPower >= node.hashPower) {
            totalNetworkPower -= node.hashPower;
        }
        node.hashPower = 0;

        // 销毁惩罚代币（永久从流通中移除）
        token.burn(slashAmount);

        // 清零待领取奖励
        node.pendingRewards = 0;

        emit NodeSlashed(nodeAddress, slashAmount, reason);
    }

    /**
     * @notice 管理员手动惩罚节点（用于紧急情况）
     * @param nodeAddress 节点地址
     * @param reason 惩罚原因
     */
    function slashNode(address nodeAddress, string calldata reason) external onlyAdmin {
        if (!nodes[nodeAddress].isAuthorized) revert NodeNotRegistered();
        _slashNode(nodeAddress, reason);
    }

    // ============================================================
    // 减半机制
    // ============================================================

    /**
     * @notice 检查并执行减半
     * @dev 当经过的区块数达到 HALVING_BLOCKS 时，区块奖励减半
     */
    function _checkHalving() internal {
        uint256 blocksElapsed = block.number - startBlock;
        uint256 newEpoch = blocksElapsed / HALVING_BLOCKS;

        if (newEpoch > currentHalvingEpoch) {
            currentHalvingEpoch = newEpoch;
            currentBlockReward = INITIAL_BLOCK_REWARD / (2 ** currentHalvingEpoch);

            if (currentBlockReward < MIN_REWARD) {
                currentBlockReward = MIN_REWARD;
            }

            emit RewardHalved(currentHalvingEpoch, currentBlockReward);
        }
    }

    // ============================================================
    // 难度调整
    // ============================================================

    /**
     * @notice 检查并调整难度
     * @dev 每经过 1000 个区块调整一次难度
     *      如果全网算力 > 目标算力，则增加难度（降低有效算力）
     *      如果全网算力 < 目标算力，则降低难度（提高有效算力）
     */
    function _checkDifficultyAdjustment() internal {
        uint256 blocksSinceAdjust = block.number - lastDifficultyAdjustBlock;

        if (blocksSinceAdjust >= 1000) {
            uint256 oldDifficulty = currentDifficulty;

            if (totalNetworkPower > TARGET_TOTAL_POWER) {
                // 算力过高，增加难度
                uint256 ratio = (totalNetworkPower * 10 ** 18) / TARGET_TOTAL_POWER;
                uint256 adjustment = (currentDifficulty * DIFFICULTY_ADJUSTMENT_FACTOR * (ratio - 10 ** 18)) / (10 ** 18 * 10000);
                currentDifficulty += adjustment;
            } else if (totalNetworkPower < TARGET_TOTAL_POWER && totalNetworkPower > 0) {
                // 算力过低，降低难度
                uint256 ratio = (TARGET_TOTAL_POWER * 10 ** 18) / totalNetworkPower;
                uint256 adjustment = (currentDifficulty * DIFFICULTY_ADJUSTMENT_FACTOR * (ratio - 10 ** 18)) / (10 ** 18 * 10000);
                if (adjustment < currentDifficulty) {
                    currentDifficulty -= adjustment;
                } else {
                    currentDifficulty = INITIAL_DIFFICULTY;
                }
            }

            // 确保难度在合理范围内
            if (currentDifficulty < INITIAL_DIFFICULTY / 100) {
                currentDifficulty = INITIAL_DIFFICULTY / 100;
            }

            lastDifficultyAdjustBlock = block.number;
            emit DifficultyAdjusted(oldDifficulty, currentDifficulty, block.number);
        }
    }

    /**
     * @notice 手动调整难度（仅管理员）
     * @param _newDifficulty 新的难度值
     */
    function setDifficulty(uint256 _newDifficulty) external onlyAdmin {
        if (_newDifficulty == 0) revert InvalidDifficulty();
        uint256 oldDifficulty = currentDifficulty;
        currentDifficulty = _newDifficulty;
        emit DifficultyAdjusted(oldDifficulty, currentDifficulty, block.number);
    }

    // ============================================================
    // 节点查询
    // ============================================================

    /**
     * @notice 查询节点信息
     * @param nodeAddress 节点地址
     * @return 节点的完整信息
     */
    function getNodeInfo(address nodeAddress) external view returns (NodeInfo memory) {
        return nodes[nodeAddress];
    }

    /**
     * @notice 查询节点数量
     * @return 注册节点总数
     */
    function getNodeCount() external view returns (uint256) {
        return nodeAddresses.length;
    }

    /**
     * @notice 查询待验证证明数量
     * @return 待验证证明的数量
     */
    function getPendingProofCount() external view returns (uint256) {
        return pendingProofIds.length;
    }

    // ============================================================
    // 紧急控制
    // ============================================================

    /**
     * @notice 暂停合约（仅管理员）
     */
    function pause() external onlyAdmin {
        paused = true;
        emit MiningPaused(msg.sender);
    }

    /**
     * @notice 恢复合约（仅管理员）
     */
    function unpause() external onlyAdmin {
        paused = false;
        emit MiningUnpaused(msg.sender);
    }

    /**
     * @notice 暂停证明提交（仅管理员）
     * @dev 比完全暂停更轻量，仅阻止新的计算证明提交
     */
    function pauseProofSubmission() external onlyAdmin {
        proofSubmissionPaused = true;
    }

    /**
     * @notice 恢复证明提交（仅管理员）
     */
    function unpauseProofSubmission() external onlyAdmin {
        proofSubmissionPaused = false;
    }

    // ============================================================
    // 内部辅助
    // ============================================================

    /**
     * @notice 更新节点在线时间
     * @param nodeAddress 节点地址
     */
    function _updateUptime(address nodeAddress) internal {
        NodeInfo storage node = nodes[nodeAddress];
        if (node.uptimeStart > 0 && block.timestamp > node.uptimeStart) {
            node.totalUptime += (block.timestamp - node.uptimeStart);
        }
        node.uptimeStart = block.timestamp;
    }
}
