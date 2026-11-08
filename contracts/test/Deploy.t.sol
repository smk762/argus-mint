// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Deploy} from "../script/Deploy.s.sol";

contract DeployTest is Test {
    Deploy script = new Deploy();

    function test_defaultsFallBackToAdmin() public view {
        address admin = address(0xA11CE);
        Deploy.Config memory c = script.configFromEnv(admin);
        assertEq(c.admin, admin);
        assertEq(c.signer, admin);
        assertEq(c.metadataOperator, admin);
        assertEq(c.minter, admin);
        assertEq(c.committer, admin);
        assertEq(c.royaltyBps, 0);
        assertEq(c.cardName, "argus-mint Card");
    }

    function test_deployAssignsRoles() public {
        Deploy.Config memory c = script.configFromEnv(makeAddr("admin"));
        c.signer = makeAddr("signer");
        c.metadataOperator = makeAddr("metadata");
        c.minter = makeAddr("minter");
        c.committer = makeAddr("committer");
        c.cardName = "Realms Cards";
        Deploy.Deployed memory d = script.deploy(c);
        assertTrue(d.card.hasRole(d.card.DEFAULT_ADMIN_ROLE(), c.admin));
        assertTrue(d.card.hasRole(d.card.SIGNER_ROLE(), c.signer));
        assertTrue(d.card.hasRole(d.card.METADATA_ROLE(), c.metadataOperator));
        assertTrue(d.deed.hasRole(d.deed.MINTER_ROLE(), c.minter));
        assertTrue(d.ledger.hasRole(d.ledger.COMMITTER_ROLE(), c.committer));
        assertFalse(d.card.hasRole(d.card.SIGNER_ROLE(), c.admin));
        assertEq(d.card.name(), "Realms Cards");
    }

    function test_runBroadcasts() public {
        Deploy.Deployed memory d = script.run();
        assertGt(address(d.card).code.length, 0);
        assertGt(address(d.deed).code.length, 0);
        assertGt(address(d.ledger).code.length, 0);
    }
}
