// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {Ledger} from "../src/Ledger.sol";

contract LedgerTest is Test {
    Ledger ledger;
    address admin = makeAddr("admin");
    address committer = makeAddr("committer");
    address anyone = makeAddr("anyone");
    uint64 constant EPOCH = 20_400; // a game day: days since 1970-01-01

    event Committed(uint64 indexed epoch, bytes32 commitment);
    event Revealed(uint64 indexed epoch, bytes secret);
    event Anchored(uint64 indexed epoch, bytes32 root, uint32 count);

    function setUp() public {
        ledger = new Ledger(admin, committer);
        vm.warp(1_762_560_000);
    }

    function _fixture(string memory name) internal view returns (string memory) {
        return vm.readFile(string.concat(vm.projectRoot(), "/../tests/fixtures/", name));
    }

    // --- commit / reveal -------------------------------------------------------------------

    function test_commitAndReveal() public {
        // atlas-realms' daily secret: 32 random bytes, committed as sha256(secret)
        bytes memory secret = abi.encodePacked(keccak256("daily secret"));
        bytes32 c = sha256(secret);
        vm.expectEmit(address(ledger));
        emit Committed(EPOCH, c);
        vm.prank(committer);
        ledger.commit(EPOCH, c);
        (bytes32 hash, uint64 stamped) = ledger.commitment(EPOCH);
        assertEq(hash, c);
        assertEq(stamped, block.timestamp);
        assertFalse(ledger.isRevealed(EPOCH));

        vm.expectEmit(address(ledger));
        emit Revealed(EPOCH, secret);
        vm.prank(anyone);
        ledger.reveal(EPOCH, secret);
        assertTrue(ledger.isRevealed(EPOCH));
    }

    // NB: sha256(...) is a precompile call, so it would consume a pending vm.prank or
    // vm.expectRevert; hashes are computed before the cheatcodes throughout this file.

    function test_secondCommitReverts() public {
        (bytes32 a, bytes32 b) = (sha256("a"), sha256("b"));
        vm.startPrank(committer);
        ledger.commit(EPOCH, a);
        vm.expectRevert(abi.encodeWithSelector(Ledger.AlreadyCommitted.selector, EPOCH));
        ledger.commit(EPOCH, b);
        vm.stopPrank();
    }

    function test_commitRequiresRole() public {
        bytes32 c = sha256("a");
        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector, anyone, ledger.COMMITTER_ROLE()
            )
        );
        vm.prank(anyone);
        ledger.commit(EPOCH, c);
    }

    function test_zeroCommitmentReverts() public {
        vm.expectRevert(Ledger.EmptyValue.selector);
        vm.prank(committer);
        ledger.commit(EPOCH, bytes32(0));
    }

    function test_revealRejectsWrongSecret() public {
        bytes32 c = sha256("right");
        vm.prank(committer);
        ledger.commit(EPOCH, c);
        vm.expectRevert(abi.encodeWithSelector(Ledger.WrongSecret.selector, EPOCH));
        ledger.reveal(EPOCH, "wrong");
    }

    function test_revealKeccakOfSecretRejected() public {
        // the commitment is sha256, not keccak: a keccak preimage must not pass
        bytes memory secret = "s";
        vm.prank(committer);
        ledger.commit(EPOCH, keccak256(secret));
        vm.expectRevert(abi.encodeWithSelector(Ledger.WrongSecret.selector, EPOCH));
        ledger.reveal(EPOCH, secret);
    }

    function test_revealUncommittedReverts() public {
        vm.expectRevert(abi.encodeWithSelector(Ledger.NotCommitted.selector, EPOCH));
        ledger.reveal(EPOCH, "x");
    }

    function test_revealOnce() public {
        bytes32 c = sha256("s");
        vm.prank(committer);
        ledger.commit(EPOCH, c);
        ledger.reveal(EPOCH, "s");
        vm.expectRevert(abi.encodeWithSelector(Ledger.AlreadyRevealed.selector, EPOCH));
        ledger.reveal(EPOCH, "s");
    }

    function testFuzz_reveal(bytes memory secret, bytes memory guess) public {
        bytes32 c = sha256(secret);
        vm.prank(committer);
        ledger.commit(EPOCH, c);
        if (keccak256(guess) != keccak256(secret)) {
            vm.expectRevert(abi.encodeWithSelector(Ledger.WrongSecret.selector, EPOCH));
            ledger.reveal(EPOCH, guess);
        }
        ledger.reveal(EPOCH, secret);
        assertTrue(ledger.isRevealed(EPOCH));
    }

    // --- anchor / verify -------------------------------------------------------------------

    function test_anchor() public {
        bytes32 r = keccak256("root");
        vm.expectEmit(address(ledger));
        emit Anchored(EPOCH, r, 12);
        vm.prank(committer);
        ledger.anchor(EPOCH, r, 12);
        (bytes32 root, uint32 count, uint64 stamped) = ledger.root(EPOCH);
        assertEq(root, r);
        assertEq(count, 12);
        assertEq(stamped, block.timestamp);
    }

    function test_secondAnchorReverts() public {
        vm.startPrank(committer);
        ledger.anchor(EPOCH, keccak256("a"), 1);
        vm.expectRevert(abi.encodeWithSelector(Ledger.AlreadyAnchored.selector, EPOCH));
        ledger.anchor(EPOCH, keccak256("b"), 1);
        vm.stopPrank();
    }

    function test_anchorRejectsEmpty() public {
        vm.startPrank(committer);
        vm.expectRevert(Ledger.EmptyValue.selector);
        ledger.anchor(EPOCH, bytes32(0), 1);
        vm.expectRevert(Ledger.EmptyValue.selector);
        ledger.anchor(EPOCH, keccak256("a"), 0);
        vm.stopPrank();
    }

    function test_anchorRequiresRole() public {
        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector, anyone, ledger.COMMITTER_ROLE()
            )
        );
        vm.prank(anyone);
        ledger.anchor(EPOCH, keccak256("a"), 1);
    }

    function test_verifyUnanchoredIsFalse() public view {
        assertFalse(ledger.verify(EPOCH, keccak256("x"), new bytes32[](0)));
    }

    /// Proofs built by argus_mint.merkle (tests/fixtures/proofs.json) verify on-chain.
    function test_pythonBuiltProofsVerify() public {
        string memory json = _fixture("proofs.json");
        for (uint256 c = 0; c < 3; c++) {
            string memory p = string.concat(".cases[", vm.toString(c), "]");
            uint64 epoch = EPOCH + uint64(c);
            uint256 n = vm.parseJsonUint(json, string.concat(p, ".count"));
            bytes32 r = vm.parseJsonBytes32(json, string.concat(p, ".root"));
            bytes32[] memory values = vm.parseJsonBytes32Array(json, string.concat(p, ".values"));
            vm.prank(committer);
            ledger.anchor(epoch, r, uint32(n));
            for (uint256 i = 0; i < n; i++) {
                bytes32[] memory proof =
                    vm.parseJsonBytes32Array(json, string.concat(p, ".proofs[", vm.toString(i), "]"));
                assertTrue(ledger.verify(epoch, values[i], proof), "proof should verify");
                assertFalse(ledger.verify(epoch, values[(i + 1) % n], proof), "wrong value");
                assertFalse(ledger.verify(epoch + 100, values[i], proof), "wrong epoch");
            }
        }
    }

    /// Proofs built by the reference JS library (tests/fixtures/merkle.json) verify on-chain.
    function test_openZeppelinJsProofsVerify() public {
        string memory json = _fixture("merkle.json");
        // case 4 has 1,000 leaves and sampled proofs at these indices
        uint256[6] memory idx = [uint256(0), 1, 2, 500, 998, 999];
        bytes32 r = vm.parseJsonBytes32(json, ".cases[4].root");
        bytes32[] memory values = vm.parseJsonBytes32Array(json, ".cases[4].values");
        vm.prank(committer);
        ledger.anchor(EPOCH, r, 1000);
        for (uint256 k = 0; k < idx.length; k++) {
            bytes32[] memory proof =
                vm.parseJsonBytes32Array(json, string.concat(".cases[4].proofs.", vm.toString(idx[k])));
            assertTrue(ledger.verify(EPOCH, values[idx[k]], proof));
        }
    }

    function testFuzz_tamperedProofFails(uint256 i, uint256 j, bytes32 noise) public {
        vm.assume(noise != bytes32(0));
        string memory json = _fixture("proofs.json");
        bytes32 r = vm.parseJsonBytes32(json, ".cases[2].root");
        bytes32[] memory values = vm.parseJsonBytes32Array(json, ".cases[2].values");
        i = bound(i, 0, values.length - 1);
        bytes32[] memory proof = vm.parseJsonBytes32Array(json, string.concat(".cases[2].proofs[", vm.toString(i), "]"));
        j = bound(j, 0, proof.length - 1);
        vm.prank(committer);
        ledger.anchor(EPOCH, r, uint32(values.length));
        assertTrue(ledger.verify(EPOCH, values[i], proof));
        proof[j] ^= noise;
        assertFalse(ledger.verify(EPOCH, values[i], proof));
    }
}
