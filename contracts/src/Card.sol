// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {IERC4906} from "@openzeppelin/contracts/interfaces/IERC4906.sol";
import {IERC165} from "@openzeppelin/contracts/interfaces/IERC165.sol";
import {ERC2981} from "@openzeppelin/contracts/token/common/ERC2981.sol";
import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {Strings} from "@openzeppelin/contracts/utils/Strings.sol";
import {IpfsURI} from "./utils/IpfsURI.sol";

/// @title argus-mint Card
/// @notice A transferable provenance card for one game asset. Minted by anyone holding a
///         voucher signed by a SIGNER_ROLE key (any relayer may submit it). Metadata is served
///         live from the consumer until the record is final, then frozen to an ipfs:// URI.
/// @dev Not upgradeable. Carries no game logic: nothing here is read back to decide outcomes.
contract Card is ERC721, ERC2981, AccessControl, EIP712, IERC4906 {
    using Strings for uint256;

    bytes32 public constant SIGNER_ROLE = keccak256("SIGNER_ROLE");
    bytes32 public constant METADATA_ROLE = keccak256("METADATA_ROLE");
    bytes32 public constant MINT_VOUCHER_TYPEHASH =
        keccak256("MintVoucher(address to,bytes32 assetId,bytes32 contentHash,uint256 price,uint64 deadline)");

    struct MintVoucher {
        address to;
        bytes32 assetId; // consumer's id for the asset; each mints once
        bytes32 contentHash; // keccak256 of canonical {image, provenance} at mint
        uint256 price; // wei; 0 for free mints
        uint64 deadline; // unix seconds; the voucher is valid while block.timestamp <= deadline
    }

    uint256 private _nextTokenId = 1;
    string private _baseTokenURI;

    mapping(bytes32 assetId => uint256 tokenId) public tokenOfAsset;
    mapping(uint256 tokenId => bytes32 assetId) public assetOf;
    mapping(uint256 tokenId => bytes32 contentHash) public contentHashOf;
    mapping(uint256 tokenId => string uri) private _frozenURI;

    event Minted(uint256 indexed tokenId, bytes32 indexed assetId, bytes32 contentHash, address indexed to);
    /// @dev OpenSea's convention for metadata that will never change.
    event PermanentURI(string value, uint256 indexed id);

    error InvalidSigner(address signer);
    error VoucherExpired(uint64 deadline);
    error AssetAlreadyMinted(bytes32 assetId, uint256 tokenId);
    error WrongPrice(uint256 expected, uint256 sent);
    error AlreadyFrozen(uint256 tokenId);
    error NotAnIpfsURI();
    error WithdrawFailed();

    constructor(
        string memory name_,
        string memory symbol_,
        string memory baseURI_,
        address admin,
        address signer,
        address metadataOperator,
        address royaltyReceiver,
        uint96 royaltyBps
    ) ERC721(name_, symbol_) EIP712("argus-mint Card", "1") {
        _baseTokenURI = baseURI_;
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(SIGNER_ROLE, signer);
        _grantRole(METADATA_ROLE, metadataOperator);
        if (royaltyBps > 0) _setDefaultRoyalty(royaltyReceiver, royaltyBps);
    }

    // --- minting ---------------------------------------------------------------------------

    /// @notice Mint the card a voucher authorises. Reverts on a bad signer, an expired voucher,
    ///         an asset that already has a card, or msg.value != price.
    function mint(MintVoucher calldata v, bytes calldata sig) external payable returns (uint256 tokenId) {
        // forge-lint: disable-next-line(block-timestamp) -- deadlines are minutes to days; seconds of drift don't matter
        if (block.timestamp > v.deadline) revert VoucherExpired(v.deadline);
        if (msg.value != v.price) revert WrongPrice(v.price, msg.value);
        uint256 existing = tokenOfAsset[v.assetId];
        if (existing != 0) revert AssetAlreadyMinted(v.assetId, existing);
        address signer = ECDSA.recover(voucherDigest(v), sig);
        if (!hasRole(SIGNER_ROLE, signer)) revert InvalidSigner(signer);

        tokenId = _nextTokenId++;
        tokenOfAsset[v.assetId] = tokenId;
        assetOf[tokenId] = v.assetId;
        contentHashOf[tokenId] = v.contentHash;
        // forge-lint: disable-next-line(reentrancy-events) -- the only prior call is the ecrecover precompile
        emit Minted(tokenId, v.assetId, v.contentHash, v.to);
        _safeMint(v.to, tokenId);
    }

    /// @notice The EIP-712 digest a SIGNER_ROLE key signs for this voucher.
    function voucherDigest(MintVoucher calldata v) public view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(abi.encode(MINT_VOUCHER_TYPEHASH, v.to, v.assetId, v.contentHash, v.price, v.deadline))
        );
    }

    function domainSeparator() external view returns (bytes32) {
        return _domainSeparatorV4();
    }

    /// @notice Number of cards minted so far (token ids run 1..totalMinted()).
    function totalMinted() external view returns (uint256) {
        return _nextTokenId - 1;
    }

    // --- metadata --------------------------------------------------------------------------

    /// @notice baseURI + tokenId while the record is live; the frozen ipfs:// URI after freeze.
    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        _requireOwned(tokenId);
        string memory frozen = _frozenURI[tokenId];
        if (bytes(frozen).length > 0) return frozen;
        return bytes(_baseTokenURI).length > 0 ? string.concat(_baseTokenURI, tokenId.toString()) : "";
    }

    function isFrozen(uint256 tokenId) public view returns (bool) {
        return bytes(_frozenURI[tokenId]).length > 0;
    }

    /// @notice Tell indexers the live metadata changed (ERC-4906).
    function refresh(uint256 tokenId) external onlyRole(METADATA_ROLE) {
        _requireOwned(tokenId);
        if (isFrozen(tokenId)) revert AlreadyFrozen(tokenId);
        emit MetadataUpdate(tokenId);
    }

    /// @notice Pin the final metadata: tokenURI is `ipfsUri` from now on. Once per token.
    function freeze(uint256 tokenId, string calldata ipfsUri) external onlyRole(METADATA_ROLE) {
        _requireOwned(tokenId);
        if (isFrozen(tokenId)) revert AlreadyFrozen(tokenId);
        if (!IpfsURI.isIpfs(ipfsUri)) revert NotAnIpfsURI();
        _frozenURI[tokenId] = ipfsUri;
        emit MetadataUpdate(tokenId);
        emit PermanentURI(ipfsUri, tokenId);
    }

    function setBaseURI(string calldata uri) external onlyRole(DEFAULT_ADMIN_ROLE) {
        _baseTokenURI = uri;
        if (_nextTokenId > 1) emit BatchMetadataUpdate(1, _nextTokenId - 1);
    }

    // --- admin -----------------------------------------------------------------------------

    /// @notice ERC-2981 royalty on resale (bps of the sale price). 0 removes it.
    function setDefaultRoyalty(address receiver, uint96 bps) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (bps == 0) _deleteDefaultRoyalty();
        else _setDefaultRoyalty(receiver, bps);
    }

    /// @notice Send collected mint fees to `to`.
    // forge-lint: disable-next-item(missing-zero-check,arbitrary-send-eth) -- admin-only; the admin picks the payee
    function withdraw(address payable to) external onlyRole(DEFAULT_ADMIN_ROLE) {
        (bool ok,) = to.call{value: address(this).balance}("");
        if (!ok) revert WithdrawFailed();
    }

    // --- internals -------------------------------------------------------------------------

    function supportsInterface(bytes4 interfaceId)
        public
        view
        override(ERC721, ERC2981, AccessControl, IERC165)
        returns (bool)
    {
        return interfaceId == bytes4(0x49064906) || super.supportsInterface(interfaceId);
    }
}
