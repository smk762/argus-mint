// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title ERC-5192: Minimal Soulbound NFTs
/// @dev Interface id 0xb45a3c0e.
interface IERC5192 {
    /// @notice Emitted when the locking status is changed to locked.
    event Locked(uint256 tokenId);

    /// @notice Emitted when the locking status is changed to unlocked.
    event Unlocked(uint256 tokenId);

    /// @notice Returns the locking status of a soulbound token.
    function locked(uint256 tokenId) external view returns (bool);
}
