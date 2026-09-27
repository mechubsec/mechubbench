"""Redact secret-shaped values from benchmark manifests before they hit disk.

Applied once, over the whole manifest, right before cli.py writes
results/*.json. Runs after scoring so outcome assertions (which check
staged_diff against synthetic fixture content) see the unredacted transcript;
only the on-disk artifact is scrubbed.
"""

from __future__ import annotations

import copy
import hashlib
import re

REDACTED = "[REDACTED]"
REDACTED_IP = "[REDACTED-IP]"

# Junos/PAN-OS crypt hashes: "$9$...", "$6$salt$hash", etc. Junos' $9$ family
# uses its own base alphabet beyond [\w./-], so match anything up to the next
# whitespace or quote rather than enumerating characters.
_CRYPT_HASH_RE = re.compile(r'\$\d\$[^\s"\'\\]+')

# SSH public/private key material.
_SSH_KEY_RE = re.compile(
    r"ssh-(?:rsa|dss|ed25519|ecdsa-[\w-]+)\s+[A-Za-z0-9+/=]+(?:\s+\S+)?"
)

# RFC1918 private address ranges: 10/8, 172.16/12, 192.168/16.
_RFC1918_RE = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|192\.168(?:\.\d{1,3}){2})\b"
)

# Keyword-anchored redaction: the token right after one of these keywords is
# the secret, regardless of vendor syntax quirks (quoted or bare).
_SECRET_KEYWORDS = (
    "secret",
    "encrypted-password",
    "pre-shared-key",
    "authentication-key",
    "community",
    "device-id",
)
_SECRET_KEYWORD_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(k) for k in _SECRET_KEYWORDS) + r")\b"
    r"(\s+)(\"(?:[^\"\\]|\\.)*\"|\S+)"
)

# Large multi-line blobs (e.g. an embedded device config dump) are reduced to
# a digest rather than stored verbatim.
_CONFIG_DIGEST_MIN_CHARS = 2000
_CONFIG_DIGEST_MIN_LINES = 5


def _redact_text(text: str) -> str:
    text = _CRYPT_HASH_RE.sub(REDACTED, text)
    text = _SSH_KEY_RE.sub(REDACTED, text)
    text = _RFC1918_RE.sub(REDACTED_IP, text)
    text = _SECRET_KEYWORD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)

    if len(text) > _CONFIG_DIGEST_MIN_CHARS and text.count("\n") > _CONFIG_DIGEST_MIN_LINES:
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        lines = text.count("\n") + 1
        text = (
            f"[CONFIG DIGEST sha256:{digest} "
            f"({lines} lines, {len(text)} chars) - content redacted]"
        )

    return text


def _redact_obj(obj):
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


def redact_manifest(manifest: dict) -> dict:
    """Return a deep copy of manifest with secret-shaped values redacted.

    Args:
        manifest: Run manifest as produced by run_all_scenarios[_agentic]

    Returns:
        A new manifest dict; the input is not mutated.
    """
    return _redact_obj(copy.deepcopy(manifest))
