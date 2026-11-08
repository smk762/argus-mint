// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {IERC4906} from "@openzeppelin/contracts/interfaces/IERC4906.sol";
import {IERC2981} from "@openzeppelin/contracts/interfaces/IERC2981.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {IERC721Errors} from "@openzeppelin/contracts/interfaces/draft-IERC6093.sol";
import {Card} from "../src/Card.sol";

contract CardTest is Test {
    Card card;
    address admin = makeAddr("admin");
    address metadataOp = makeAddr("metadataOp");
    address royalty = makeAddr("royalty");
    address relayer = makeAddr("relayer");
    address player = makeAddr("player");
    address signer;
    uint256 signerKey;

    event Minted(uint256 indexed tokenId, bytes32 indexed assetId, bytes32 contentHash, address indexed to);
    event PermanentURI(string value, uint256 indexed id);

    function setUp() public {
        (signer, signerKey) = makeAddrAndKey("signer");
        card = new Card("Test Cards", "TCARD", "https://api.example/cards/", admin, signer, metadataOp, royalty, 250);
        vm.deal(relayer, 100 ether);
        vm.deal(player, 100 ether);
        vm.warp(1_800_000_000);
    }

    // --- helpers ---------------------------------------------------------------------------

    function _voucher(bytes32 assetId, uint256 price) internal view returns (Card.MintVoucher memory) {
        return Card.MintVoucher({
            to: player,
            assetId: assetId,
            contentHash: keccak256(abi.encode("content", assetId)),
            price: price,
            deadline: uint64(block.timestamp + 1 hours)
        });
    }

    function _sign(Card.MintVoucher memory v, uint256 key) internal view returns (bytes memory) {
        (uint8 sv, bytes32 r, bytes32 s) = vm.sign(key, card.voucherDigest(v));
        return abi.encodePacked(r, s, sv);
    }

    function _mint(bytes32 assetId) internal returns (uint256) {
        Card.MintVoucher memory v = _voucher(assetId, 0);
        return card.mint(v, _sign(v, signerKey));
    }

    // --- minting ---------------------------------------------------------------------------

    function test_mintFree() public {
        Card.MintVoucher memory v = _voucher(keccak256("asset-1"), 0);
        bytes memory sig = _sign(v, signerKey);
        vm.expectEmit(address(card));
        emit Minted(1, v.assetId, v.contentHash, player);
        vm.prank(relayer); // anyone may submit a voucher
        uint256 id = card.mint(v, sig);
        assertEq(id, 1);
        assertEq(card.ownerOf(1), player);
        assertEq(card.tokenOfAsset(v.assetId), 1);
        assertEq(card.assetOf(1), v.assetId);
        assertEq(card.contentHashOf(1), v.contentHash);
        assertEq(card.totalMinted(), 1);
        assertEq(card.tokenURI(1), "https://api.example/cards/1");
    }

    function test_mintPricedAndWithdraw() public {
        Card.MintVoucher memory v = _voucher(keccak256("asset-1"), 0.01 ether);
        bytes memory sig = _sign(v, signerKey);
        vm.prank(player);
        card.mint{value: 0.01 ether}(v, sig);
        assertEq(address(card).balance, 0.01 ether);

        address payable treasury = payable(makeAddr("treasury"));
        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector, player, card.DEFAULT_ADMIN_ROLE()
            )
        );
        vm.prank(player);
        card.withdraw(treasury);
        vm.prank(admin);
        card.withdraw(treasury);
        assertEq(treasury.balance, 0.01 ether);
        assertEq(address(card).balance, 0);
    }

    function test_withdrawToRejectingContractReverts() public {
        Card.MintVoucher memory v = _voucher(keccak256("asset-1"), 1 ether);
        bytes memory sig = _sign(v, signerKey);
        vm.prank(player);
        card.mint{value: 1 ether}(v, sig);
        address payable rejecter = payable(address(new RejectsEth()));
        vm.expectRevert(Card.WithdrawFailed.selector);
        vm.prank(admin);
        card.withdraw(rejecter);
    }

    function test_revertBadSigner() public {
        (address mallory, uint256 malloryKey) = makeAddrAndKey("mallory");
        Card.MintVoucher memory v = _voucher(keccak256("asset-1"), 0);
        bytes memory sig = _sign(v, malloryKey);
        vm.expectRevert(abi.encodeWithSelector(Card.InvalidSigner.selector, mallory));
        card.mint(v, sig);
    }

    function test_revertExpired() public {
        Card.MintVoucher memory v = _voucher(keccak256("asset-1"), 0);
        bytes memory sig = _sign(v, signerKey);
        vm.warp(v.deadline + 1);
        vm.expectRevert(abi.encodeWithSelector(Card.VoucherExpired.selector, v.deadline));
        card.mint(v, sig);
    }

    function test_mintAtDeadlineIsAllowed() public {
        Card.MintVoucher memory v = _voucher(keccak256("asset-1"), 0);
        bytes memory sig = _sign(v, signerKey);
        vm.warp(v.deadline);
        card.mint(v, sig);
    }

    function test_revertAssetAlreadyMinted() public {
        Card.MintVoucher memory v = _voucher(keccak256("asset-1"), 0);
        bytes memory sig = _sign(v, signerKey);
        card.mint(v, sig);
        // a fresh voucher for the same asset is refused too
        Card.MintVoucher memory again = _voucher(keccak256("asset-1"), 0);
        again.deadline += 1;
        bytes memory sig2 = _sign(again, signerKey);
        vm.expectRevert(abi.encodeWithSelector(Card.AssetAlreadyMinted.selector, v.assetId, 1));
        card.mint(again, sig2);
    }

    function test_revertWrongPrice() public {
        Card.MintVoucher memory v = _voucher(keccak256("asset-1"), 1 ether);
        bytes memory sig = _sign(v, signerKey);
        vm.startPrank(player);
        vm.expectRevert(abi.encodeWithSelector(Card.WrongPrice.selector, 1 ether, 0.5 ether));
        card.mint{value: 0.5 ether}(v, sig);
        vm.expectRevert(abi.encodeWithSelector(Card.WrongPrice.selector, 1 ether, 2 ether));
        card.mint{value: 2 ether}(v, sig);
        vm.stopPrank();
    }

    function test_revertMalformedSignature() public {
        Card.MintVoucher memory v = _voucher(keccak256("asset-1"), 0);
        vm.expectRevert(); // ECDSAInvalidSignatureLength
        card.mint(v, hex"1234");
    }

    function test_revertHighSSignature() public {
        Card.MintVoucher memory v = _voucher(keccak256("asset-1"), 0);
        (uint8 sv, bytes32 r, bytes32 s) = vm.sign(signerKey, card.voucherDigest(v));
        uint256 n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;
        bytes memory malleated = abi.encodePacked(r, bytes32(n - uint256(s)), sv == 27 ? uint8(28) : uint8(27));
        vm.expectRevert(); // ECDSAInvalidSignatureS
        card.mint(v, malleated);
    }

    function test_domainSeparator() public view {
        bytes32 expected = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256("argus-mint Card"),
                keccak256("1"),
                block.chainid,
                address(card)
            )
        );
        assertEq(card.domainSeparator(), expected);
    }

    // --- metadata --------------------------------------------------------------------------

    function test_refresh() public {
        _mint(keccak256("a"));
        vm.expectEmit(address(card));
        emit IERC4906.MetadataUpdate(1);
        vm.prank(metadataOp);
        card.refresh(1);
    }

    function test_refreshRequiresRole() public {
        _mint(keccak256("a"));
        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector, player, card.METADATA_ROLE()
            )
        );
        vm.prank(player);
        card.refresh(1);
    }

    function test_refreshUnknownToken() public {
        vm.expectRevert(abi.encodeWithSelector(IERC721Errors.ERC721NonexistentToken.selector, 7));
        vm.prank(metadataOp);
        card.refresh(7);
    }

    function test_freeze() public {
        _mint(keccak256("a"));
        vm.expectEmit(address(card));
        emit IERC4906.MetadataUpdate(1);
        vm.expectEmit(address(card));
        emit PermanentURI("ipfs://bafyfinal", 1);
        vm.prank(metadataOp);
        card.freeze(1, "ipfs://bafyfinal");
        assertTrue(card.isFrozen(1));
        assertEq(card.tokenURI(1), "ipfs://bafyfinal");

        // a later base URI change doesn't touch a frozen token
        vm.prank(admin);
        card.setBaseURI("https://other.example/");
        assertEq(card.tokenURI(1), "ipfs://bafyfinal");
    }

    function test_freezeOnce() public {
        _mint(keccak256("a"));
        vm.startPrank(metadataOp);
        card.freeze(1, "ipfs://bafyfinal");
        vm.expectRevert(abi.encodeWithSelector(Card.AlreadyFrozen.selector, 1));
        card.freeze(1, "ipfs://bafyother");
        vm.expectRevert(abi.encodeWithSelector(Card.AlreadyFrozen.selector, 1));
        card.refresh(1);
        vm.stopPrank();
    }

    function test_freezeRequiresIpfs() public {
        _mint(keccak256("a"));
        vm.startPrank(metadataOp);
        vm.expectRevert(Card.NotAnIpfsURI.selector);
        card.freeze(1, "https://example.org/1.json");
        vm.expectRevert(Card.NotAnIpfsURI.selector);
        card.freeze(1, "ipfs://");
        vm.stopPrank();
    }

    function test_freezeRequiresRole() public {
        _mint(keccak256("a"));
        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector, player, card.METADATA_ROLE()
            )
        );
        vm.prank(player);
        card.freeze(1, "ipfs://bafyfinal");
    }

    function test_setBaseURI() public {
        _mint(keccak256("a"));
        _mint(keccak256("b"));
        vm.expectEmit(address(card));
        emit IERC4906.BatchMetadataUpdate(1, 2);
        vm.prank(admin);
        card.setBaseURI("https://new.example/");
        assertEq(card.tokenURI(2), "https://new.example/2");

        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector, metadataOp, card.DEFAULT_ADMIN_ROLE()
            )
        );
        vm.prank(metadataOp);
        card.setBaseURI("https://evil.example/");
    }

    function test_emptyBaseURI() public {
        vm.prank(admin);
        card.setBaseURI("");
        _mint(keccak256("a"));
        assertEq(card.tokenURI(1), "");
    }

    // --- royalties, interfaces, transfers --------------------------------------------------

    function test_royalties() public {
        (address receiver, uint256 amount) = card.royaltyInfo(1, 10_000);
        assertEq(receiver, royalty);
        assertEq(amount, 250);
        vm.prank(admin);
        card.setDefaultRoyalty(address(0), 0);
        (receiver, amount) = card.royaltyInfo(1, 10_000);
        assertEq(amount, 0);
    }

    function test_noRoyaltyByDefault() public {
        Card c = new Card("X", "X", "", admin, signer, metadataOp, address(0), 0);
        (, uint256 amount) = c.royaltyInfo(1, 10_000);
        assertEq(amount, 0);
    }

    function test_supportsInterface() public view {
        assertTrue(card.supportsInterface(type(IERC721).interfaceId));
        assertTrue(card.supportsInterface(type(IERC2981).interfaceId));
        assertTrue(card.supportsInterface(bytes4(0x49064906)));
        assertTrue(card.supportsInterface(type(IAccessControl).interfaceId));
        assertFalse(card.supportsInterface(0xffffffff));
    }

    function test_cardsAreTransferable() public {
        _mint(keccak256("a"));
        address buyer = makeAddr("buyer");
        vm.prank(player);
        card.transferFrom(player, buyer, 1);
        assertEq(card.ownerOf(1), buyer);
    }

    // --- fuzz ------------------------------------------------------------------------------

    function testFuzz_mintAnyVoucher(address to, bytes32 assetId, bytes32 contentHash, uint96 price, uint64 deadline)
        public
    {
        vm.assume(to != address(0) && to.code.length == 0);
        assumeNotPrecompile(to);
        deadline = uint64(bound(deadline, block.timestamp, type(uint64).max));
        Card.MintVoucher memory v = Card.MintVoucher(to, assetId, contentHash, price, deadline);
        bytes memory sig = _sign(v, signerKey);
        vm.deal(relayer, price);
        vm.prank(relayer);
        uint256 id = card.mint{value: price}(v, sig);
        assertEq(card.ownerOf(id), to);
        assertEq(card.tokenOfAsset(assetId), id);
        assertEq(card.contentHashOf(id), contentHash);
    }

    function testFuzz_tamperedVoucherRejected(uint8 field, bytes32 noise) public {
        vm.assume(noise != bytes32(0));
        Card.MintVoucher memory v = _voucher(keccak256("asset"), 1 ether);
        bytes memory sig = _sign(v, signerKey);
        field = field % 5;
        if (field == 0) v.to = address(uint160(v.to) ^ uint160(uint256(noise)));
        if (field == 1) v.assetId ^= noise;
        if (field == 2) v.contentHash ^= noise;
        if (field == 3) v.price ^= uint256(noise);
        if (field == 4) v.deadline = uint64(block.timestamp + 1 + uint256(noise) % 1e9);
        vm.assume(field != 0 || (uint160(uint256(noise)) != 0 && v.to != address(0) && v.to.code.length == 0));
        vm.assume(field != 4 || v.deadline != uint64(block.timestamp + 1 hours));
        vm.deal(relayer, v.price);
        vm.prank(relayer);
        vm.expectRevert(); // InvalidSigner(recovered) — the recovered address is not predictable
        card.mint{value: v.price}(v, sig);
    }

    function testFuzz_expiredRejected(uint64 deadline) public {
        deadline = uint64(bound(deadline, 0, block.timestamp - 1));
        Card.MintVoucher memory v = _voucher(keccak256("asset"), 0);
        v.deadline = deadline;
        bytes memory sig = _sign(v, signerKey);
        vm.expectRevert(abi.encodeWithSelector(Card.VoucherExpired.selector, deadline));
        card.mint(v, sig);
    }

    // --- Python-built fixture --------------------------------------------------------------

    /// A voucher signed by argus_mint.vouchers (tests/fixtures/vouchers.json) mints here.
    function test_pythonSignedVouchersMint() public {
        string memory json = vm.readFile(string.concat(vm.projectRoot(), "/../tests/fixtures/vouchers.json"));
        vm.chainId(vm.parseJsonUint(json, ".chainId"));
        address cardAt = vm.parseJsonAddress(json, ".card");
        address pySigner = vm.parseJsonAddress(json, ".signer");
        deployCodeTo(
            "Card.sol:Card",
            abi.encode("Fixture", "FIX", "", admin, pySigner, metadataOp, address(0), uint96(0)),
            cardAt
        );
        Card c = Card(cardAt);

        for (uint256 i = 0; i < 2; i++) {
            string memory p = string.concat(".vouchers[", vm.toString(i), "]");
            Card.MintVoucher memory v = Card.MintVoucher({
                to: vm.parseJsonAddress(json, string.concat(p, ".to")),
                assetId: vm.parseJsonBytes32(json, string.concat(p, ".assetId")),
                contentHash: vm.parseJsonBytes32(json, string.concat(p, ".contentHash")),
                price: vm.parseUint(vm.parseJsonString(json, string.concat(p, ".price"))),
                deadline: uint64(vm.parseJsonUint(json, string.concat(p, ".deadline")))
            });
            assertEq(c.voucherDigest(v), vm.parseJsonBytes32(json, string.concat(p, ".digest")), "digest");
            bytes memory sig = vm.parseJsonBytes(json, string.concat(p, ".signature"));
            vm.deal(relayer, v.price);
            vm.prank(relayer);
            uint256 id = c.mint{value: v.price}(v, sig);
            assertEq(c.ownerOf(id), v.to);
            assertEq(c.contentHashOf(id), v.contentHash);
        }
    }
}

contract RejectsEth {
    receive() external payable {
        revert("no");
    }
}
