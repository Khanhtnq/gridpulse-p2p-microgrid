// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title KilowattCreditToken
/// @notice 18-decimal ERC-20 token representing verified solar energy credits.
/// 1.000 token (1e18) = 1.000 kWh.
/// 1 Wh = 1e15 units.
/// Mintable and burnable strictly by authorized SolarMicrogridEscrow.
contract KilowattCreditToken {
    string public constant name = "Kilowatt Solar Credit";
    string public constant symbol = "kWHc";
    uint8 public constant decimals = 18;
    uint256 public totalSupply;

    address public immutable escrow;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    error OnlyEscrowAllowed();
    error InsufficientBalance();
    error InsufficientAllowance();
    error ZeroAddress();

    modifier onlyEscrow() {
        if (msg.sender != escrow) revert OnlyEscrowAllowed();
        _;
    }

    constructor(address _escrow) {
        if (_escrow == address(0)) revert ZeroAddress();
        escrow = _escrow;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        if (to == address(0)) revert ZeroAddress();
        if (balanceOf[msg.sender] < amount) revert InsufficientBalance();

        unchecked {
            balanceOf[msg.sender] -= amount;
            balanceOf[to] += amount;
        }

        emit Transfer(msg.sender, to, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        if (spender == address(0)) revert ZeroAddress();
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        if (to == address(0)) revert ZeroAddress();
        uint256 currentAllowance = allowance[from][msg.sender];
        if (currentAllowance != type(uint256).max) {
            if (currentAllowance < amount) revert InsufficientAllowance();
            unchecked {
                allowance[from][msg.sender] = currentAllowance - amount;
            }
        }

        if (balanceOf[from] < amount) revert InsufficientBalance();
        unchecked {
            balanceOf[from] -= amount;
            balanceOf[to] += amount;
        }

        emit Transfer(from, to, amount);
        return true;
    }

    /// @notice Mint energy credits strictly invoked by SolarMicrogridEscrow upon smart meter telemetry attestation
    function mint(address to, uint256 amount) external onlyEscrow {
        if (to == address(0)) revert ZeroAddress();
        totalSupply += amount;
        unchecked {
            balanceOf[to] += amount;
        }
        emit Transfer(address(0), to, amount);
    }

    /// @notice Burn energy credits strictly invoked by SolarMicrogridEscrow on energy redemption
    function burn(address from, uint256 amount) external onlyEscrow {
        if (balanceOf[from] < amount) revert InsufficientBalance();
        unchecked {
            balanceOf[from] -= amount;
            totalSupply -= amount;
        }
        emit Transfer(from, address(0), amount);
    }
}
