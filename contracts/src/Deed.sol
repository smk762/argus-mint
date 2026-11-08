// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {IERC5192} from "./interfaces/IERC5192.sol";
import {IpfsURI} from "./utils/IpfsURI.sol";

/// @title argus-mint Deed
/// @notice A soulbound (ERC-5192) record of a feat, minted by the consumer's server. It can't be
///         transferred or approved; the holder may burn their own deed.
/// @dev Each recordId mints at most once, ever (a burned deed's record stays used), so a retried
///      mint can't issue a duplicate. Not upgradeable.
contract Deed is ERC721, AccessControl, IERC5192 {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");

    uint256 private _nextTokenId = 1;

    mapping(uint256 tokenId => string uri) private _uris;
    mapping(uint256 tokenId => bytes32 recordId) public recordOf;
    mapping(bytes32 recordId => uint256 tokenId) public tokenOfRecord;

    event DeedMinted(uint256 indexed tokenId, bytes32 indexed recordId, address indexed to);

    error Soulbound();
    error NotHolder();
    error RecordAlreadyMinted(bytes32 recordId, uint256 tokenId);
    error NotAnIpfsURI();

    constructor(string memory name_, string memory symbol_, address admin, address minter) ERC721(name_, symbol_) {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(MINTER_ROLE, minter);
    }

    function mint(address to, bytes32 recordId, string calldata ipfsUri)
        external
        onlyRole(MINTER_ROLE)
        returns (uint256 tokenId)
    {
        uint256 existing = tokenOfRecord[recordId];
        if (existing != 0) revert RecordAlreadyMinted(recordId, existing);
        if (!IpfsURI.isIpfs(ipfsUri)) revert NotAnIpfsURI();

        tokenId = _nextTokenId++;
        _uris[tokenId] = ipfsUri;
        recordOf[tokenId] = recordId;
        tokenOfRecord[recordId] = tokenId;
        emit DeedMinted(tokenId, recordId, to);
        emit Locked(tokenId);
        _safeMint(to, tokenId);
    }

    /// @notice The holder removes their own deed.
    function burn(uint256 tokenId) external {
        if (_requireOwned(tokenId) != msg.sender) revert NotHolder();
        delete _uris[tokenId];
        _burn(tokenId);
    }

    /// @notice Always true for an existing deed (ERC-5192).
    function locked(uint256 tokenId) external view returns (bool) {
        _requireOwned(tokenId);
        return true;
    }

    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        _requireOwned(tokenId);
        return _uris[tokenId];
    }

    function totalMinted() external view returns (uint256) {
        return _nextTokenId - 1;
    }

    // --- soulbound: no transfers, no approvals ----------------------------------------------

    function _update(address to, uint256 tokenId, address auth) internal override returns (address) {
        address from = _ownerOf(tokenId);
        if (from != address(0) && to != address(0)) revert Soulbound();
        return super._update(to, tokenId, auth);
    }

    function approve(address, uint256) public pure override {
        revert Soulbound();
    }

    function setApprovalForAll(address, bool) public pure override {
        revert Soulbound();
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC721, AccessControl) returns (bool) {
        return interfaceId == type(IERC5192).interfaceId || super.supportsInterface(interfaceId);
    }
}
