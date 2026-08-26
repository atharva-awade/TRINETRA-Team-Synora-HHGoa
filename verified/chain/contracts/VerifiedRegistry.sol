// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title VerifiedRegistry
/// @notice Tamper-evident registry of face-verified social-media matches.
///
/// Each record anchors a canonical *evidence bundle* produced by the pipeline:
///   recordHash      keccak256 of the canonical (sorted, compact) JSON bundle
///   faceCommitment  HMAC-SHA256 commitment of the quantised query-face template
///                   (the biometric itself is never published)
///   contentHash     sha256 of the matched post image bytes at anchoring time
///   merkleRoot      root over every bundle field so single fields can be
///                   proven / disclosed selectively without revealing the rest
///   evidenceCID     IPFS CID of the full bundle (optional)
///
/// Anyone can later re-derive the hashes from the bundle (or from the live
/// post) and compare them with what is stored here.
contract VerifiedRegistry {
    struct Record {
        bytes32 recordHash;
        bytes32 faceCommitment;
        bytes32 contentHash;
        bytes32 merkleRoot;
        string uri;
        string platform;
        string evidenceCID;
        uint16 similarityBps;
        uint64 timestamp;
        address submitter;
    }

    Record[] private _records;
    mapping(bytes32 => uint256) private _idPlusOneByRecordHash;
    mapping(bytes32 => uint256[]) private _idsByCommitment;
    mapping(bytes32 => uint256[]) private _idsByContent;

    event Anchored(
        uint256 indexed id,
        bytes32 indexed recordHash,
        bytes32 indexed faceCommitment,
        bytes32 contentHash,
        bytes32 merkleRoot,
        string uri,
        string platform,
        string evidenceCID,
        uint16 similarityBps,
        address submitter,
        uint64 timestamp
    );

    error AlreadyAnchored(uint256 id);
    error UnknownRecord();

    /// @notice Anchor a new evidence bundle. Reverts if the exact bundle was anchored before.
    function anchor(
        bytes32 recordHash,
        bytes32 faceCommitment,
        bytes32 contentHash,
        bytes32 merkleRoot,
        string calldata uri,
        string calldata platform,
        string calldata evidenceCID,
        uint16 similarityBps
    ) external returns (uint256 id) {
        uint256 existing = _idPlusOneByRecordHash[recordHash];
        if (existing != 0) revert AlreadyAnchored(existing - 1);
        id = _records.length;
        _records.push(
            Record({
                recordHash: recordHash,
                faceCommitment: faceCommitment,
                contentHash: contentHash,
                merkleRoot: merkleRoot,
                uri: uri,
                platform: platform,
                evidenceCID: evidenceCID,
                similarityBps: similarityBps,
                timestamp: uint64(block.timestamp),
                submitter: msg.sender
            })
        );
        _idPlusOneByRecordHash[recordHash] = id + 1;
        _idsByCommitment[faceCommitment].push(id);
        _idsByContent[contentHash].push(id);
        emit Anchored(id, recordHash, faceCommitment, contentHash, merkleRoot, uri, platform, evidenceCID, similarityBps, msg.sender, uint64(block.timestamp));
    }

    function count() external view returns (uint256) {
        return _records.length;
    }

    function get(uint256 id) external view returns (Record memory) {
        if (id >= _records.length) revert UnknownRecord();
        return _records[id];
    }

    /// @notice Look a bundle up by its hash. `exists == false` means it was never anchored (or was altered).
    function verify(bytes32 recordHash) external view returns (bool exists, uint256 id, uint64 timestamp, address submitter) {
        uint256 p = _idPlusOneByRecordHash[recordHash];
        if (p == 0) return (false, 0, 0, address(0));
        Record storage r = _records[p - 1];
        return (true, p - 1, r.timestamp, r.submitter);
    }

    /// @notice All anchors produced from the same face template commitment.
    function recordsByCommitment(bytes32 faceCommitment) external view returns (uint256[] memory) {
        return _idsByCommitment[faceCommitment];
    }

    /// @notice All anchors that reference the same post image.
    function recordsByContent(bytes32 contentHash) external view returns (uint256[] memory) {
        return _idsByContent[contentHash];
    }

    /// @notice On-chain Merkle proof check for a single evidence leaf (sorted-pair hashing).
    /// @dev The caller MUST derive `leaf` itself as keccak256(0x00 ++ "<dotted.key>=<canonical json value>")
    ///      - the contract cannot tell a leaf pre-image from an internal node, so a verifier that
    ///      accepts a leaf value from an untrusted party (instead of hashing the field itself)
    ///      proves nothing. `verified verify` always recomputes the leaf locally.
    function verifyLeaf(uint256 id, bytes32 leaf, bytes32[] calldata proof) external view returns (bool) {
        if (id >= _records.length) revert UnknownRecord();
        bytes32 h = leaf;
        for (uint256 i = 0; i < proof.length; i++) {
            bytes32 p = proof[i];
            h = h < p ? keccak256(abi.encodePacked(h, p)) : keccak256(abi.encodePacked(p, h));
        }
        return h == _records[id].merkleRoot;
    }
}
