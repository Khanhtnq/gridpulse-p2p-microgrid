// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "./StableSettlementToken.sol";
import "./KilowattCreditToken.sol";

/// @title SolarMicrogridEscrow
/// @notice Decentralized P2P Solar Energy Trading & Smart Meter Escrow Protocol.
/// Supports Ghana's intermittent grid ("Dumsor") mitigation via decentralized solar microgrid settlement.
contract SolarMicrogridEscrow {
    // -------------------------------------------------------------
    // Storage & State Variables
    // -------------------------------------------------------------

    address public owner;
    address public feederOracle;

    StableSettlementToken public immutable settlementToken;
    KilowattCreditToken public immutable creditToken;

    uint256 public maxTariffPerKwh; // in 6 decimals (e.g. 2_500_000 == 2.50 cGHS / kWh)
    uint256 public protocolFeeBps;  // basis points, e.g. 200 == 2%, max 500 (5%)
    uint256 public accumulatedFees;
    bool public gridIslandPaused;

    uint256 private _reentrancyStatus;
    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;

    struct Meter {
        address prosumer;
        uint256 lastCumulativeWh;
        uint256 lastNonce;
        bool registered;
    }

    struct SellOffer {
        address prosumer;
        uint256 whAmount;
        uint256 remainingWh;
        uint256 pricePerKwh; // 6-decimal stablecoin per 1000 Wh (1 kWh)
        bool active;
    }

    mapping(bytes32 => Meter) public meters;
    mapping(uint256 => SellOffer) public offers;
    uint256 public nextOfferId;

    // -------------------------------------------------------------
    // Events
    // -------------------------------------------------------------

    event MeterRegistered(bytes32 indexed meterId, address indexed prosumer);
    event SolarSurplusAttested(
        bytes32 indexed meterId,
        address indexed prosumer,
        uint256 deltaWh,
        uint256 cumulativeWh,
        uint256 nonce
    );
    event OfferCreated(
        uint256 indexed offerId,
        address indexed prosumer,
        uint256 whAmount,
        uint256 pricePerKwh
    );
    event EnergySettled(
        uint256 indexed offerId,
        address indexed seller,
        address indexed buyer,
        uint256 whPurchased,
        uint256 cost,
        uint256 fee
    );
    event OfferCancelled(uint256 indexed offerId, uint256 refundWh);
    event MaxTariffUpdated(uint256 oldTariff, uint256 newTariff);
    event ProtocolFeeUpdated(uint256 oldFeeBps, uint256 newFeeBps);
    event GridIslandPauseToggled(bool isPaused);
    event FeederOracleUpdated(address indexed oldOracle, address indexed newOracle);
    event ReserveFeesWithdrawn(address indexed recipient, uint256 amount);

    // -------------------------------------------------------------
    // Custom Errors
    // -------------------------------------------------------------

    error UnauthorizedOwner();
    error UnauthorizedOracle();
    error UnauthorizedProsumer();
    error GridIslandPaused();
    error MeterAlreadyRegistered(bytes32 meterId);
    error MeterNotRegistered(bytes32 meterId);
    error InvalidNonce(uint256 provided, uint256 expected);
    error NonMonotonicCumulativeWh(uint256 provided, uint256 current);
    error TariffExceedsCap(uint256 pricePerKwh, uint256 maxTariff);
    error ProtocolFeeTooHigh(uint256 feeBps, uint256 maxAllowed);
    error ZeroWhAmount();
    error ZeroPrice();
    error ZeroAddress();
    error OfferNotActive(uint256 offerId);
    error InsufficientOfferLiquidity(uint256 requestedWh, uint256 remainingWh);
    error TransferFailed();
    error ReentrancyGuardReentrantCall();

    // -------------------------------------------------------------
    // Modifiers
    // -------------------------------------------------------------

    modifier onlyOwner() {
        if (msg.sender != owner) revert UnauthorizedOwner();
        _;
    }

    modifier onlyOracleOrOwner() {
        if (msg.sender != feederOracle && msg.sender != owner) revert UnauthorizedOracle();
        _;
    }

    modifier whenNotPaused() {
        if (gridIslandPaused) revert GridIslandPaused();
        _;
    }

    modifier nonReentrant() {
        if (_reentrancyStatus == _ENTERED) revert ReentrancyGuardReentrantCall();
        _reentrancyStatus = _ENTERED;
        _;
        _reentrancyStatus = _NOT_ENTERED;
    }

    // -------------------------------------------------------------
    // Constructor
    // -------------------------------------------------------------

    constructor(
        address _settlementToken,
        address _feederOracle,
        uint256 _maxTariffPerKwh,
        uint256 _protocolFeeBps
    ) {
        if (_settlementToken == address(0)) revert ZeroAddress();
        if (_protocolFeeBps > 500) revert ProtocolFeeTooHigh(_protocolFeeBps, 500);

        owner = msg.sender;
        feederOracle = _feederOracle == address(0) ? msg.sender : _feederOracle;
        settlementToken = StableSettlementToken(_settlementToken);
        creditToken = new KilowattCreditToken(address(this));

        maxTariffPerKwh = _maxTariffPerKwh;
        protocolFeeBps = _protocolFeeBps;
        _reentrancyStatus = _NOT_ENTERED;
    }

    // -------------------------------------------------------------
    // Smart-Meter Registry & Attestation
    // -------------------------------------------------------------

    function registerMeter(address prosumer, bytes32 meterId) external onlyOracleOrOwner {
        if (prosumer == address(0)) revert ZeroAddress();
        if (meters[meterId].registered) revert MeterAlreadyRegistered(meterId);

        meters[meterId] = Meter({
            prosumer: prosumer,
            lastCumulativeWh: 0,
            lastNonce: 0,
            registered: true
        });

        emit MeterRegistered(meterId, prosumer);
    }

    function attestSolarSurplus(
        bytes32 meterId,
        uint256 cumulativeWh,
        uint256 nonce
    ) external onlyOracleOrOwner {
        Meter storage meter = meters[meterId];
        if (!meter.registered) revert MeterNotRegistered(meterId);

        if (nonce != meter.lastNonce + 1) {
            revert InvalidNonce(nonce, meter.lastNonce + 1);
        }
        if (cumulativeWh <= meter.lastCumulativeWh) {
            revert NonMonotonicCumulativeWh(cumulativeWh, meter.lastCumulativeWh);
        }

        uint256 deltaWh = cumulativeWh - meter.lastCumulativeWh;
        meter.lastCumulativeWh = cumulativeWh;
        meter.lastNonce = nonce;

        // 1 Wh == 1e15 token units (18 decimals, 1 token = 1000 Wh = 1 kWh)
        uint256 tokensToMint = deltaWh * 1e15;
        creditToken.mint(meter.prosumer, tokensToMint);

        emit SolarSurplusAttested(meterId, meter.prosumer, deltaWh, cumulativeWh, nonce);
    }

    // -------------------------------------------------------------
    // P2P Solar Energy Trading & Settlement
    // -------------------------------------------------------------

    function createSellOffer(
        uint256 whAmount,
        uint256 pricePerKwh
    ) external whenNotPaused nonReentrant returns (uint256 offerId) {
        if (whAmount == 0) revert ZeroWhAmount();
        if (pricePerKwh == 0) revert ZeroPrice();
        if (pricePerKwh > maxTariffPerKwh) revert TariffExceedsCap(pricePerKwh, maxTariffPerKwh);

        // Escrow the energy credits from prosumer into escrow
        uint256 tokenAmount = whAmount * 1e15;
        bool ok = creditToken.transferFrom(msg.sender, address(this), tokenAmount);
        if (!ok) revert TransferFailed();

        offerId = nextOfferId++;
        offers[offerId] = SellOffer({
            prosumer: msg.sender,
            whAmount: whAmount,
            remainingWh: whAmount,
            pricePerKwh: pricePerKwh,
            active: true
        });

        emit OfferCreated(offerId, msg.sender, whAmount, pricePerKwh);
    }

    function purchaseEnergy(
        uint256 offerId,
        uint256 whToBuy
    ) external whenNotPaused nonReentrant {
        SellOffer storage offer = offers[offerId];
        if (!offer.active) revert OfferNotActive(offerId);
        if (whToBuy == 0) revert ZeroWhAmount();
        if (whToBuy > offer.remainingWh) {
            revert InsufficientOfferLiquidity(whToBuy, offer.remainingWh);
        }

        // Exact ceiling division in 6-decimal stablecoin:
        // cost = ceil(whToBuy * pricePerKwh / 1000)
        uint256 cost = (whToBuy * offer.pricePerKwh + 999) / 1000;
        uint256 fee = (cost * protocolFeeBps) / 10000;
        uint256 prosumerProceeds = cost - fee;

        // Checks-Effects-Interactions (CEI)
        offer.remainingWh -= whToBuy;
        if (offer.remainingWh == 0) {
            offer.active = false;
        }
        accumulatedFees += fee;

        // Verify net ERC-20 stablecoin delta
        uint256 balBefore = settlementToken.balanceOf(address(this));
        bool s1 = settlementToken.transferFrom(msg.sender, address(this), cost);
        if (!s1 || settlementToken.balanceOf(address(this)) != balBefore + cost) {
            revert TransferFailed();
        }

        if (prosumerProceeds > 0) {
            bool s2 = settlementToken.transfer(offer.prosumer, prosumerProceeds);
            if (!s2) revert TransferFailed();
        }

        // Deliver energy credits to the buyer
        bool s3 = creditToken.transfer(msg.sender, whToBuy * 1e15);
        if (!s3) revert TransferFailed();

        emit EnergySettled(offerId, offer.prosumer, msg.sender, whToBuy, cost, fee);
    }

    function cancelOffer(uint256 offerId) external nonReentrant {
        SellOffer storage offer = offers[offerId];
        if (offer.prosumer != msg.sender) revert UnauthorizedProsumer();
        if (!offer.active) revert OfferNotActive(offerId);

        uint256 refundWh = offer.remainingWh;
        offer.remainingWh = 0;
        offer.active = false;

        if (refundWh > 0) {
            bool ok = creditToken.transfer(msg.sender, refundWh * 1e15);
            if (!ok) revert TransferFailed();
        }

        emit OfferCancelled(offerId, refundWh);
    }

    // -------------------------------------------------------------
    // Governance & Grid Controls
    // -------------------------------------------------------------

    function setMaxTariffPerKwh(uint256 newMaxTariff) external onlyOwner {
        if (newMaxTariff == 0) revert ZeroPrice();
        uint256 old = maxTariffPerKwh;
        maxTariffPerKwh = newMaxTariff;
        emit MaxTariffUpdated(old, newMaxTariff);
    }

    function setProtocolFeeBps(uint256 newFeeBps) external onlyOwner {
        if (newFeeBps > 500) revert ProtocolFeeTooHigh(newFeeBps, 500);
        uint256 old = protocolFeeBps;
        protocolFeeBps = newFeeBps;
        emit ProtocolFeeUpdated(old, newFeeBps);
    }

    function setGridIslandPause(bool paused) external onlyOwner {
        gridIslandPaused = paused;
        emit GridIslandPauseToggled(paused);
    }

    function setFeederOracle(address newOracle) external onlyOwner {
        if (newOracle == address(0)) revert ZeroAddress();
        address old = feederOracle;
        feederOracle = newOracle;
        emit FeederOracleUpdated(old, newOracle);
    }

    function withdrawReserveFees(address recipient) external onlyOwner nonReentrant {
        if (recipient == address(0)) revert ZeroAddress();
        uint256 amount = accumulatedFees;
        accumulatedFees = 0;

        bool ok = settlementToken.transfer(recipient, amount);
        if (!ok) revert TransferFailed();

        emit ReserveFeesWithdrawn(recipient, amount);
    }
}
