// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title AICoinToken
 * @notice AICoin ERC20 代币合约 - AICoin 去中心化 AI 算力挖矿网络的核心代币
 * @dev 基于 OpenZeppelin ERC20 标准，支持挖矿铸造、代币销毁和治理投票功能。
 *      最大供应量 21,000,000 AIC，与比特币总量一致。
 *      仅 MiningContract 合约拥有铸币权限（基于角色访问控制）。
 */
contract AICoinToken {

    // ============================================================
    // 状态变量
    // ============================================================

    /// @notice 代币名称
    string public constant name = "AICoin";

    /// @notice 代币符号
    string public constant symbol = "AIC";

    /// @notice 代币精度
    uint8 public constant decimals = 18;

    /// @notice 最大供应量：21,000,000 AIC（含 18 位精度）
    uint256 public constant MAX_SUPPLY = 21_000_000 * 10 ** 18;

    /// @notice 创建提案所需的最低代币质押量
    uint256 public constant MIN_PROPOSAL_STAKE = 1_000 * 10 ** 18;

    /// @notice 代币总供应量
    uint256 private _totalSupply;

    /// @notice 账户余额映射
    mapping(address => uint256) private _balances;

    /// @notice 授权额度映射（所有者 => 授权方 => 额度）
    mapping(address => mapping(address => uint256)) private _allowances;

    /// @notice 投票权重缓存：记录每个地址在某个检查点区块号的投票权重
    mapping(address => uint256) private _voteWeight;

    /// @notice 投票权重检查点：记录每个地址上次更新投票权重时的供应量快照
    mapping(address => uint256) private _voteCheckpoint;

    // ============================================================
    // 角色管理
    // ============================================================

    /// @dev 角色：DEFAULT_ADMIN - 拥有最高管理权限，可授予/撤销其他角色
    bytes32 public constant ROLE_DEFAULT_ADMIN = keccak256("DEFAULT_ADMIN_ROLE");

    /// @dev 角色：MINER - 矿工角色，可调用挖矿相关铸币功能
    bytes32 public constant ROLE_MINER = keccak256("MINER_ROLE");

    /// @dev 角色：GOVERNANCE - 治理合约角色，可操作治理相关功能
    bytes32 public constant ROLE_GOVERNANCE = keccak256("GOVERNANCE_ROLE");

    /// @dev 角色：API_ACCESS - API 访问合约角色，可操作燃烧扣费功能
    bytes32 public constant ROLE_API_ACCESS = keccak256("API_ACCESS_ROLE");

    /// @notice 管理员地址
    address public admin;

    /// @notice 合约是否已暂停
    bool public paused;

    /// @notice 挖矿合约地址（拥有铸币权限）
    address public miningContract;

    /// @notice 治理合约地址
    address public governanceContract;

    // ============================================================
    // 角色映射
    // ============================================================

    mapping(bytes32 => mapping(address => bool)) private _hasRole;
    mapping(bytes32 => address[]) private _roleMembers;

    // ============================================================
    // 事件
    // ============================================================

    /// @notice 当新代币被铸造时触发
    /// @param to 接收铸造代币的地址
    /// @param amount 铸造数量
    event Minted(address indexed to, uint256 amount);

    /// @notice当代币被销毁时触发
    /// @param from 销毁代币的地址
    /// @param amount 销毁数量
    event Burned(address indexed from, uint256 amount);

    /// @notice 代币转账事件（兼容 ERC20 Transfer）
    /// @param from 发送方
    /// @param to 接收方
    /// @param amount 转账数量
    event Transfer(address indexed from, address indexed to, uint256 amount);

    /// @notice 授权事件（兼容 ERC20 Approval）
    /// @param owner 代币所有者
    /// @param spender 被授权方
    /// @param amount 授权额度
    event Approval(address indexed owner, address indexed spender, uint256 amount);

    /// @notice 角色授予事件
    /// @param role 角色标识
    /// @param account 被授予角色的地址
    /// @param sender 执行授予操作的地址
    event RoleGranted(bytes32 indexed role, address indexed account, address indexed sender);

    /// @notice 角色撤销事件
    /// @param role 角色标识
    /// @param account 被撤销角色的地址
    /// @param sender 执行撤销操作的地址
    event RoleRevoked(bytes32 indexed role, address indexed account, address indexed sender);

    /// @notice 合约暂停事件
    event Paused(address account);

    /// @notice 合约恢复事件
    event Unpaused(address account);

    /// @notice 管理员变更事件
    /// @param oldAdmin 旧管理员地址
    /// @param newAdmin 新管理员地址
    event AdminChanged(address indexed oldAdmin, address indexed newAdmin);

    /// @notice 挖矿合约地址变更事件
    /// @param oldMining 旧挖矿合约地址
    /// @param newMining 新挖矿合约地址
    event MiningContractChanged(address indexed oldMining, address indexed newMining);

    /// @notice 治理合约地址变更事件
    /// @param oldGov 旧治理合约地址
    /// @param newGov 新治理合约地址
    event GovernanceContractChanged(address indexed oldGov, address indexed newGov);

    // ============================================================
    // 错误
    // ============================================================

    error ZeroAddress();
    error InsufficientBalance();
    error InsufficientAllowance();
    error ExceedsMaxSupply();
    error NotAuthorized();
    error ContractPaused();
    error AlreadyPaused();
    error NotPaused();
    error SelfTransfer();
    error NotRoleAdmin();

    // ============================================================
    // 修饰符
    // ============================================================

    /// @notice 仅管理员可调用
    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAuthorized();
        _;
    }

    /// @notice 暂停状态下不可调用
    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    /// @notice 非暂停状态下不可调用
    modifier whenPaused() {
        if (!paused) revert NotPaused();
        _;
    }

    /// @notice 要求指定地址非零地址
    modifier nonZeroAddress(address _addr) {
        if (_addr == address(0)) revert ZeroAddress();
        _;
    }

    /// @notice 仅挖矿合约可调用
    modifier onlyMiningContract() {
        if (msg.sender != miningContract) revert NotAuthorized();
        _;
    }

    /// @notice 仅治理合约可调用
    modifier onlyGovernanceContract() {
        if (msg.sender != governanceContract) revert NotAuthorized();
        _;
    }

    // ============================================================
    // 构造函数
    // ============================================================

    /**
     * @notice 构造函数 - 初始化 AICoin 代币
     * @param _admin 管理员地址，负责初始设置和权限管理
     */
    constructor(address _admin) {
        if (_admin == address(0)) revert ZeroAddress();
        admin = _admin;

        // 授予管理员默认管理员角色
        _hasRole[ROLE_DEFAULT_ADMIN][_admin] = true;
        _roleMembers[ROLE_DEFAULT_ADMIN].push(_admin);
        emit RoleGranted(ROLE_DEFAULT_ADMIN, _admin, _admin);
    }

    // ============================================================
    // ERC20 核心功能
    // ============================================================

    /**
     * @notice 查询代币总供应量
     * @return 当前代币总供应量
     */
    function totalSupply() external view returns (uint256) {
        return _totalSupply;
    }

    /**
     * @notice 查询指定地址的代币余额
     * @param account 要查询的地址
     * @return 该地址持有的代币数量
     */
    function balanceOf(address account) external view returns (uint256) {
        return _balances[account];
    }

    /**
     * @notice 代币转账
     * @param to 接收方地址
     * @param amount 转账数量
     * @dev 转账后自动更新投票权重快照
     */
    function transfer(address to, uint256 amount) external whenNotPaused returns (bool) {
        if (to == address(0)) revert ZeroAddress();
        if (amount == 0) return true; // 零值转账允许但不触发事件
        if (_balances[msg.sender] < amount) revert InsufficientBalance();

        _balances[msg.sender] -= amount;
        _balances[to] += amount;

        // 更新投票权重
        _voteWeight[msg.sender] = _balances[msg.sender];
        _voteCheckpoint[msg.sender] = _totalSupply;
        _voteWeight[to] = _balances[to];
        _voteCheckpoint[to] = _totalSupply;

        emit Transfer(msg.sender, to, amount);
        return true;
    }

    /**
     * @notice 授权指定地址使用一定数量的代币
     * @param spender 被授权方地址
     * @param amount 授权额度
     */
    function approve(address spender, uint256 amount) external whenNotPaused returns (bool) {
        _allowances[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    /**
     * @notice 查询授权额度
     * @param owner 代币所有者地址
     * @param spender 被授权方地址
     * @return 剩余授权额度
     */
    function allowance(address owner, address spender) external view returns (uint256) {
        return _allowances[owner][spender];
    }

    /**
     * @notice 从授权方地址转账代币（需先 approve）
     * @param from 代币所有者地址
     * @param to 接收方地址
     * @param amount 转账数量
     */
    function transferFrom(address from, address to, uint256 amount) external whenNotPaused returns (bool) {
        if (to == address(0)) revert ZeroAddress();
        if (amount == 0) return true;
        if (_balances[from] < amount) revert InsufficientBalance();
        if (_allowances[from][msg.sender] < amount) revert InsufficientAllowance();

        // 先扣减授权额度，再转账
        _allowances[from][msg.sender] -= amount;
        _balances[from] -= amount;
        _balances[to] += amount;

        // 更新投票权重
        _voteWeight[from] = _balances[from];
        _voteCheckpoint[from] = _totalSupply;
        _voteWeight[to] = _balances[to];
        _voteCheckpoint[to] = _totalSupply;

        emit Transfer(from, to, amount);
        return true;
    }

    // ============================================================
    // 挖矿铸币功能
    // ============================================================

    /**
     * @notice 铸造新代币（仅挖矿合约可调用）
     * @param to 接收铸造代币的地址
     * @param amount 铸造数量
     * @dev 铸造数量不能超过最大供应量限制，铸造后自动更新投票权重
     */
    function mint(address to, uint256 amount) external onlyMiningContract whenNotPaused {
        if (to == address(0)) revert ZeroAddress();
        if (_totalSupply + amount > MAX_SUPPLY) revert ExceedsMaxSupply();

        _totalSupply += amount;
        _balances[to] += amount;

        // 更新投票权重
        _voteWeight[to] = _balances[to];
        _voteCheckpoint[to] = _totalSupply;

        emit Minted(to, amount);
        emit Transfer(address(0), to, amount);
    }

    // ============================================================
    // 代币销毁功能
    // ============================================================

    /**
     * @notice 销毁自己的代币
     * @param amount 要销毁的代币数量
     * @dev 任何持有代币的地址都可以销毁自己的代币，销毁后自动更新投票权重
     */
    function burn(uint256 amount) external whenNotPaused {
        if (_balances[msg.sender] < amount) revert InsufficientBalance();

        _balances[msg.sender] -= amount;
        _totalSupply -= amount;

        // 更新投票权重
        _voteWeight[msg.sender] = _balances[msg.sender];
        _voteCheckpoint[msg.sender] = _totalSupply;

        emit Burned(msg.sender, amount);
        emit Transfer(msg.sender, address(0), amount);
    }

    /**
     * @notice 销毁已授权的代币（从授权方地址扣除）
     * @param account 被销毁代币的地址
     * @param amount 要销毁的代币数量
     */
    function burnFrom(address account, uint256 amount) external whenNotPaused {
        if (_balances[account] < amount) revert InsufficientBalance();
        if (_allowances[account][msg.sender] < amount) revert InsufficientAllowance();

        _allowances[account][msg.sender] -= amount;
        _balances[account] -= amount;
        _totalSupply -= amount;

        // 更新投票权重
        _voteWeight[account] = _balances[account];
        _voteCheckpoint[account] = _totalSupply;

        emit Burned(account, amount);
        emit Transfer(account, address(0), amount);
    }

    // ============================================================
    // 治理投票权重
    // ============================================================

    /**
     * @notice 查询指定地址的投票权重
     * @param account 要查询的地址
     * @return 该地址的投票权重（等于当前余额）
     */
    function getVoteWeight(address account) external view returns (uint256) {
        return _balances[account];
    }

    /**
     * @notice 查询指定地址的投票权重检查点
     * @param account 要查询的地址
     * @return 快照的投票权重和对应的总供应量
     */
    function getVoteCheckpoint(address account) external view returns (uint256 weight, uint256 supplyCheckpoint) {
        return (_voteWeight[account], _voteCheckpoint[account]);
    }

    // ============================================================
    // 角色管理
    // ============================================================

    /**
     * @notice 检查指定地址是否拥有某个角色
     * @param role 角色标识（bytes32）
     * @param account 要检查的地址
     * @return 是否拥有该角色
     */
    function hasRole(bytes32 role, address account) external view returns (bool) {
        return _hasRole[role][account];
    }

    /**
     * @notice 授予角色（仅管理员可调用）
     * @param role 角色标识
     * @param account 被授予角色的地址
     */
    function grantRole(bytes32 role, address account) external onlyAdmin nonZeroAddress(account) {
        if (!_hasRole[ROLE_DEFAULT_ADMIN][msg.sender]) revert NotRoleAdmin();
        if (_hasRole[role][account]) return; // 已拥有角色则跳过

        _hasRole[role][account] = true;
        _roleMembers[role].push(account);
        emit RoleGranted(role, account, msg.sender);
    }

    /**
     * @notice 撤销角色（仅管理员可调用）
     * @param role 角色标识
     * @param account 被撤销角色的地址
     */
    function revokeRole(bytes32 role, address account) external onlyAdmin nonZeroAddress(account) {
        if (!_hasRole[role][account]) return; // 未拥有角色则跳过

        _hasRole[role][account] = false;

        // 从角色成员列表中移除
        uint256 length = _roleMembers[role].length;
        for (uint256 i = 0; i < length; i++) {
            if (_roleMembers[role][i] == account) {
                _roleMembers[role][i] = _roleMembers[role][length - 1];
                _roleMembers[role].pop();
                break;
            }
        }

        emit RoleRevoked(role, account, msg.sender);
    }

    /**
     * @notice 查询角色成员数量
     * @param role 角色标识
     * @return 该角色的成员数量
     */
    function getRoleMemberCount(bytes32 role) external view returns (uint256) {
        return _roleMembers[role].length;
    }

    /**
     * @notice 按索引查询角色成员
     * @param role 角色标识
     * @param index 成员索引
     * @return 该索引对应的成员地址
     */
    function getRoleMember(bytes32 role, uint256 index) external view returns (address) {
        require(index < _roleMembers[role].length, "Index out of bounds");
        return _roleMembers[role][index];
    }

    // ============================================================
    // 紧急暂停
    // ============================================================

    /**
     * @notice 暂停合约（仅管理员）
     * @dev 暂停后所有转账、铸币、销毁操作将不可用
     */
    function pause() external onlyAdmin whenNotPaused {
        paused = true;
        emit Paused(msg.sender);
    }

    /**
     * @notice 恢复合约（仅管理员）
     */
    function unpause() external onlyAdmin whenPaused {
        paused = false;
        emit Unpaused(msg.sender);
    }

    // ============================================================
    // 管理功能
    // ============================================================

    /**
     * @notice 变更管理员地址
     * @param newAdmin 新管理员地址
     */
    function setAdmin(address newAdmin) external onlyAdmin nonZeroAddress(newAdmin) {
        emit AdminChanged(admin, newAdmin);
        admin = newAdmin;
    }

    /**
     * @notice 设置挖矿合约地址（铸币权限）
     * @param _miningContract 挖矿合约地址
     */
    function setMiningContract(address _miningContract) external onlyAdmin nonZeroAddress(_miningContract) {
        emit MiningContractChanged(miningContract, _miningContract);
        miningContract = _miningContract;
    }

    /**
     * @notice 设置治理合约地址
     * @param _governanceContract 治理合约地址
     */
    function setGovernanceContract(address _governanceContract) external onlyAdmin nonZeroAddress(_governanceContract) {
        emit GovernanceContractChanged(governanceContract, _governanceContract);
        governanceContract = _governanceContract;
    }

    // ============================================================
    // 接收 ETH（安全性）
    // ============================================================

    /// @notice 允许合约接收 ETH
    receive() external payable {}
}
