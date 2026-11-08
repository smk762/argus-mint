// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @dev Frozen and deed URIs must be content-addressed: "ipfs://" followed by something.
library IpfsURI {
    function isIpfs(string calldata uri) internal pure returns (bool) {
        bytes calldata b = bytes(uri);
        // forge-lint: disable-next-line(unsafe-typecast) -- a 7-byte literal into bytes7 is exact
        return b.length > 7 && bytes7(b[:7]) == bytes7("ipfs://");
    }
}
