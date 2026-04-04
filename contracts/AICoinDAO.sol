// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./AICoinToken.sol";
import "./Mining.sol";
import "./Governance.sol";
import "./APIAccess.sol";

/**
 * @title AICoinDAO
 * @notice DAO 主合约 - AICoin 去中心化 AI 算力挖矿网络的聚合管理合约
 * @dev 本合约是整个 AICoin 生态系统的入口点和统一管理层，负责：
 *      1. 部署和链接所有子合约（Token、Mining、Governance、APIAccess）
 *      2. 管理员初始设置和权限分配
 *      3. 合约升级模式（管理员控制的可升级模式）
 *      4. 紧急暂停/恢复所有子合约
 *      5. 统一的事件日志和状态查询
 *
 * 升级策略：
 * - 采用管理员控制的简单可升级模式
 * - 管理员可以更新各子合约的地址引用
 * - 升级前需经过治理投票通过
 * - 紧急升级由管理员直接执行（需在 24 小时内通过治理确认）
 */
contract AICoinDAO {

    // ============================================================
    // 常量
    // ============================================================

    /// @notice 紧急升级时间锁：24 小时
    uint256 public constant EMERGENCY_UPGRADE_DELAY = 24 hours;

    /// @notice 治理确认时间窗口：7 天
    uint256 public constant GOVERNANCE_CONFIRMATION_WINDOW = 7 days;

    /// @notice 版本号
    string public constant VERSION = "1.0.0";

    // ============================================================
    // 数据结构
    // ============================================================

    /// @notice 升级提案
    struct UpgradeProposal {
        address oldContract;          // 旧合约地址
        address newContract;          // 新合约地址
        string contractName;          // 合约名称
        uint256 proposedBlock;        // 提议升级的区块号
        uint256 executedBlock;        // 实际执行区块号
        bool executed;                // 是否已执行
        bool isEmergency;             // 是否为紧急升级
        bool governanceConfirmed;     // 是否已通过治理确认
    }

    // ============================================================
    // 状态变量
    // ============================================================

    /// @notice DAO 管理员地址
    address public admin;

    /// @notice 待定管理员地址（用于两步转移管理员）
    address public pendingAdmin;

    /// @notice DAO 是否已暂停（全局暂停开关）
    bool public paused;

    /// @notice DAO 初始化时间戳
    uint256 public initializedAt;

    /// @notice 升级提案计数器
    uint256 public upgradeProposalCount;

    /// @notice 是否已初始化（防止重复初始化）
    bool public isInitialized;

    // ============================================================
    // 子合约引用
    // ============================================================

    /// @notice AICoin 代币合约
    AICoinToken public tokenContract;

    /// @notice 挖矿合约
    Mining public miningContract;

    /// @notice 治理合约
    Governance public governanceContract;

    /// @notice API 访问合约
    APIAccess public apiAccessContract;

    // ============================================================
    // 映射
    // ============================================================

    /// @notice 升级提案 ID => 提案数据
    mapping(uint256 => UpgradeProposal) public upgradeProposals;

    /// @notice 合约名称 => 当前合约地址
    mapping(string => address) public contractAddresses;

    // ============================================================
    // 事件
    // ============================================================

    /// @notice DAO 初始化事件
    event DAOInitialized(
        address token,
        address mining,
        address governance,
        address apiAccess
    );

    /// @notice 全局暂停事件
    event DAOPaused(address admin);

    /// @notice 全局恢复事件
    event DAOUnpaused(address admin);

    /// @notice 管理员变更提案事件
    event AdminChangeProposed(address indexed currentAdmin, address indexed pendingAdmin);

    /// @notice 管理员变更接受事件
    event AdminChangeAccepted(address indexed oldAdmin, address indexed newAdmin);

    /// @notice 合约升级提议事件
    event UpgradeProposed(
        uint256 indexed proposalId,
        string contractName,
        address oldContract,
        address newContract,
        bool isEmergency
    );

    /// @notice 合约升级执行事件
    event UpgradeExecuted(
        uint256 indexed proposalId,
        string contractName,
        address newContract
    );

    /// @notice 治理确认升级事件
    event UpgradeGovernanceConfirmed(uint256 indexed proposalId);

    /// @notice 子合约暂停事件
    event ContractPaused(string contractName, bool isPaused);

    // ============================================================
    // 错误
    // ============================================================

    error NotAdmin();
    error NotPendingAdmin();
    error AlreadyInitialized();
    error ContractPaused();
    error AlreadyPaused();
    error NotPaused();
    error UpgradeNotProposed();
    error UpgradeAlreadyExecuted();
    error EmergencyDelayNotElapsed();
    error GovernanceNotConfirmed();
    error ZeroAddress();
    error InvalidContractName();
    error SameAddress();

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

    modifier nonZeroAddress(address _addr) {
        if (_addr == address(0)) revert ZeroAddress();
        _;
    }

    // ============================================================
    // 构造函数
    // ============================================================

    /**
     * @notice 构造函数 - 部署空的 DAO 合约
     * @dev 需要调用 initialize() 进行初始化
     */
    constructor() {
        admin = msg.sender;
    }

    // ============================================================
    // 初始化
    // ============================================================

    /**
     * @notice 初始化 DAO 合约，部署并链接所有子合约
     * @param _treasury 国库地址（接收 API 燃烧费用的 20%）
     * @return tokenAddr 代币合约地址
     * @return miningAddr 挖矿合约地址
     * @return govAddr 治理合约地址
     * @return apiAddr API 访问合约地址
     * @dev 仅可调用一次，部署所有子合约并设置互相关联的权限
     */
    function initialize(address _treasury)
        external
        onlyAdmin
        nonZeroAddress(_treasury)
        returns (
            address tokenAddr,
            address miningAddr,
            address govAddr,
            address apiAddr
        )
    {
        if (isInitialized) revert AlreadyInitialized();

        // 1. 部署 AICoin 代币合约
        tokenContract = new AICoinToken(admin);
        tokenAddr = address(tokenContract);

        // 2. 部署挖矿合约
        miningContract = new Mining(tokenAddr, admin, block.number);
        miningAddr = address(miningContract);

        // 3. 部署治理合约
        governanceContract = new Governance(tokenAddr, admin, address(this));
        govAddr = address(governanceContract);

        // 4. 部署 API 访问合约
        apiAccessContract = new APIAccess(tokenAddr, admin, _treasury, miningAddr);
        apiAddr = address(apiAccessContract);

        // 5. 在代币合约中设置挖矿合约地址（铸币权限）
        tokenContract.setMiningContract(miningAddr);

        // 6. 在代币合约中设置治理合约地址
        tokenContract.setGovernanceContract(govAddr);

        // 7. 在代币合约中授予 API 访问合约 API_ACCESS 角色
        tokenContract.grantRole(tokenContract.ROLE_API_ACCESS(), apiAddr);

        // 8. 记录合约地址映射
        contractAddresses["Token"] = tokenAddr;
        contractAddresses["Mining"] = miningAddr;
        contractAddresses["Governance"] = govAddr;
        contractAddresses["APIAccess"] = apiAddr;

        initializedAt = block.timestamp;
        isInitialized = true;

        emit DAOInitialized(tokenAddr, miningAddr, govAddr, apiAddr);
    }

    /**
     * @notice 使用已有合约地址初始化（用于设置模式）
     * @param _token 代币合约地址
     * @param _mining 挖矿合约地址
     * @param _governance 治理合约地址
     * @param _apiAccess API 访问合约地址
     * @param _treasury 国库地址
     */
    function initializeWithAddresses(
        address _token,
        address _mining,
        address _governance,
        address _apiAccess,
        address _treasury
    ) external onlyAdmin {
        if (isInitialized) revert AlreadyInitialized();

        tokenContract = AICoinToken(_token);
        miningContract = Mining(_mining);
        governanceContract = Governance(_governance);
        apiAccessContract = APIAccess(_apiAccess);

        // 设置代币合约权限
        tokenContract.setMiningContract(_mining);
        tokenContract.setGovernanceContract(_governance);
        tokenContract.grantRole(tokenContract.ROLE_API_ACCESS(), _apiAccess);

        // 记录合约地址映射
        contractAddresses["Token"] = _token;
        contractAddresses["Mining"] = _mining;
        contractAddresses["Governance"] = _governance;
        contractAddresses["APIAccess"] = _apiAccess;

        initializedAt = block.timestamp;
        isInitialized = true;

        emit DAOInitialized(_token, _mining, _governance, _apiAccess);
    }

    // ============================================================
    // 全局紧急控制
    // ============================================================

    /**
     * @notice 全局暂停 - 暂停所有子合约
     * @dev 同时暂停 Token、Mining、Governance、APIAccess 四个合约
     */
    function emergencyPause() external onlyAdmin {
        if (paused) revert AlreadyPaused();
        paused = true;

        // 暂停所有子合约
        try tokenContract.pause() {} catch {}
        try miningContract.pause() {} catch {}
        try governanceContract.pause() {} catch {}
        try apiAccessContract.pause() {} catch {}

        emit DAOPaused(admin);
        emit ContractPaused("All", true);
    }

    /**
     * @notice 全局恢复 - 恢复所有子合约
     * @dev 同时恢复 Token、Mining、Governance、APIAccess 四个合约
     */
    function emergencyUnpause() external onlyAdmin {
        if (!paused) revert NotPaused();
        paused = false;

        // 恢复所有子合约
        try tokenContract.unpause() {} catch {}
        try miningContract.unpause() {} catch {}
        try governanceContract.unpause() {} catch {}
        try apiAccessContract.unpause() {} catch {}

        emit DAOUnpaused(admin);
        emit ContractPaused("All", false);
    }

    /**
     * @notice 暂停指定子合约
     * @param contractName 合约名称："Token", "Mining", "Governance", "APIAccess"
     */
    function pauseContract(string calldata contractName) external onlyAdmin whenNotPaused {
        address contractAddr = contractAddresses[contractName];
        if (contractAddr == address(0)) revert InvalidContractName();

        _pauseNamedContract(contractName, contractAddr);
    }

    /**
     * @notice 恢复指定子合约
     * @param contractName 合约名称
     */
    function unpauseContract(string calldata contractName) external onlyAdmin {
        address contractAddr = contractAddresses[contractName];
        if (contractAddr == address(0)) revert InvalidContractName();

        _unpauseNamedContract(contractName, contractAddr);
    }

    // ============================================================
    // 合约升级
    // ============================================================

    /**
     * @notice 提议升级某个子合约
     * @param contractName 要升级的合约名称
     * @param newContract 新合约地址
     * @param isEmergency 是否为紧急升级
     * @return proposalId 升级提案 ID
     */
    function proposeUpgrade(
        string calldata contractName,
        address newContract,
        bool isEmergency
    ) external onlyAdmin nonZeroAddress(newContract) returns (uint256 proposalId) {
        address oldContract = contractAddresses[contractName];
        if (oldContract == address(0)) revert InvalidContractName();
        if (oldContract == newContract) revert SameAddress();

        proposalId = ++upgradeProposalCount;
        upgradeProposals[proposalId] = UpgradeProposal({
            oldContract: oldContract,
            newContract: newContract,
            contractName: contractName,
            proposedBlock: block.number,
            executedBlock: 0,
            executed: false,
            isEmergency: isEmergency,
            governanceConfirmed: false
        });

        emit UpgradeProposed(proposalId, contractName, oldContract, newContract, isEmergency);
    }

    /**
     * @notice 执行合约升级
     * @param proposalId 升级提案 ID
     * @dev 非紧急升级需治理确认 + 时间延迟
     *      紧急升级需等待 24 小时延迟
     */
    function executeUpgrade(uint256 proposalId) external onlyAdmin {
        UpgradeProposal storage proposal = upgradeProposals[proposalId];
        if (proposal.proposedBlock == 0) revert UpgradeNotProposed();
        if (proposal.executed) revert UpgradeAlreadyExecuted();

        if (proposal.isEmergency) {
            // 紧急升级：需要等待 24 小时延迟
            if (block.number < proposal.proposedBlock + (EMERGENCY_UPGRADE_DELAY / 12)) {
                revert EmergencyDelayNotElapsed();
            }
        } else {
            // 正常升级：需要治理确认
            if (!proposal.governanceConfirmed) revert GovernanceNotConfirmed();
        }

        proposal.executed = true;
        proposal.executedBlock = block.number;

        // 更新合约引用
        _updateContractReference(proposal.contractName, proposal.newContract);

        emit UpgradeExecuted(proposalId, proposal.contractName, proposal.newContract);
    }

    /**
     * @notice 确认升级提案（由治理合约或管理员调用）
     * @param proposalId 升级提案 ID
     */
    function confirmUpgradeGovernance(uint256 proposalId) external onlyAdmin {
        UpgradeProposal storage proposal = upgradeProposals[proposalId];
        if (proposal.proposedBlock == 0) revert UpgradeNotProposed();
        if (proposal.executed) revert UpgradeAlreadyExecuted();

        proposal.governanceConfirmed = true;

        emit UpgradeGovernanceConfirmed(proposalId);
    }

    /**
     * @notice 直接设置合约地址（紧急情况使用）
     * @param contractName 合约名称
     * @param newContract 新合约地址
     * @dev 仅在极端紧急情况下使用，会触发事件记录
     */
    function setContractAddress(
        string calldata contractName,
        address newContract
    ) external onlyAdmin nonZeroAddress(newContract) {
        if (contractAddresses[contractName] == address(0)) revert InvalidContractName();

        _updateContractReference(contractName, newContract);
    }

    // ============================================================
    // 管理员管理
    // ============================================================

    /**
     * @notice 提议转移管理员权限（两步转移）
     * @param newAdmin 新管理员地址
     */
    function proposeAdminChange(address newAdmin) external onlyAdmin nonZeroAddress(newAdmin) {
        pendingAdmin = newAdmin;
        emit AdminChangeProposed(admin, newAdmin);
    }

    /**
     * @notice 接受管理员权限（由新管理员调用）
     */
    function acceptAdmin() external {
        if (msg.sender != pendingAdmin) revert NotPendingAdmin();

        address oldAdmin = admin;
        admin = pendingAdmin;
        pendingAdmin = address(0);

        emit AdminChangeAccepted(oldAdmin, admin);
    }

    // ============================================================
    // 统一状态查询
    // ============================================================

    /**
     * @notice 获取 DAO 系统状态摘要
     * @return tokenSupply 代币总供应量
     * @return maxSupply 代币最大供应量
     * @return totalNetworkPower 全网总算力
     * @return activeNodes 活跃节点数
     * @return totalBurned 累计燃烧量
     * @return proposalCount 治理提案数
     * @return isPaused 是否暂停
     */
    function getSystemStatus()
        external
        view
        returns (
            uint256 tokenSupply,
            uint256 maxSupply,
            uint256 totalNetworkPower,
            uint256 activeNodes,
            uint256 totalBurned,
            uint256 proposalCount,
            bool isPaused
        )
    {
        tokenSupply = isInitialized ? tokenContract.totalSupply() : 0;
        maxSupply = 21_000_000 * 10 ** 18;
        totalNetworkPower = isInitialized ? miningContract.getTotalNetworkPower() : 0;
        activeNodes = isInitialized ? miningContract.activeNodeCount() : 0;
        totalBurned = isInitialized ? apiAccessContract.totalBurned() : 0;
        proposalCount = isInitialized ? governanceContract.proposalCount() : 0;
        isPaused = paused;
    }

    /**
     * @notice 获取所有合约地址
     * @return tokenAddr 代币合约地址
     * @return miningAddr 挖矿合约地址
     * @return govAddr 治理合约地址
     * @return apiAddr API 访问合约地址
     */
    function getAllContractAddresses()
        external
        view
        returns (
            address tokenAddr,
            address miningAddr,
            address govAddr,
            address apiAddr
        )
    {
        tokenAddr = address(tokenContract);
        miningAddr = address(miningContract);
        govAddr = address(governanceContract);
        apiAddr = address(apiAccessContract);
    }

    /**
     * @notice 查询升级提案
     * @param proposalId 提案 ID
     * @return 合约名称
     * @return 旧合约地址
     * @return 新合约地址
     * @return 是否已执行
     * @return 是否为紧急升级
     * @return 是否已治理确认
     */
    function getUpgradeProposal(uint256 proposalId)
        external
        view
        returns (
            string memory contractName,
            address oldContract,
            address newContract,
            bool executed,
            bool isEmergency,
            bool governanceConfirmed
        )
    {
        UpgradeProposal storage p = upgradeProposals[proposalId];
        return (
            p.contractName,
            p.oldContract,
            p.newContract,
            p.executed,
            p.isEmergency,
            p.governanceConfirmed
        );
    }

    // ============================================================
    // 内部函数
    // ============================================================

    /**
     * @notice 按名称暂停子合约
     * @param name 合约名称
     * @param addr 合约地址
     */
    function _pauseNamedContract(string memory name, address addr) internal {
        bytes32 nameHash = keccak256(bytes(name));

        if (nameHash == keccak256("Token")) {
            tokenContract.pause();
        } else if (nameHash == keccak256("Mining")) {
            miningContract.pause();
        } else if (nameHash == keccak256("Governance")) {
            governanceContract.pause();
        } else if (nameHash == keccak256("APIAccess")) {
            apiAccessContract.pause();
        } else {
            revert InvalidContractName();
        }

        emit ContractPaused(name, true);
    }

    /**
     * @notice 按名称恢复子合约
     * @param name 合约名称
     * @param addr 合约地址
     */
    function _unpauseNamedContract(string memory name, address addr) internal {
        bytes32 nameHash = keccak256(bytes(name));

        if (nameHash == keccak256("Token")) {
            tokenContract.unpause();
        } else if (nameHash == keccak256("Mining")) {
            miningContract.unpause();
        } else if (nameHash == keccak256("Governance")) {
            governanceContract.unpause();
        } else if (nameHash == keccak256("APIAccess")) {
            apiAccessContract.unpause();
        } else {
            revert InvalidContractName();
        }

        emit ContractPaused(name, false);
    }

    /**
     * @notice 更新合约引用和权限
     * @param contractName 合约名称
     * @param newContract 新合约地址
     */
    function _updateContractReference(string memory contractName, address newContract) internal {
        bytes32 nameHash = keccak256(bytes(contractName));

        if (nameHash == keccak256("Token")) {
            tokenContract = AICoinToken(newContract);
            // 重新设置挖矿合约和治理合约引用
            tokenContract.setMiningContract(address(miningContract));
            tokenContract.setGovernanceContract(address(governanceContract));
        } else if (nameHash == keccak256("Mining")) {
            miningContract = Mining(newContract);
            tokenContract.setMiningContract(newContract);
        } else if (nameHash == keccak256("Governance")) {
            governanceContract = Governance(newContract);
            tokenContract.setGovernanceContract(newContract);
        } else if (nameHash == keccak256("APIAccess")) {
            apiAccessContract = APIAccess(newContract);
            tokenContract.grantRole(tokenContract.ROLE_API_ACCESS(), newContract);
        }

        contractAddresses[contractName] = newContract;
    }

    /// @notice 允许合约接收 ETH
    receive() external payable {}
}
