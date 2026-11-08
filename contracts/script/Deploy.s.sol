// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {Card} from "../src/Card.sol";
import {Deed} from "../src/Deed.sol";
import {Ledger} from "../src/Ledger.sol";

/// @notice Deploy Card, Deed and Ledger for one consumer.
/// @dev Configure with env vars; every role defaults to ADMIN (the broadcaster if unset).
///      forge script script/Deploy.s.sol --rpc-url $RPC_URL --broadcast --private-key $DEPLOYER_KEY
///
///      ADMIN, SIGNER, METADATA_OPERATOR, MINTER, COMMITTER   addresses
///      CARD_NAME, CARD_SYMBOL, CARD_BASE_URI                  Card metadata (base URI ends with "/")
///      DEED_NAME, DEED_SYMBOL                                 Deed metadata
///      ROYALTY_RECEIVER, ROYALTY_BPS                          ERC-2981 (default 0: no royalty)
contract Deploy is Script {
    struct Config {
        address admin;
        address signer;
        address metadataOperator;
        address minter;
        address committer;
        string cardName;
        string cardSymbol;
        string cardBaseURI;
        string deedName;
        string deedSymbol;
        address royaltyReceiver;
        uint96 royaltyBps;
    }

    struct Deployed {
        Card card;
        Deed deed;
        Ledger ledger;
    }

    function run() external returns (Deployed memory d) {
        vm.startBroadcast();
        (, address broadcaster,) = vm.readCallers();
        d = deploy(configFromEnv(broadcaster));
        vm.stopBroadcast();
        console.log("Card  ", address(d.card));
        console.log("Deed  ", address(d.deed));
        console.log("Ledger", address(d.ledger));
    }

    function configFromEnv(address fallbackAdmin) public view returns (Config memory c) {
        c.admin = vm.envOr("ADMIN", fallbackAdmin);
        c.signer = vm.envOr("SIGNER", c.admin);
        c.metadataOperator = vm.envOr("METADATA_OPERATOR", c.admin);
        c.minter = vm.envOr("MINTER", c.admin);
        c.committer = vm.envOr("COMMITTER", c.admin);
        c.cardName = vm.envOr("CARD_NAME", string("argus-mint Card"));
        c.cardSymbol = vm.envOr("CARD_SYMBOL", string("CARD"));
        c.cardBaseURI = vm.envOr("CARD_BASE_URI", string(""));
        c.deedName = vm.envOr("DEED_NAME", string("argus-mint Deed"));
        c.deedSymbol = vm.envOr("DEED_SYMBOL", string("DEED"));
        c.royaltyReceiver = vm.envOr("ROYALTY_RECEIVER", c.admin);
        c.royaltyBps = uint96(vm.envOr("ROYALTY_BPS", uint256(0)));
    }

    function deploy(Config memory c) public returns (Deployed memory d) {
        d.card = new Card(
            c.cardName,
            c.cardSymbol,
            c.cardBaseURI,
            c.admin,
            c.signer,
            c.metadataOperator,
            c.royaltyReceiver,
            c.royaltyBps
        );
        d.deed = new Deed(c.deedName, c.deedSymbol, c.admin, c.minter);
        d.ledger = new Ledger(c.admin, c.committer);
    }
}
