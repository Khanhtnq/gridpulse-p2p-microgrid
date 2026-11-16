// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface Vm {
    function prank(address) external;
    function startPrank(address) external;
    function stopPrank() external;
    function expectRevert() external;
    function expectRevert(bytes calldata) external;
    function assume(bool) external;
}

abstract contract TestBase {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function assertTrue(bool condition) internal pure {
        if (!condition) revert("ASSERT_TRUE_FAILED");
    }

    function assertFalse(bool condition) internal pure {
        if (condition) revert("ASSERT_FALSE_FAILED");
    }

    function assertEq(uint256 a, uint256 b) internal pure {
        if (a != b) revert("ASSERT_EQ_UINT_FAILED");
    }

    function assertEq(address a, address b) internal pure {
        if (a != b) revert("ASSERT_EQ_ADDR_FAILED");
    }

    function assertEq(bytes32 a, bytes32 b) internal pure {
        if (a != b) revert("ASSERT_EQ_BYTES32_FAILED");
    }

    function assertEq(bool a, bool b) internal pure {
        if (a != b) revert("ASSERT_EQ_BOOL_FAILED");
    }

    function assertGe(uint256 a, uint256 b) internal pure {
        if (a < b) revert("ASSERT_GE_FAILED");
    }

    function assertLe(uint256 a, uint256 b) internal pure {
        if (a > b) revert("ASSERT_LE_FAILED");
    }
}
