// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {MerkleProof} from "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";

/// @title argus-mint Ledger
/// @notice A public notary for commit-reveal secrets and Merkle roots, keyed by an epoch the
///         consumer chooses (atlas-realms: days since 1970-01-01). Nothing can be overwritten:
///         a second commit or anchor for an epoch reverts.
/// @dev Commitments are sha256(secret), matching what atlas-realms already publishes. Revealed
///      secrets live in the Revealed event, not in storage. Merkle leaves follow OpenZeppelin's
///      StandardMerkleTree with leaf encoding ["bytes32"]. Not upgradeable.
contract Ledger is AccessControl {
    bytes32 public constant COMMITTER_ROLE = keccak256("COMMITTER_ROLE");

    struct Commitment {
        bytes32 hash;
        uint64 committedAt;
        bool revealed;
    }

    struct Root {
        bytes32 root;
        uint32 count;
        uint64 anchoredAt;
    }

    mapping(uint64 epoch => Commitment) private _commitments;
    mapping(uint64 epoch => Root) private _roots;

    event Committed(uint64 indexed epoch, bytes32 commitment);
    event Revealed(uint64 indexed epoch, bytes secret);
    event Anchored(uint64 indexed epoch, bytes32 root, uint32 count);

    error AlreadyCommitted(uint64 epoch);
    error NotCommitted(uint64 epoch);
    error AlreadyRevealed(uint64 epoch);
    error WrongSecret(uint64 epoch);
    error AlreadyAnchored(uint64 epoch);
    error EmptyValue();

    constructor(address admin, address committer) {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(COMMITTER_ROLE, committer);
    }

    /// @notice Publish sha256(secret) for `epoch`. Once per epoch.
    function commit(uint64 epoch, bytes32 commitment_) external onlyRole(COMMITTER_ROLE) {
        if (commitment_ == bytes32(0)) revert EmptyValue();
        if (_commitments[epoch].hash != bytes32(0)) revert AlreadyCommitted(epoch);
        // forge-lint: disable-next-line(unsafe-typecast) -- uint64 seconds outlasts the chain
        _commitments[epoch] = Commitment(commitment_, uint64(block.timestamp), false);
        emit Committed(epoch, commitment_);
    }

    /// @notice Reveal the secret behind `epoch`'s commitment. Anyone may call it, once.
    function reveal(uint64 epoch, bytes calldata secret) external {
        Commitment storage c = _commitments[epoch];
        if (c.hash == bytes32(0)) revert NotCommitted(epoch);
        if (c.revealed) revert AlreadyRevealed(epoch);
        if (sha256(secret) != c.hash) revert WrongSecret(epoch);
        c.revealed = true;
        emit Revealed(epoch, secret);
    }

    /// @notice Publish the Merkle root over `count` records for `epoch`. Once per epoch.
    function anchor(uint64 epoch, bytes32 root_, uint32 count) external onlyRole(COMMITTER_ROLE) {
        if (root_ == bytes32(0) || count == 0) revert EmptyValue();
        if (_roots[epoch].root != bytes32(0)) revert AlreadyAnchored(epoch);
        // forge-lint: disable-next-line(unsafe-typecast) -- uint64 seconds outlasts the chain
        _roots[epoch] = Root(root_, count, uint64(block.timestamp));
        emit Anchored(epoch, root_, count);
    }

    /// @notice True when `proof` shows `value` is a leaf of `epoch`'s anchored tree.
    function verify(uint64 epoch, bytes32 value, bytes32[] calldata proof) external view returns (bool) {
        bytes32 r = _roots[epoch].root;
        if (r == bytes32(0)) return false;
        bytes32 leaf = keccak256(bytes.concat(keccak256(abi.encode(value))));
        return MerkleProof.verifyCalldata(proof, r, leaf);
    }

    function commitment(uint64 epoch) external view returns (bytes32 hash, uint64 committedAt) {
        Commitment storage c = _commitments[epoch];
        return (c.hash, c.committedAt);
    }

    function isRevealed(uint64 epoch) external view returns (bool) {
        return _commitments[epoch].revealed;
    }

    function root(uint64 epoch) external view returns (bytes32 root_, uint32 count, uint64 anchoredAt) {
        Root storage r = _roots[epoch];
        return (r.root, r.count, r.anchoredAt);
    }
}
