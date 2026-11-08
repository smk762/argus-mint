// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {IERC721Errors} from "@openzeppelin/contracts/interfaces/draft-IERC6093.sol";
import {Deed} from "../src/Deed.sol";
import {IERC5192} from "../src/interfaces/IERC5192.sol";

contract DeedTest is Test {
    Deed deed;
    address admin = makeAddr("admin");
    address minter = makeAddr("minter");
    address holder = makeAddr("holder");
    address other = makeAddr("other");
    bytes32 constant RECORD = keccak256("first to take Wellington");

    event Locked(uint256 tokenId);

    function setUp() public {
        deed = new Deed("Test Deeds", "TDEED", admin, minter);
    }

    function _mint() internal returns (uint256) {
        vm.prank(minter);
        return deed.mint(holder, RECORD, "ipfs://bafydeed");
    }

    function test_mint() public {
        vm.expectEmit(address(deed));
        emit Locked(1);
        uint256 id = _mint();
        assertEq(id, 1);
        assertEq(deed.ownerOf(1), holder);
        assertTrue(deed.locked(1));
        assertEq(deed.tokenURI(1), "ipfs://bafydeed");
        assertEq(deed.recordOf(1), RECORD);
        assertEq(deed.tokenOfRecord(RECORD), 1);
        assertEq(deed.totalMinted(), 1);
    }

    function test_mintRequiresRole() public {
        vm.expectRevert(
            abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, other, deed.MINTER_ROLE())
        );
        vm.prank(other);
        deed.mint(other, RECORD, "ipfs://bafydeed");
    }

    function test_recordMintsOnce() public {
        _mint();
        vm.expectRevert(abi.encodeWithSelector(Deed.RecordAlreadyMinted.selector, RECORD, 1));
        vm.prank(minter);
        deed.mint(other, RECORD, "ipfs://bafydeed2");
    }

    function test_requiresIpfsURI() public {
        vm.expectRevert(Deed.NotAnIpfsURI.selector);
        vm.prank(minter);
        deed.mint(holder, RECORD, "https://example.org/deed.json");
    }

    function test_transfersRevert() public {
        _mint();
        vm.startPrank(holder);
        vm.expectRevert(Deed.Soulbound.selector);
        deed.transferFrom(holder, other, 1);
        vm.expectRevert(Deed.Soulbound.selector);
        deed.safeTransferFrom(holder, other, 1);
        vm.expectRevert(Deed.Soulbound.selector);
        deed.safeTransferFrom(holder, other, 1, "");
        vm.expectRevert(Deed.Soulbound.selector);
        deed.approve(other, 1);
        vm.expectRevert(Deed.Soulbound.selector);
        deed.setApprovalForAll(other, true);
        vm.stopPrank();
        assertEq(deed.ownerOf(1), holder);
    }

    function test_holderBurns() public {
        _mint();
        vm.prank(holder);
        deed.burn(1);
        assertEq(deed.balanceOf(holder), 0);
        vm.expectRevert(abi.encodeWithSelector(IERC721Errors.ERC721NonexistentToken.selector, 1));
        deed.locked(1);
        // a burned deed's record stays used
        vm.expectRevert(abi.encodeWithSelector(Deed.RecordAlreadyMinted.selector, RECORD, 1));
        vm.prank(minter);
        deed.mint(holder, RECORD, "ipfs://bafydeed");
    }

    function test_onlyHolderBurns() public {
        _mint();
        vm.expectRevert(Deed.NotHolder.selector);
        vm.prank(minter);
        deed.burn(1);
    }

    function test_supportsInterface() public view {
        assertTrue(deed.supportsInterface(0xb45a3c0e));
        assertTrue(deed.supportsInterface(type(IERC5192).interfaceId));
        assertTrue(deed.supportsInterface(type(IERC721).interfaceId));
        assertTrue(deed.supportsInterface(type(IAccessControl).interfaceId));
    }

    function testFuzz_noTransferAnywhere(address to) public {
        vm.assume(to != address(0));
        _mint();
        vm.expectRevert(Deed.Soulbound.selector);
        vm.prank(holder);
        deed.transferFrom(holder, to, 1);
    }
}
