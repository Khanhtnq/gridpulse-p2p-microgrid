// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "./TestBase.sol";
import "../src/StableSettlementToken.sol";
import "../src/KilowattCreditToken.sol";
import "../src/SolarMicrogridEscrow.sol";

contract SolarMicrogridTest is TestBase {
    StableSettlementToken settlementToken;
    SolarMicrogridEscrow escrow;
    KilowattCreditToken creditToken;

    address constant OWNER = address(0xAA11);
    address constant ORACLE = address(0xD00D);
    address constant ALICE_PROSUMER = address(0xA11CE);
    address constant BOB_BUYER = address(0xB0B);
    address constant CHARLIE_STRANGER = address(0xCAFE);
    address constant RESERVE_VAULT = address(0x0420);

    bytes32 constant METER_ACCRA_01 = keccak256("GH-ACCRA-METER-001");
    bytes32 constant METER_KUMASI_02 = keccak256("GH-KUMASI-METER-002");

    uint256 constant INITIAL_MAX_TARIFF = 2_500_000; // 2.50 cGHS / kWh (6 decimals)
    uint256 constant INITIAL_FEE_BPS = 200;          // 2.00% (200 bps)

    function setUp() external {
        vm.startPrank(OWNER);
        settlementToken = new StableSettlementToken("Cedis Grid Settlement", "cGHS");
        escrow = new SolarMicrogridEscrow(
            address(settlementToken),
            ORACLE,
            INITIAL_MAX_TARIFF,
            INITIAL_FEE_BPS
        );
        creditToken = escrow.creditToken();
        vm.stopPrank();

        // Fund Bob with 1,000 cGHS (1,000 * 1e6)
        settlementToken.mint(BOB_BUYER, 1_000_000_000);
    }

    // -------------------------------------------------------------
    // 1. Meter Registration Tests
    // -------------------------------------------------------------

    function test_MeterRegistration_Success() external {
        vm.prank(ORACLE);
        escrow.registerMeter(ALICE_PROSUMER, METER_ACCRA_01);

        (address prosumer, uint256 lastWh, uint256 nonce, bool reg) = escrow.meters(METER_ACCRA_01);
        assertEq(prosumer, ALICE_PROSUMER);
        assertEq(lastWh, 0);
        assertEq(nonce, 0);
        assertTrue(reg);
    }

    function test_MeterRegistration_RevertAlreadyRegistered() external {
        vm.prank(ORACLE);
        escrow.registerMeter(ALICE_PROSUMER, METER_ACCRA_01);

        vm.prank(ORACLE);
        vm.expectRevert();
        escrow.registerMeter(ALICE_PROSUMER, METER_ACCRA_01);
    }

    function test_MeterRegistration_RevertUnauthorized() external {
        vm.prank(CHARLIE_STRANGER);
        vm.expectRevert();
        escrow.registerMeter(ALICE_PROSUMER, METER_ACCRA_01);
    }

    // -------------------------------------------------------------
    // 2. Monotonic Attestation & Nonce Tests
    // -------------------------------------------------------------

    function test_AttestSolarSurplus_Success() external {
        vm.prank(ORACLE);
        escrow.registerMeter(ALICE_PROSUMER, METER_ACCRA_01);

        // First attestation: 5,000 Wh (5 kWh) at nonce 1
        vm.prank(ORACLE);
        escrow.attestSolarSurplus(METER_ACCRA_01, 5000, 1);

        assertEq(creditToken.balanceOf(ALICE_PROSUMER), 5000 * 1e15);

        // Second attestation: cumulative 12,000 Wh (delta 7,000 Wh) at nonce 2
        vm.prank(ORACLE);
        escrow.attestSolarSurplus(METER_ACCRA_01, 12000, 2);

        assertEq(creditToken.balanceOf(ALICE_PROSUMER), 12000 * 1e15);
    }

    function test_AttestSolarSurplus_RevertNonMonotonic() external {
        vm.prank(ORACLE);
        escrow.registerMeter(ALICE_PROSUMER, METER_ACCRA_01);

        vm.prank(ORACLE);
        escrow.attestSolarSurplus(METER_ACCRA_01, 5000, 1);

        // Replay same or lower cumulative reading
        vm.prank(ORACLE);
        vm.expectRevert();
        escrow.attestSolarSurplus(METER_ACCRA_01, 5000, 2);

        vm.prank(ORACLE);
        vm.expectRevert();
        escrow.attestSolarSurplus(METER_ACCRA_01, 4999, 2);
    }

    function test_AttestSolarSurplus_RevertInvalidNonce() external {
        vm.prank(ORACLE);
        escrow.registerMeter(ALICE_PROSUMER, METER_ACCRA_01);

        // Nonce skip: nonce 2 instead of 1
        vm.prank(ORACLE);
        vm.expectRevert();
        escrow.attestSolarSurplus(METER_ACCRA_01, 5000, 2);

        // Valid nonce 1
        vm.prank(ORACLE);
        escrow.attestSolarSurplus(METER_ACCRA_01, 5000, 1);

        // Replay nonce 1
        vm.prank(ORACLE);
        vm.expectRevert();
        escrow.attestSolarSurplus(METER_ACCRA_01, 6000, 1);
    }

    function test_AttestSolarSurplus_RevertUnregisteredMeter() external {
        vm.prank(ORACLE);
        vm.expectRevert();
        escrow.attestSolarSurplus(METER_KUMASI_02, 1000, 1);
    }

    // -------------------------------------------------------------
    // 3. P2P Solar Sell Offer Creation & Tariff Caps
    // -------------------------------------------------------------

    function test_CreateSellOffer_Success() external {
        _mintAliceCredits(10000); // 10,000 Wh

        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);
        uint256 offerId = escrow.createSellOffer(4000, 1_800_000); // 4 kWh at 1.80 cGHS / kWh
        vm.stopPrank();

        assertEq(offerId, 0);
        assertEq(creditToken.balanceOf(address(escrow)), 4000 * 1e15);
        assertEq(creditToken.balanceOf(ALICE_PROSUMER), 6000 * 1e15);

        (address seller, uint256 whAmt, uint256 remWh, uint256 price, bool act) = escrow.offers(offerId);
        assertEq(seller, ALICE_PROSUMER);
        assertEq(whAmt, 4000);
        assertEq(remWh, 4000);
        assertEq(price, 1_800_000);
        assertTrue(act);
    }

    function test_CreateSellOffer_RevertTariffExceedsCap() external {
        _mintAliceCredits(5000);

        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);

        // INITIAL_MAX_TARIFF is 2_500_000. Try 2_500_001
        vm.expectRevert();
        escrow.createSellOffer(1000, INITIAL_MAX_TARIFF + 1);
        vm.stopPrank();
    }

    function test_CreateSellOffer_RevertZeroAmountOrPrice() external {
        _mintAliceCredits(5000);

        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);

        vm.expectRevert();
        escrow.createSellOffer(0, 1_000_000);

        vm.expectRevert();
        escrow.createSellOffer(1000, 0);
        vm.stopPrank();
    }

    // -------------------------------------------------------------
    // 4. Energy Settlement & Partial Fills
    // -------------------------------------------------------------

    function test_PurchaseEnergy_FullFill() external {
        _mintAliceCredits(5000);

        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);
        uint256 offerId = escrow.createSellOffer(3000, 1_500_000); // 3,000 Wh at 1.50 cGHS/kWh = 4.50 cGHS
        vm.stopPrank();

        // 3000 * 1_500_000 / 1000 = 4_500_000 units (4.50 cGHS)
        // Fee = 2% of 4.50 = 0.09 cGHS = 90,000 units
        // Alice proceeds = 4_410_000 units
        uint256 bobBalBefore = settlementToken.balanceOf(BOB_BUYER);
        uint256 aliceBalBefore = settlementToken.balanceOf(ALICE_PROSUMER);

        vm.startPrank(BOB_BUYER);
        settlementToken.approve(address(escrow), type(uint256).max);
        escrow.purchaseEnergy(offerId, 3000);
        vm.stopPrank();

        assertEq(creditToken.balanceOf(BOB_BUYER), 3000 * 1e15);
        assertEq(settlementToken.balanceOf(BOB_BUYER), bobBalBefore - 4_500_000);
        assertEq(settlementToken.balanceOf(ALICE_PROSUMER), aliceBalBefore + 4_410_000);
        assertEq(escrow.accumulatedFees(), 90_000);

        (, , uint256 remWh, , bool act) = escrow.offers(offerId);
        assertEq(remWh, 0);
        assertFalse(act);
    }

    function test_PurchaseEnergy_PartialFill() external {
        _mintAliceCredits(10000);

        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);
        uint256 offerId = escrow.createSellOffer(8000, 2_000_000); // 8,000 Wh at 2.00 cGHS/kWh
        vm.stopPrank();

        vm.startPrank(BOB_BUYER);
        settlementToken.approve(address(escrow), type(uint256).max);
        // Buy 3,500 Wh
        escrow.purchaseEnergy(offerId, 3500);
        vm.stopPrank();

        assertEq(creditToken.balanceOf(BOB_BUYER), 3500 * 1e15);
        (, , uint256 remWh, , bool act) = escrow.offers(offerId);
        assertEq(remWh, 4500);
        assertTrue(act);

        // Buy remaining 4,500 Wh
        vm.startPrank(BOB_BUYER);
        escrow.purchaseEnergy(offerId, 4500);
        vm.stopPrank();

        assertEq(creditToken.balanceOf(BOB_BUYER), 8000 * 1e15);
        (, , uint256 remWhFinal, , bool actFinal) = escrow.offers(offerId);
        assertEq(remWhFinal, 0);
        assertFalse(actFinal);
    }

    function test_PurchaseEnergy_DustCeilingDivision() external {
        _mintAliceCredits(1000);

        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);
        // 1 Wh at 1_000_000 (1.00 cGHS / kWh = 0.001 cGHS / Wh)
        uint256 offerId = escrow.createSellOffer(100, 1_000_000);
        vm.stopPrank();

        // 1 Wh: cost = (1 * 1_000_000 + 999) / 1000 = 1000 units (0.001000 cGHS)
        // Without ceiling division (+999), 1 * 500 / 1000 would truncate to 0.
        // With ceiling division (+999), cost is strictly non-zero!
        vm.startPrank(BOB_BUYER);
        settlementToken.approve(address(escrow), type(uint256).max);
        escrow.purchaseEnergy(offerId, 1);
        vm.stopPrank();

        assertEq(creditToken.balanceOf(BOB_BUYER), 1 * 1e15);
    }

    function test_PurchaseEnergy_RevertInsufficientLiquidity() external {
        _mintAliceCredits(2000);

        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);
        uint256 offerId = escrow.createSellOffer(1000, 1_000_000);
        vm.stopPrank();

        vm.startPrank(BOB_BUYER);
        settlementToken.approve(address(escrow), type(uint256).max);
        vm.expectRevert();
        escrow.purchaseEnergy(offerId, 1001);
        vm.stopPrank();
    }

    // -------------------------------------------------------------
    // 5. Cancel Offer & Credit Conservation
    // -------------------------------------------------------------

    function test_CancelOffer_AccountingConservation() external {
        _mintAliceCredits(5000);

        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);
        uint256 offerId = escrow.createSellOffer(5000, 1_200_000);
        vm.stopPrank();

        // Partial fill 2,000 Wh
        vm.startPrank(BOB_BUYER);
        settlementToken.approve(address(escrow), type(uint256).max);
        escrow.purchaseEnergy(offerId, 2000);
        vm.stopPrank();

        // Alice cancels remaining 3,000 Wh
        vm.prank(ALICE_PROSUMER);
        escrow.cancelOffer(offerId);

        assertEq(creditToken.balanceOf(ALICE_PROSUMER), 3000 * 1e15);
        assertEq(creditToken.balanceOf(BOB_BUYER), 2000 * 1e15);
        assertEq(creditToken.balanceOf(address(escrow)), 0);

        // Cannot buy cancelled offer
        vm.startPrank(BOB_BUYER);
        vm.expectRevert();
        escrow.purchaseEnergy(offerId, 100);
        vm.stopPrank();
    }

    function test_CancelOffer_RevertUnauthorized() external {
        _mintAliceCredits(3000);

        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);
        uint256 offerId = escrow.createSellOffer(3000, 1_200_000);
        vm.stopPrank();

        vm.prank(CHARLIE_STRANGER);
        vm.expectRevert();
        escrow.cancelOffer(offerId);
    }

    // -------------------------------------------------------------
    // 6. Grid Island Pause & Governance Controls
    // -------------------------------------------------------------

    function test_GridIslandPause_RevertsTrade() external {
        _mintAliceCredits(3000);

        vm.prank(OWNER);
        escrow.setGridIslandPause(true);

        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);
        vm.expectRevert();
        escrow.createSellOffer(1000, 1_000_000);
        vm.stopPrank();

        // Unpause and verify resume
        vm.prank(OWNER);
        escrow.setGridIslandPause(false);

        vm.startPrank(ALICE_PROSUMER);
        uint256 offerId = escrow.createSellOffer(1000, 1_000_000);
        vm.stopPrank();
        assertEq(offerId, 0);
    }

    function test_Governance_FeeCapAndWithdraw() external {
        vm.startPrank(OWNER);
        // Fee bps > 500 must revert
        vm.expectRevert();
        escrow.setProtocolFeeBps(501);

        // Fee bps <= 500 succeeds
        escrow.setProtocolFeeBps(350);
        assertEq(escrow.protocolFeeBps(), 350);
        vm.stopPrank();

        // Execute a trade to collect fees
        _mintAliceCredits(2000);
        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);
        uint256 offerId = escrow.createSellOffer(2000, 1_000_000); // 2 kWh at 1.00 = 2.00 cGHS (2_000_000 units)
        vm.stopPrank();

        vm.startPrank(BOB_BUYER);
        settlementToken.approve(address(escrow), type(uint256).max);
        escrow.purchaseEnergy(offerId, 2000);
        vm.stopPrank();

        // 3.5% fee on 2_000_000 = 70_000 units
        assertEq(escrow.accumulatedFees(), 70_000);

        // Withdraw fees to Reserve Vault
        vm.prank(OWNER);
        escrow.withdrawReserveFees(RESERVE_VAULT);

        assertEq(settlementToken.balanceOf(RESERVE_VAULT), 70_000);
        assertEq(escrow.accumulatedFees(), 0);
    }

    // -------------------------------------------------------------
    // 7. Fuzz Test: Conservation of Energy & Currency Invariants
    // -------------------------------------------------------------

    function testFuzz_EscrowConservation(
        uint96 whGenerated,
        uint96 whSold,
        uint64 pricePerKwh
    ) external {
        // Bound inputs to realistic microgrid ranges
        vm.assume(whGenerated >= 10 && whGenerated <= 500_000); // 10 Wh to 500 kWh
        vm.assume(whSold >= 1 && whSold <= whGenerated);
        vm.assume(pricePerKwh >= 100_000 && pricePerKwh <= INITIAL_MAX_TARIFF);

        // Attest solar surplus
        vm.prank(ORACLE);
        escrow.registerMeter(ALICE_PROSUMER, keccak256(abi.encodePacked("FUZZ_METER", whGenerated)));

        vm.prank(ORACLE);
        escrow.attestSolarSurplus(keccak256(abi.encodePacked("FUZZ_METER", whGenerated)), whGenerated, 1);

        // Prosumer offers all generated Wh
        vm.startPrank(ALICE_PROSUMER);
        creditToken.approve(address(escrow), type(uint256).max);
        uint256 offerId = escrow.createSellOffer(whGenerated, pricePerKwh);
        vm.stopPrank();

        // Buyer purchases whSold
        uint256 expectedCost = (uint256(whSold) * pricePerKwh + 999) / 1000;
        settlementToken.mint(BOB_BUYER, expectedCost);

        vm.startPrank(BOB_BUYER);
        settlementToken.approve(address(escrow), type(uint256).max);
        escrow.purchaseEnergy(offerId, whSold);
        vm.stopPrank();

        // Prosumer cancels remainder
        vm.prank(ALICE_PROSUMER);
        escrow.cancelOffer(offerId);

        // INVARIANT 1: Total Kilowatt Credit Tokens Conserved
        assertEq(
            creditToken.balanceOf(ALICE_PROSUMER) + creditToken.balanceOf(BOB_BUYER),
            uint256(whGenerated) * 1e15
        );

        // INVARIANT 2: Escrow has zero trapped credits
        assertEq(creditToken.balanceOf(address(escrow)), 0);

        // INVARIANT 3: Stablecoin settlement matches exact ceiling cost
        uint256 expectedFee = (expectedCost * INITIAL_FEE_BPS) / 10000;
        uint256 expectedProceeds = expectedCost - expectedFee;

        assertEq(settlementToken.balanceOf(ALICE_PROSUMER), expectedProceeds);
        assertEq(escrow.accumulatedFees(), expectedFee);
    }

    // -------------------------------------------------------------
    // Internal Helpers
    // -------------------------------------------------------------

    function _mintAliceCredits(uint256 wh) internal {
        vm.prank(ORACLE);
        escrow.registerMeter(ALICE_PROSUMER, METER_ACCRA_01);

        vm.prank(ORACLE);
        escrow.attestSolarSurplus(METER_ACCRA_01, wh, 1);
    }
}
