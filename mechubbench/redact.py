"""Redact secret-shaped values from benchmark manifests before they hit disk.

Applied once, over the whole manifest, right before cli.py writes
results/*.json. Runs after scoring so outcome assertions (which check
staged_diff against synthetic fixture content) see the unredacted transcript;
only the on-disk artifact is scrubbed.
"""

from __future__ import annotations

import copy
import hashlib
import json
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
# the secret, regardless of vendor syntax quirks (quoted or bare). Used both
# for text scanning (_SECRET_KEYWORD_RE, below) and for key-aware redaction
# of parsed dict values (_redact_obj) whose value doesn't itself look like
# any known secret shape (e.g. an opaque token stored under "community").
_SECRET_KEYWORDS = (
    "secret",
    "encrypted-password",
    "pre-shared-key",
    "psk",
    "authentication-key",
    "authentication-password",  # SNMPv3 USM auth key
    "community",
    "device-id",
    "password",
    "passphrase",
    "token",
    "api-key",
    "private-key",
    "client-secret",
)


def _normalize_key(key: str) -> str:
    """Fold a key/keyword to a separator-insensitive form for comparison.

    Vendor CLI, JSON, and Python-style dict keys spell the same secret
    differently (`pre-shared-key`, `pre_shared_key`, `presharedkey`); collapse
    all of them to the same bare-word form before comparing.
    """
    return key.strip().lower().replace("-", "").replace("_", "")


_SECRET_KEY_NAMES = frozenset(_normalize_key(k) for k in _SECRET_KEYWORDS)

# Segment form of each keyword (e.g. "pre-shared-key" -> ["pre", "shared",
# "key"]), used to match a keyword appearing as a contiguous run within a
# compound dict key ("x-api-key", "admin_password", "snmp-community") that
# _normalize_key's whole-string comparison above misses.
_SECRET_KEYWORD_SEGMENTS = tuple(k.lower().split("-") for k in _SECRET_KEYWORDS)


def _key_segments(key: str) -> list[str]:
    """Split a key into lowercase segments on `-`, `_`, and camelCase bounds."""
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return [seg.lower() for seg in re.split(r"[-_]", key) if seg]


def _keyword_to_pattern(keyword: str) -> str:
    """Build a regex fragment matching `keyword` with `-` or `_` word separators."""
    return "[-_]".join(re.escape(part) for part in keyword.split("-"))


# Matches keyword-value pairs across vendor CLI syntax (`community FAKEval`),
# JSON text (`"community": "FAKEval"`), key=value syntax (`community=FAKEval`),
# and the `_`/`-` separator variants of multi-word keywords (`pre_shared_key`).
# The keyword itself may be bare or quoted (JSON keys); the value may be
# double- or single-quoted, bare, separated by whitespace, a colon, or an
# equals sign. An optional Junos value-format word (`ascii-text`,
# `hexadecimal`) between the keyword and the actual secret is skipped so the
# secret itself - not just the format tag - gets redacted.
#
# A trailing `[-_][A-Za-z0-9]+` run is consumed after the keyword (and before
# the `\b`) so a compound key like `secret_key` or `token_value` is matched
# whole: `_` is a word character, so `\b` alone doesn't fire between the
# keyword and an attached `_suffix` (same underlying gap V1 fixed on the
# leading side via the `(?<![A-Za-z0-9])` lookbehind above).
_SECRET_KEYWORD_ALT = "|".join(_keyword_to_pattern(k) for k in _SECRET_KEYWORDS)
_SECRET_KEYWORD_RE = re.compile(
    r'(?i)(?P<key>"?(?<![A-Za-z0-9])(?:' + _SECRET_KEYWORD_ALT + r')'
    r'(?:[-_][A-Za-z0-9]+)*\b"?)'
    r"(?P<sep>\s*[:=]\s*|\s+)"
    r"(?:(?:ascii-text|hexadecimal|key)\s+)?"
    r'(?P<val>"(?:[^"\\]|\\.)*"'
    r"|'(?:[^'\\]|\\.)*'"
    r"|(?![\[{])[^\s\"']+)"
)

# OSPF plaintext MD5 authentication key, e.g. vendor CLI's
# `authentication md5 <id> key <value>` syntax. The bare word `key` is not
# itself a secret-carrying keyword (too common a false-positive source), so
# this only fires in that specific `md5 <id> key` sequence.
_OSPF_MD5_KEY_RE = re.compile(
    r'(?i)(?P<key>\bmd5\s+\d+\s+key)(?P<sep>\s+)'
    r'(?P<val>"(?:[^"\\]|\\.)*"'
    r"|'(?:[^'\\]|\\.)*'"
    r"|[^\s\"']+)"
)

# PAN-OS's own obfuscation format for secrets (pre-shared keys, TOTP seeds,
# etc.) always starts with this literal prefix regardless of surrounding
# syntax (bare CLI, JSON, or wrapped in XML - see _XML_SECRET_ELEMENT_RE).
_PANOS_AQ_SECRET_RE = re.compile(r"-AQ==[A-Za-z0-9+/=]{8,}")

# XML element wrapping a secret, e.g. PAN-OS API responses:
#   <pre-shared-key><key>-AQ==...</key></pre-shared-key>
# The secret may sit directly in the element or in a nested child element
# (like <key>); redacting the whole element body handles both without
# needing to enumerate vendor-specific child tag names. The tag name may
# also carry segments around the keyword (e.g. <snmp-community-string>),
# same as _is_secret_key does for dict keys.
_XML_SECRET_ELEMENT_RE = re.compile(
    r"(?is)<((?:[A-Za-z0-9]+[-_])*(?:" + _SECRET_KEYWORD_ALT + r")"
    r"(?:[-_][A-Za-z0-9]+)*)\b([^>]*)>.*?</\1>"
)

# `Authorization: Bearer <token>` / `Authorization: Basic <creds>` (or a bare
# scheme + token in logged headers/errors) - the scheme name is kept, only
# the token/credentials are redacted. The token charclass excludes quotes so
# a JSON-quoted header value (`"Authorization": "Bearer x"`) keeps its
# closing quote intact.
_AUTH_SCHEME_TOKEN_RE = re.compile(r'(?i)\b(Bearer|Basic)\s+([^\s"\']+)')

# Large multi-line blobs (e.g. an embedded device config dump) are reduced to
# a digest rather than stored verbatim.
_CONFIG_DIGEST_MIN_CHARS = 2000
_CONFIG_DIGEST_MIN_LINES = 5


def _is_config_sized(text: str) -> bool:
    return (
        len(text) > _CONFIG_DIGEST_MIN_CHARS
        and text.count("\n") > _CONFIG_DIGEST_MIN_LINES
    )


def _digest(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    lines = text.count("\n") + 1
    return (
        f"[CONFIG DIGEST sha256:{digest} "
        f"({lines} lines, {len(text)} chars) - content redacted]"
    )


def _redact_text(text: str) -> str:
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        try:
            parsed = json.loads(stripped)
        except ValueError:
            pass
        else:
            # Check the digest threshold against the original text first:
            # json.dumps re-serialises compactly (no newlines), so a large
            # pretty-printed config would otherwise sail through as full
            # JSON instead of being digested like its non-JSON equivalent.
            if _is_config_sized(text):
                return _digest(text)
            return json.dumps(_redact_obj(parsed))

    text = _CRYPT_HASH_RE.sub(REDACTED, text)
    text = _SSH_KEY_RE.sub(REDACTED, text)
    text = _XML_SECRET_ELEMENT_RE.sub(
        lambda m: f"<{m.group(1)}{m.group(2)}>{REDACTED}</{m.group(1)}>", text
    )
    text = _PANOS_AQ_SECRET_RE.sub(REDACTED, text)
    text = _RFC1918_RE.sub(REDACTED_IP, text)
    text = _AUTH_SCHEME_TOKEN_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _SECRET_KEYWORD_RE.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{REDACTED}", text
    )
    text = _OSPF_MD5_KEY_RE.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{REDACTED}", text
    )

    if _is_config_sized(text):
        text = _digest(text)

    return text


def _is_secret_key(key: str) -> bool:
    if _normalize_key(key) in _SECRET_KEY_NAMES:
        return True
    segments = _key_segments(key)
    for kw_segs in _SECRET_KEYWORD_SEGMENTS:
        n = len(kw_segs)
        if n > len(segments):
            continue
        for i in range(len(segments) - n + 1):
            if segments[i : i + n] == kw_segs:
                return True
    return False


def _redact_obj(obj, key: str | None = None):
    # Key-aware redaction: any value stored under a secret-carrying key
    # (e.g. {"community": "abc123"}, {"pre-shared-key": {"key": "x"}},
    # {"device-id": 123456789}) is redacted outright, regardless of its
    # shape or type - the key name is the only signal available, and a
    # non-string or nested value is not itself scanned for known secret
    # shapes below.
    if key is not None and _is_secret_key(key) and obj is not None:
        return REDACTED
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v, key=k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v, key=key) for v in obj]
    return obj


def redact_manifest(manifest: dict) -> dict:
    """Return a deep copy of manifest with secret-shaped values redacted.

    Args:
        manifest: Run manifest as produced by run_all_scenarios[_agentic]

    Returns:
        A new manifest dict; the input is not mutated.
    """
    return _redact_obj(copy.deepcopy(manifest))
