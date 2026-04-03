// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./AICoinToken.sol";

/**
 * @title APIAccess
 * @notice API 调用燃烧合约 - 管理通过燃烧 AIC 代币获取 AI 推理 API 访问权限
 * @dev 本合约实现了 "Burn-to-Earn" 机制：用户通过燃烧 AIC 代币来获取 AI 推理服务。
 *      支持多层级定价（Basic / Premium / Priority），优先级队列排序，
 *      费用分配（80% 矿工节点，20% 国库），以及每日燃烧限额。
 *
 * 费用结构：
 * - Basic 层：0.01 AIC / 1K tokens
 * - Premium 层：0.05 AIC / 1K tokens
 * - Priority 层：0.10 AIC / 1K tokens
 *
 * 费用分配：
 * - 80% 分配给挖矿节点（激励算力提供者）
 * - 20% 分配到国库（用于协议开发和运营）
 */
contract APIAccess {

    // ============================================================
    // 常量
    // ============================================================

    /// @notice 矿工节点分配比例：80%（BPS 8000/10000）
    uint256 public constant MINER_SHARE_BPS = 8000;

    /// @notice 国库分配比例：20%（BPS 2000/10000）
    uint256 public constant TREASURY_SHARE_BPS = 2000;

    /// @notice 默认每日燃烧限额：10,000 AIC
    uint256 public constant DEFAULT_DAILY_BURN_LIMIT = 10_000 * 10 ** 18;

    /// @notice 最小 token 数量：1K tokens = 1000
    uint256 public constant MIN_TOKENS = 1_000;

    /// @notice 最大 tier 数量
    uint256 public constant MAX_TIERS = 10;

    // ============================================================
    // 枚举
    // ============================================================

    /// @notice API 访问层级
    enum Tier {
        Basic,      // 基础层：最低优先级，最低费率
        Premium,    // 高级层：中等优先级，中等费率
        Priority    // 优先层：最高优先级，最高费率
    }

    // ============================================================
    // 数据结构
    // ============================================================

    /// @notice 燃烧请求记录
    struct BurnRequest {
        address user;            // 请求用户地址
        uint256 amount;          // 燃烧金额（AIC）
        uint256 tokensRequested; // 请求的 AI 推理 token 数量
        Tier tier;               // 访问层级
        uint256 timestamp;       // 请求时间戳
        bool processed;          // 是否已处理
        uint256 priorityScore;   // 优先级分数（用于排序）
    }

    /// @notice 层级定价信息
    struct TierPricing {
        uint256 ratePer1K;       // 每 1K tokens 的费率（AIC，含 18 位精度）
        uint256 priorityWeight;  // 优先级权重（用于优先级队列排序）
        bool active;             // 是否启用
    }

    /// @notice 地址每日燃烧统计
    struct DailyBurnInfo {
        uint256 totalBurned;         // 当日累计燃烧金额
        uint256 lastResetTimestamp;  // 上次重置时间戳
    }

    // ============================================================
    // 状态变量
    // ============================================================

    /// @notice AICoin 代币合约
    AICoinToken public token;

    /// @notice 管理员地址
    address public admin;

    /// @notice 国库地址（接收 20% 费用）
    address public treasury;

    /// @notice 挖矿合约地址（接收 80% 费用）
    address public miningPool;

    /// @notice 是否暂停
    bool public paused;

    /// @notice 请求计数器
    uint256 public requestCount;

    /// @notice 国库累计余额（从燃烧中分配的）
    uint256 public treasuryBalance;

    /// @notice 矿工累计分配余额
    uint256 public minerPoolBalance;

    /// @notice 全网累计燃烧总量
    uint256 public totalBurned;

    /// @notice 每地址每日燃烧限额
    uint256 public dailyBurnLimit;

    /// @notice 全局每日燃烧限额（0 = 无限制）
    uint256 public globalDailyBurnLimit;

    /// @notice 全局当日累计燃烧量
    uint256 public globalDailyBurned;

    /// @notice 全局燃烧计数重置时间戳
    uint256 public globalLastResetTimestamp;

    // ============================================================
    // 映射
    // ============================================================

    /// @notice 层级 => 定价信息
    mapping(Tier => TierPricing) public tierPricing;

    /// @notice 请求 ID => 请求记录
    mapping(uint256 => BurnRequest) public burnRequests;

    /// @notice 地址 => 每日燃烧信息
    mapping(address => DailyBurnInfo) public dailyBurnInfo;

    /// @notice 待处理的请求 ID 列表（优先级队列）
    uint256[] public pendingRequestIds;

    // ============================================================
    // 事件
    // ============================================================

    /// @notice API 访问请求事件
    event AccessRequested(
        address indexed user,
        uint256 indexed requestId,
        uint256 amountBurned,
        uint256 tokensRequested,
        Tier tier
    );

    /// @notice 费用分配事件
    event FeesDistributed(
        uint256 indexed requestId,
        uint256 minerShare,
        uint256 treasuryShare
    );

    /// @notice 费率变更事件
    event BurnRateChanged(Tier tier, uint256 oldRate, uint256 newRate);

    /// @notice 请求已处理事件
    event RequestProcessed(uint256 indexed requestId);

    /// @notice 国库提取事件
    event TreasuryWithdrawn(address indexed to, uint256 amount);

    /// @notice 矿工池提取事件
    event MinerPoolWithdrawn(address indexed to, uint256 amount);

    /// @notice 暂停事件
    event APIAccessPaused(address account);

    /// @notice 恢复事件
    event APIAccessUnpaused(address account);

    /// @notice 每日限额变更事件
    event DailyBurnLimitChanged(uint256 oldLimit, uint256 newLimit);

    // ============================================================
    // 错误
    // ============================================================

    error NotAdmin();
    error InvalidTier();
    error InsufficientTokens();
    error DailyBurnLimitExceeded();
    error GlobalDailyBurnLimitExceeded();
    error InsufficientBalance();
    error NoPendingRequests();
    error InvalidAmount();
    error ContractPaused();
    error TierNotActive();
    error ZeroAddress();

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

    modifier validTier(Tier _tier) {
        if (_tier > Tier.Priority) revert InvalidTier();
        _;
    }

    // ============================================================
    // 构造函数
    // ============================================================

    /**
     * @notice 构造函数 - 初始化 API 访问合约
     * @param _token AICoin 代币合约地址
     * @param _admin 管理员地址
     * @param _treasury 国库地址
     * @param _miningPool 挖矿合约地址（接收矿工分配费用）
     */
    constructor(
        address _token,
        address _admin,
        address _treasury,
        address _miningPool
    ) {
        if (_token == address(0) || _admin == address(0)) revert ZeroAddress();

        token = AICoinToken(_token);
        admin = _admin;
        treasury = _treasury == address(0) ? _admin : _treasury;
        miningPool = _miningPool == address(0) ? _admin : _miningPool;
        dailyBurnLimit = DEFAULT_DAILY_BURN_LIMIT;
        globalDailyBurnLimit = 0; // 默认无限制
        globalLastResetTimestamp = block.timestamp;

        // 初始化层级定价
        tierPricing[Tier.Basic] = TierPricing({
            ratePer1K: 0.01 * 10 ** 18,       // 0.01 AIC / 1K tokens
            priorityWeight: 1,                  // 最低优先级
            active: true
        });
        tierPricing[Tier.Premium] = TierPricing({
            ratePer1K: 0.05 * 10 ** 18,       // 0.05 AIC / 1K tokens
            priorityWeight: 5,                  // 中等优先级
            active: true
        });
        tierPricing[Tier.Priority] = TierPricing({
            ratePer1K: 0.10 * 10 ** 18,       // 0.10 AIC / 1K tokens
            priorityWeight: 10,                 // 最高优先级
            active: true
        });
    }

    // ============================================================
    // 核心：燃烧获取 API 访问
    // ============================================================

    /**
     * @notice 燃烧 AIC 代币以获取 AI 推理 API 访问权限
     * @param _tokensRequested 请求的 AI 推理 token 数量
     * @param _tier 访问层级
     * @return requestId 请求 ID
     * @dev 调用前需先 approve 本合约地址的代币额度
     *      费用 = (_tokensRequested / 1000) × 层级费率
     *      费用将被燃烧，其中 80% 价值分配给矿工，20% 分配给国库
     */
    function burnForAccess(
        uint256 _tokensRequested,
        Tier _tier
    ) external whenNotPaused validTier(_tier) returns (uint256 requestId) {
        TierPricing storage pricing = tierPricing[_tier];
        if (!pricing.active) revert TierNotActive();
        if (_tokensRequested < MIN_TOKENS) revert InsufficientTokens();

        // 计算费用
        uint256 tokenUnits = _tokensRequested / 1000;
        if (tokenUnits == 0) revert InsufficientTokens();

        uint256 burnAmount = tokenUnits * pricing.ratePer1K;
        if (burnAmount == 0) revert InvalidAmount();

        // 检查每日燃烧限额
        _checkAndUpdateDailyLimit(msg.sender);
        _checkAndUpdateGlobalDailyLimit();

        // 从调用者转入代币
        if (token.balanceOf(msg.sender) < burnAmount) revert InsufficientBalance();
        bool success = token.transferFrom(msg.sender, address(this), burnAmount);
        require(success, "Transfer failed");

        // 燃烧代币
        token.burn(burnAmount);

        // 计算费用分配
        uint256 minerShare = (burnAmount * MINER_SHARE_BPS) / 10000;
        uint256 treasuryShare = (burnAmount * TREASURY_SHARE_BPS) / 10000;

        // 更新统计
        treasuryBalance += treasuryShare;
        minerPoolBalance += minerShare;
        totalBurned += burnAmount;
        dailyBurnInfo[msg.sender].totalBurned += burnAmount;
        globalDailyBurned += burnAmount;

        // 创建请求记录
        requestId = ++requestCount;
        burnRequests[requestId] = BurnRequest({
            user: msg.sender,
            amount: burnAmount,
            tokensRequested: _tokensRequested,
            tier: _tier,
            timestamp: block.timestamp,
            processed: false,
            priorityScore: _calculatePriorityScore(_tier, burnAmount)
        });

        // 添加到优先级队列
        pendingRequestIds.push(requestId);

        emit AccessRequested(msg.sender, requestId, burnAmount, _tokensRequested, _tier);
        emit FeesDistributed(requestId, minerShare, treasuryShare);
    }

    // ============================================================
    // 费率管理
    // ============================================================

    /**
     * @notice 设置层级燃烧费率（仅管理员）
     * @param _tier 层级
     * @param _rate 新的每 1K tokens 费率（AIC，含 18 位精度）
     */
    function setBurnRate(Tier _tier, uint256 _rate) external onlyAdmin validTier(_tier) {
        TierPricing storage pricing = tierPricing[_tier];
        uint256 oldRate = pricing.ratePer1K;
        pricing.ratePer1K = _rate;

        emit BurnRateChanged(_tier, oldRate, _rate);
    }

    /**
     * @notice 查询层级燃烧费率
     * @param _tier 层级
     * @return 每 1K tokens 的费率
     * @return 优先级权重
     * @return 是否启用
     */
    function getBurnRate(Tier _tier)
        external
        view
        validTier(_tier)
        returns (uint256 rate, uint256 priorityWeight, bool active)
    {
        TierPricing storage pricing = tierPricing[_tier];
        return (pricing.ratePer1K, pricing.priorityWeight, pricing.active);
    }

    /**
     * @notice 设置层级激活状态（仅管理员）
     * @param _tier 层级
     * @param _active 是否激活
     */
    function setTierActive(Tier _tier, bool _active) external onlyAdmin validTier(_tier) {
        tierPricing[_tier].active = _active;
    }

    /**
     * @notice 设置层级优先级权重（仅管理员）
     * @param _tier 层级
     * @param _weight 优先级权重
     */
    function setTierPriorityWeight(Tier _tier, uint256 _weight) external onlyAdmin validTier(_tier) {
        tierPricing[_tier].priorityWeight = _weight;
    }

    // ============================================================
    // 优先级队列
    // ============================================================

    /**
     * @notice 获取下一个待处理请求（最高优先级）
     * @return requestId 请求 ID
     * @dev 按优先级分数降序排列，分数越高优先级越高
     *      优先级分数 = 层级权重 × log2(燃烧金额)
     */
    function getNextPendingRequest() external view returns (uint256 requestId) {
        if (pendingRequestIds.length == 0) revert NoPendingRequests();

        // 找到最高优先级的请求
        uint256 highestScore = 0;
        uint256 highestIndex = 0;

        for (uint256 i = 0; i < pendingRequestIds.length; i++) {
            uint256 reqId = pendingRequestIds[i];
            if (!burnRequests[reqId].processed && burnRequests[reqId].priorityScore > highestScore) {
                highestScore = burnRequests[reqId].priorityScore;
                highestIndex = i;
                requestId = reqId;
            }
        }
    }

    /**
     * @notice 处理请求（标记为已处理）
     * @param _requestId 请求 ID
     */
    function processRequest(uint256 _requestId) external onlyAdmin {
        if (_requestId == 0 || _requestId > requestCount) revert InvalidAmount();
        if (burnRequests[_requestId].processed) revert NoPendingRequests();

        burnRequests[_requestId].processed = true;

        // 从待处理列表中移除
        _removeFromPending(_requestId);

        emit RequestProcessed(_requestId);
    }

    /**
     * @notice 获取待处理请求数量
     * @return 待处理请求的数量
     */
    function getPendingRequestCount() external view returns (uint256) {
        return pendingRequestIds.length;
    }

    // ============================================================
    // 资金提取
    // ============================================================

    /**
     * @notice 查询国库余额
     * @return 国库累计可提取余额
     */
    function getTreasuryBalance() external view returns (uint256) {
        return treasuryBalance;
    }

    /**
     * @notice 提取国库资金（仅管理员）
     * @param _amount 提取金额
     * @param _to 接收地址
     */
    function withdrawTreasury(uint256 _amount, address _to) external onlyAdmin {
        if (_amount == 0) revert InvalidAmount();
        if (_to == address(0)) revert ZeroAddress();
        if (treasuryBalance < _amount) revert InsufficientBalance();

        treasuryBalance -= _amount;

        // 国库资金通过铸造新的 AIC 来补偿（因为原始代币已被燃烧）
        // 或者从合约持有的代币中转出
        // 此处使用直接转 ETH 的模式，如果合约持有 AIC 则转账
        if (token.balanceOf(address(this)) >= _amount) {
            token.transfer(_to, _amount);
        }

        emit TreasuryWithdrawn(_to, _amount);
    }

    /**
     * @notice 分发矿工池资金（仅管理员）
     * @param _nodes 接收资金矿工地址列表
     * @param _amounts 对应分配金额列表
     * @dev 将矿工池中的资金按比例分配给矿工节点
     */
    function distributeMinerRewards(
        address[] calldata _nodes,
        uint256[] calldata _amounts
    ) external onlyAdmin {
        if (_nodes.length != _amounts.length) revert InvalidAmount();

        uint256 totalDistribution = 0;
        for (uint256 i = 0; i < _amounts.length; i++) {
            totalDistribution += _amounts[i];
        }

        if (minerPoolBalance < totalDistribution) revert InsufficientBalance();
        minerPoolBalance -= totalDistribution;

        // 分发资金（通过铸造方式补偿，因为原始代币已被燃烧）
        for (uint256 i = 0; i < _nodes.length; i++) {
            if (_amounts[i] > 0 && _nodes[i] != address(0)) {
                token.mint(_nodes[i], _amounts[i]);
            }
        }
    }

    // ============================================================
    // 速率限制
    // ============================================================

    /**
     * @notice 设置每地址每日燃烧限额（仅管理员）
     * @param _limit 每日限额（0 = 无限制）
     */
    function setDailyBurnLimit(uint256 _limit) external onlyAdmin {
        uint256 oldLimit = dailyBurnLimit;
        dailyBurnLimit = _limit;
        emit DailyBurnLimitChanged(oldLimit, _limit);
    }

    /**
     * @notice 设置全局每日燃烧限额（仅管理员）
     * @param _limit 全局每日限额（0 = 无限制）
     */
    function setGlobalDailyBurnLimit(uint256 _limit) external onlyAdmin {
        globalDailyBurnLimit = _limit;
    }

    /**
     * @notice 查询地址当日剩余可燃烧量
     * @param _user 用户地址
     * @return 剩余可燃烧量
     */
    function getRemainingDailyBurn(address _user) external view returns (uint256) {
        DailyBurnInfo storage info = dailyBurnInfo[_user];

        // 检查是否需要重置（跨天）
        if (block.timestamp >= info.lastResetTimestamp + 1 days) {
            return dailyBurnLimit == 0 ? type(uint256).max : dailyBurnLimit;
        }

        if (dailyBurnLimit == 0) return type(uint256).max;
        if (info.totalBurned >= dailyBurnLimit) return 0;
        return dailyBurnLimit - info.totalBurned;
    }

    // ============================================================
    // 管理功能
    // ============================================================

    /**
     * @notice 设置国库地址（仅管理员）
     * @param _treasury 新的国库地址
     */
    function setTreasury(address _treasury) external onlyAdmin {
        if (_treasury == address(0)) revert ZeroAddress();
        treasury = _treasury;
    }

    /**
     * @notice 设置挖矿池地址（仅管理员）
     * @param _miningPool 新的挖矿池地址
     */
    function setMiningPool(address _miningPool) external onlyAdmin {
        if (_miningPool == address(0)) revert ZeroAddress();
        miningPool = _miningPool;
    }

    /**
     * @notice 暂停合约（仅管理员）
     */
    function pause() external onlyAdmin {
        paused = true;
        emit APIAccessPaused(msg.sender);
    }

    /**
     * @notice 恢复合约（仅管理员）
     */
    function unpause() external onlyAdmin {
        paused = false;
        emit APIAccessUnpaused(msg.sender);
    }

    // ============================================================
    // 内部函数
    // ============================================================

    /**
     * @notice 检查并重置每日燃烧限额
     * @param user 用户地址
     */
    function _checkAndUpdateDailyLimit(address user) internal {
        DailyBurnInfo storage info = dailyBurnInfo[user];

        // 跨天重置
        if (block.timestamp >= info.lastResetTimestamp + 1 days) {
            info.totalBurned = 0;
            info.lastResetTimestamp = block.timestamp;
        } else if (info.lastResetTimestamp == 0) {
            info.lastResetTimestamp = block.timestamp;
        }

        // 检查限额
        if (dailyBurnLimit > 0 && info.totalBurned + 1 > dailyBurnLimit) {
            revert DailyBurnLimitExceeded();
        }
    }

    /**
     * @notice 检查并重置全局每日燃烧限额
     */
    function _checkAndUpdateGlobalDailyLimit() internal {
        // 跨天重置
        if (block.timestamp >= globalLastResetTimestamp + 1 days) {
            globalDailyBurned = 0;
            globalLastResetTimestamp = block.timestamp;
        }

        // 检查全局限额
        if (globalDailyBurnLimit > 0 && globalDailyBurned + 1 > globalDailyBurnLimit) {
            revert GlobalDailyBurnLimitExceeded();
        }
    }

    /**
     * @notice 计算优先级分数
     * @param _tier 层级
     * @param _burnAmount 燃烧金额
     * @return 优先级分数
     * @dev 优先级分数 = 层级权重 × log2(燃烧金额) 的近似值
     *      使用 log2 的整数近似：找到最高有效位
     */
    function _calculatePriorityScore(Tier _tier, uint256 _burnAmount) internal view returns (uint256) {
        uint256 weight = tierPricing[_tier].priorityWeight;

        // 近似计算 log2(_burnAmount)：找到最高有效位位置
        if (_burnAmount == 0) return 0;

        uint256 logValue = 0;
        uint256 value = _burnAmount;
        while (value > 1) {
            value >>= 1;
            logValue++;
        }

        return weight * (logValue + 1);
    }

    /**
     * @notice 从待处理列表中移除请求
     * @param _requestId 要移除的请求 ID
     */
    function _removeFromPending(uint256 _requestId) internal {
        uint256 length = pendingRequestIds.length;
        for (uint256 i = 0; i < length; i++) {
            if (pendingRequestIds[i] == _requestId) {
                pendingRequestIds[i] = pendingRequestIds[length - 1];
                pendingRequestIds.pop();
                break;
            }
        }
    }
}
