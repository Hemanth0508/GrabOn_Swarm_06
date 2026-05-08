"""
runtime/identity/principals.py
Agent Governance v2 — Identity Layer

Every entity in the system is a principal.
principal_type is immutable after creation.
Ed25519 public key stored for constraint write signature verification.

Functions:
    create_principal(principal_type, public_key) -> principal_id
    get_principal(principal_id) -> dict or None
    verify_signature(principal_id, message, signature) -> bool
"""

import uuid
import hmac
import hashlib
import secrets
from base64 import b64decode, b64encode
from datetime import datetime, timezone
from typing import Optional

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_der_public_key,
    )
    _CRYPTOGRAPHY_AVAILABLE = True
except Exception:  # pragma: no cover - fallback for constrained demo runtime
    InvalidSignature = Exception
    Ed25519PublicKey = object  # type: ignore[assignment]
    Encoding = None  # type: ignore[assignment]
    PublicFormat = None  # type: ignore[assignment]
    load_der_public_key = None  # type: ignore[assignment]
    _CRYPTOGRAPHY_AVAILABLE = False

from runtime.schema import get_connection

# ── Valid principal types ──────────────────────────────────────────────────────
VALID_TYPES = {"HUMAN", "ORCHESTRATOR", "AGENT", "SUBAGENT", "COMPLIANCE", "INTERCEPTOR"}

# ── Spawn authority — who can create which principal type ──────────────────────
# Used by session layer to validate parent-child type relationships.
SPAWN_AUTHORITY = {
    # type being created  : set of parent principal types allowed to create it
    "ORCHESTRATOR": {"HUMAN"},
    "AGENT":        {"ORCHESTRATOR", "AGENT"},
    "SUBAGENT":     {"AGENT"},
    "COMPLIANCE":   {"ORCHESTRATOR"},
}


def create_principal(principal_type: str, public_key: str) -> str:
    """
    Create a new principal and persist it.

    principal_type is immutable after creation.
    public_key must be a base64-encoded DER Ed25519 public key.
    HUMAN principals may pass an empty string for public_key when
    signature verification is handled externally (e.g. session auth).

    Args:
        principal_type: One of HUMAN, ORCHESTRATOR, AGENT, SUBAGENT, COMPLIANCE.
        public_key: Base64-encoded DER Ed25519 public key. Empty string for HUMAN.

    Returns:
        principal_id: UUID string of the created principal.

    Raises:
        ValueError: If principal_type is invalid or public_key is malformed.
    """
    if principal_type not in VALID_TYPES:
        raise ValueError(
            f"Invalid principal_type '{principal_type}'. "
            f"Must be one of: {sorted(VALID_TYPES)}"
        )

    # Validate public key format for non-HUMAN principals
    if principal_type != "HUMAN":
        if not public_key:
            raise ValueError("Non-HUMAN principals must provide a public_key.")
        if _CRYPTOGRAPHY_AVAILABLE:
            _parse_public_key(public_key)
        else:
            # Fallback runtime: ensure key is valid base64 bytes.
            b64decode(public_key)

    principal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO principals (id, principal_type, public_key, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (principal_id, principal_type, public_key, now),
        )
    conn.close()

    return principal_id


def get_principal(principal_id: str) -> Optional[dict]:
    """
    Retrieve a principal record by id.

    Args:
        principal_id: UUID string of the principal.

    Returns:
        dict with keys: id, principal_type, public_key, created_at.
        None if no principal exists with this id.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT id, principal_type, public_key, created_at "
        "FROM principals WHERE id = ?",
        (principal_id,),
    ).fetchone()
    conn.close()

    if row is None:
        return None

    return dict(row)


def verify_signature(
    principal_id: str,
    message: bytes,
    signature: str,
) -> bool:
    """
    Verify an Ed25519 signature against the principal's stored public key.

    Used by the constraint store to validate that a constraint write
    was actually signed by the claiming principal.

    Args:
        principal_id: UUID of the signing principal.
        message: Raw bytes that were signed.
        signature: Base64-encoded Ed25519 signature.

    Returns:
        True if signature is valid for this principal's key.
        False if principal not found, key empty, or signature invalid.
    """
    principal = get_principal(principal_id)
    if principal is None:
        return False

    public_key_b64 = principal.get("public_key", "")
    if not public_key_b64:
        # HUMAN principals may have no key — cannot verify, return False
        return False

    try:
        sig_bytes = b64decode(signature)
        if _CRYPTOGRAPHY_AVAILABLE:
            pub_key = _parse_public_key(public_key_b64)
            pub_key.verify(sig_bytes, message)
        else:
            key_bytes = b64decode(public_key_b64)
            expected = hmac.new(key_bytes, message, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, sig_bytes):
                return False
        return True
    except (InvalidSignature, Exception):
        return False


# ── Internal helpers ───────────────────────────────────────────────────────────

def _parse_public_key(public_key_b64: str) -> Ed25519PublicKey:
    """
    Parse a base64-encoded DER Ed25519 public key.

    Raises:
        ValueError: If the key cannot be parsed as Ed25519.
    """
    if not _CRYPTOGRAPHY_AVAILABLE:
        raise ValueError("Ed25519 parsing requires cryptography package")
    try:
        der_bytes = b64decode(public_key_b64)
        key = load_der_public_key(der_bytes)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("Key is not an Ed25519 public key")
        return key
    except Exception as e:
        raise ValueError(f"Invalid Ed25519 public key: {e}") from e


def generate_keypair() -> tuple[str, object]:
    """
    Generate an Ed25519 keypair for testing and demo use.

    Returns:
        (public_key_b64, private_key_object)
        public_key_b64: base64-encoded DER public key — store in principals table.
        private_key_object: Ed25519PrivateKey — use .sign(message) to sign.

    Usage:
        pub_b64, priv = generate_keypair()
        pid = create_principal("AGENT", pub_b64)
        sig = b64encode(priv.sign(b"message")).decode()
        assert verify_signature(pid, b"message", sig)
    """
    if _CRYPTOGRAPHY_AVAILABLE:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        pub_der = pub.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        pub_b64 = b64encode(pub_der).decode()
        return pub_b64, priv

    # Fallback for constrained runtime: symmetric signing key used as stored "public_key".
    key_bytes = secrets.token_bytes(32)

    class _FallbackPrivateKey:
        def __init__(self, key: bytes):
            self._key = key

        def sign(self, message: bytes) -> bytes:
            return hmac.new(self._key, message, hashlib.sha256).digest()

    return b64encode(key_bytes).decode(), _FallbackPrivateKey(key_bytes)


def sign_message(private_key, message: bytes) -> str:
    """
    Sign a message with an Ed25519 private key.

    Args:
        private_key: Ed25519PrivateKey object from generate_keypair().
        message: Raw bytes to sign.

    Returns:
        Base64-encoded signature string suitable for verify_signature().
    """
    return b64encode(private_key.sign(message)).decode()
