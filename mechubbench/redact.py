"""Redact secret-shaped and identifying values from benchmark manifests
before they hit disk.

Applied once, over the whole manifest, right before cli.py writes
results/*.json. Runs after scoring so outcome assertions (which check
staged_diff against synthetic fixture content) see the unredacted transcript;
only the on-disk artifact is scrubbed.

Two independent concerns share this module:

- Secrets (crypt hashes, SSH/PEM keys, pre-shared keys, tokens, ...): masked
  to a fixed ``REDACTED`` marker. The exact value never needs to be told
  apart from another instance of the same shape.
- Identifiers (IP addresses, host-name, serial-number, username,
  domain-name): masked to a *stable per-run placeholder* instead
  (``<IP-1>``, `<HOSTNAME-1>`, ...). Unlike secrets, two occurrences of the
  same address/hostname/etc. in one manifest are useful to tell apart from
  two occurrences of two different ones when a human reviews the redacted
  artifact, so identical inputs must resolve to the same placeholder within
  one `redact_manifest` call. Placeholders are not stable *across* calls.

The IP model is allowlist-shaped, not denylist-shaped: every IPv4/IPv6
literal is masked *unless* it falls in a documentation range, loopback, or a
literal the caller explicitly allows (typically because the scenario YAML
declares it via `expected_literals` and a human reviewer needs to recognize
it). Earlier versions of this module masked only RFC1918 ranges, which let
public addresses and all of IPv6 straight through.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

# Junos/PAN-OS crypt hashes: "$9$...", "$6$salt$hash", glibc/musl yescrypt
# "$y$params$salt$hash", etc. Junos' $9$ family uses its own base alphabet
# beyond [\w./-], so match anything up to the next whitespace or quote rather
# than enumerating characters.
_CRYPT_HASH_RE = re.compile(r'\$(?:\d|y|2[aby]|gy|7)\$[^\s"\'\\]+')

# PEM-encoded private key block (any algorithm: RSA, EC, "PRIVATE KEY", ...).
# Matched non-greedily across newlines so a manifest holding the block inside
# a JSON string (with literal "\n" sequences already decoded to real
# newlines by json.loads before _redact_text ever sees it as a leaf value)
# still has the whole block replaced, not just the BEGIN/END lines.
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.S,
)

# SSH public/private key material.
_SSH_KEY_RE = re.compile(
    r"ssh-(?:rsa|dss|ed25519|ecdsa-[\w-]+)\s+[A-Za-z0-9+/=]+(?:\s+\S+)?"
)

# IPv4 literal, with an optional CIDR suffix. Each octet is bounded to
# 0-255 so version-like tokens with an out-of-range component (e.g. a
# four-part build number) don't spuriously match, and so ipaddress.ip_address
# below never sees a value it would reject.
_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:/\d{1,2})?\b"
)

# IPv6 literal: full form, every valid "::" compression, and IPv4-mapped
# forms (::ffff:a.b.c.d), with an optional CIDR suffix. The leading/trailing
# lookarounds stop a match from starting or ending mid-token so this doesn't
# fire inside an unrelated hex/word run that happens to contain a colon.
_IPV6_RE = re.compile(
    r"""
    (?<![:.\w])
    (?:
        (?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}
      | (?:[A-Fa-f0-9]{1,4}:){1,7}:
      | (?:[A-Fa-f0-9]{1,4}:){1,6}:[A-Fa-f0-9]{1,4}
      | (?:[A-Fa-f0-9]{1,4}:){1,5}(?::[A-Fa-f0-9]{1,4}){1,2}
      | (?:[A-Fa-f0-9]{1,4}:){1,4}(?::[A-Fa-f0-9]{1,4}){1,3}
      | (?:[A-Fa-f0-9]{1,4}:){1,3}(?::[A-Fa-f0-9]{1,4}){1,4}
      | (?:[A-Fa-f0-9]{1,4}:){1,2}(?::[A-Fa-f0-9]{1,4}){1,5}
      | [A-Fa-f0-9]{1,4}:(?:(?::[A-Fa-f0-9]{1,4}){1,6})
      | :(?:(?::[A-Fa-f0-9]{1,4}){1,7}|:)
      | (?:[A-Fa-f0-9]{1,4}:){1,4}:
        (?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)
      | ::(?:[Ff]{4}(?::0{1,4})?:)?
        (?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)
    )
    (?:/\d{1,3})?
    (?![:.\w])
    """,
    re.X,
)

# Documentation ranges (RFC 5737, RFC 3849): never real device addresses, so
# safe to leave visible for scoring/debugging.
_DOC_RANGE_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)
_LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
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

# Keyword-anchored redaction for *identifying* (not secret) values: the SOC
# operator's identity and the device's identity. Masked to a stable per-run
# placeholder rather than REDACTED (see module docstring). Deliberately
# excludes the bare word "user" here - too common in ordinary prose
# (a final_message like "the user requested...") to anchor safely in free
# text; "user" is only redacted as an exact dict key (see
# _IDENTIFIER_DICT_ONLY_KEY_NAMES below) and via the Junos-specific
# "login user <name>" phrase (_LOGIN_USER_RE).
_IDENTIFIER_KEYWORDS = (
    "host-name",
    "serial-number",
    "username",
    "domain-name",
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

_IDENTIFIER_KEY_NAMES = frozenset(_normalize_key(k) for k in _IDENTIFIER_KEYWORDS)
_IDENTIFIER_KEYWORD_SEGMENTS = tuple(
    k.lower().split("-") for k in _IDENTIFIER_KEYWORDS
)

# "user" is dict-key-only (see _IDENTIFIER_KEYWORDS docstring above): exact
# match only, not segment-matched, so a compound key like "user_agent" isn't
# swept in just because it contains the word "user".
_IDENTIFIER_DICT_ONLY_KEY_NAMES = frozenset({"user"})

_IDENTIFIER_TYPE_BY_NORMALIZED_KEY = {
    "hostname": "HOSTNAME",
    "serialnumber": "SERIAL",
    "username": "USER",
    "domainname": "DOMAIN",
    "user": "USER",
}


def _key_segments(key: str) -> list[str]:
    """Split a key into lowercase segments on `-`, `_`, and camelCase bounds."""
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return [seg.lower() for seg in re.split(r"[-_]", key) if seg]


def _keyword_to_pattern(keyword: str) -> str:
    """Build a regex fragment matching `keyword` with `-` or `_` word separators."""
    return "[-_]".join(re.escape(part) for part in keyword.split("-"))


# Matches keyword-value pairs across vendor CLI syntax (`community` then a
# bare value, e.g. `FAKEval`), JSON text (`"community": "FAKEval"`), key=value
# syntax (`community`=`FAKEval`), and the `_`/`-` separator variants of
# multi-word keywords (`pre_shared_key`).
#
# The examples above are deliberately split across separate backtick spans
# instead of one contiguous keyword-separator-value span: a contiguous span
# is exactly the shape this rule matches, and gitleaks would flag this
# comment as a secret outside tests/, where no allowlist covers it (see
# .gitleaks.toml).
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

# Same shape as _SECRET_KEYWORD_RE, for the identifier keyword set. No
# ascii-text/hexadecimal format-word skip: that's a pre-shared-key-specific
# Junos quirk, not something identifier fields use.
_IDENTIFIER_KEYWORD_ALT = "|".join(
    _keyword_to_pattern(k) for k in _IDENTIFIER_KEYWORDS
)
_IDENTIFIER_KEYWORD_RE = re.compile(
    r'(?i)(?P<key>"?(?<![A-Za-z0-9])(?:' + _IDENTIFIER_KEYWORD_ALT + r')'
    r'(?:[-_][A-Za-z0-9]+)*\b"?)'
    r"(?P<sep>\s*[:=]\s*|\s+)"
    r'(?P<val>"(?:[^"\\]|\\.)*"'
    r"|'(?:[^'\\]|\\.)*'"
    r"|(?![\[{])[^\s\"']+)"
)

# Junos `set system login user <name> ...` - the username is the bare token
# right after "login user". Scoped to that exact two-word phrase (not bare
# "user") to avoid firing on ordinary prose; see _IDENTIFIER_KEYWORDS above.
_LOGIN_USER_RE = re.compile(
    r'(?i)(?P<key>\blogin\s+user)(?P<sep>\s+)'
    r'(?P<val>"(?:[^"\\]|\\.)*"'
    r"|'(?:[^'\\]|\\.)*'"
    r"|[^\s\"']+)"
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

# Same idea, for identifier elements (e.g. PAN-OS/Junos XML API responses
# with <host-name>myrouter</host-name> or <serial-number>...</serial-number>).
_IDENTIFIER_XML_ELEMENT_RE = re.compile(
    r"(?is)<((?:[A-Za-z0-9]+[-_])*(?:" + _IDENTIFIER_KEYWORD_ALT + r")"
    r"(?:[-_][A-Za-z0-9]+)*)\b([^>]*)>(.*?)</\1>"
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


class _RedactionState:
    """Bookkeeping shared across one `redact_manifest` call.

    Tracks which IP literals/identifier values have already been assigned a
    placeholder (so a repeated value gets the same placeholder throughout
    the manifest) and which literals are allowed to survive unmasked because
    the caller declared them (typically a scenario's `expected_literals`).
    """

    def __init__(self, allowed_literals: frozenset[str] | None = None) -> None:
        self._allowed_networks: list = []
        self._allowed_addresses: set = set()
        for literal in allowed_literals or ():
            try:
                if "/" in literal:
                    self._allowed_networks.append(
                        ipaddress.ip_network(literal, strict=False)
                    )
                else:
                    self._allowed_addresses.add(ipaddress.ip_address(literal))
            except ValueError:
                continue
        self._ip_placeholders: dict[str, str] = {}
        self._ip_counter = 0
        self._id_placeholders: dict[tuple[str, str], str] = {}
        self._id_counters: dict[str, int] = {}

    def is_allowed_ip(self, addr) -> bool:
        candidates = [addr]
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            candidates.append(mapped)
        for candidate in candidates:
            for net in _DOC_RANGE_NETWORKS + _LOOPBACK_NETWORKS:
                if candidate in net:
                    return True
            if candidate in self._allowed_addresses:
                return True
            for net in self._allowed_networks:
                if candidate in net:
                    return True
        return False

    def ip_placeholder(self, address_text: str) -> str:
        if address_text not in self._ip_placeholders:
            self._ip_counter += 1
            self._ip_placeholders[address_text] = f"<IP-{self._ip_counter}>"
        return self._ip_placeholders[address_text]

    def identifier_placeholder(self, kind: str, value: str) -> str:
        cache_key = (kind, value)
        if cache_key not in self._id_placeholders:
            self._id_counters[kind] = self._id_counters.get(kind, 0) + 1
            self._id_placeholders[cache_key] = f"<{kind}-{self._id_counters[kind]}>"
        return self._id_placeholders[cache_key]


def _identifier_type_for_key(key: str) -> str | None:
    """Return the identifier placeholder type (HOSTNAME/SERIAL/USER/DOMAIN)
    a dict key, XML tag, or text keyword corresponds to, or None."""
    normalized = _normalize_key(key)
    if normalized in _IDENTIFIER_DICT_ONLY_KEY_NAMES:
        return _IDENTIFIER_TYPE_BY_NORMALIZED_KEY[normalized]
    if normalized in _IDENTIFIER_KEY_NAMES:
        return _IDENTIFIER_TYPE_BY_NORMALIZED_KEY[normalized]
    segments = _key_segments(key)
    for kw_segs in _IDENTIFIER_KEYWORD_SEGMENTS:
        n = len(kw_segs)
        if n > len(segments):
            continue
        for i in range(len(segments) - n + 1):
            if segments[i : i + n] == kw_segs:
                normalized_kw = _normalize_key("-".join(kw_segs))
                return _IDENTIFIER_TYPE_BY_NORMALIZED_KEY[normalized_kw]
    return None


def _mask_ip_match(match: re.Match, state: _RedactionState) -> str:
    literal = match.group(0)
    address_part, sep, prefix = literal.partition("/")
    try:
        addr = ipaddress.ip_address(address_part)
    except ValueError:
        return literal
    if state.is_allowed_ip(addr):
        return literal
    placeholder = state.ip_placeholder(address_part)
    return f"{placeholder}/{prefix}" if sep else placeholder


def _sub_identifier_keyword(match: re.Match, state: _RedactionState) -> str:
    key_text = match.group("key")
    id_type = _identifier_type_for_key(key_text.strip('"')) or "ID"
    placeholder = state.identifier_placeholder(id_type, match.group("val"))
    return f"{key_text}{match.group('sep')}{placeholder}"


def _sub_login_user(match: re.Match, state: _RedactionState) -> str:
    placeholder = state.identifier_placeholder("USER", match.group("val"))
    return f"{match.group('key')}{match.group('sep')}{placeholder}"


def _sub_identifier_xml(match: re.Match, state: _RedactionState) -> str:
    tag, attrs, inner = match.group(1), match.group(2), match.group(3)
    id_type = _identifier_type_for_key(tag) or "ID"
    placeholder = state.identifier_placeholder(id_type, inner)
    return f"<{tag}{attrs}>{placeholder}</{tag}>"


def _redact_text(text: str, state: _RedactionState | None = None) -> str:
    if state is None:
        state = _RedactionState()

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
            return json.dumps(_redact_obj(parsed, state=state))

    text = _PEM_BLOCK_RE.sub(REDACTED, text)
    text = _CRYPT_HASH_RE.sub(REDACTED, text)
    text = _SSH_KEY_RE.sub(REDACTED, text)
    text = _XML_SECRET_ELEMENT_RE.sub(
        lambda m: f"<{m.group(1)}{m.group(2)}>{REDACTED}</{m.group(1)}>", text
    )
    text = _IDENTIFIER_XML_ELEMENT_RE.sub(
        lambda m: _sub_identifier_xml(m, state), text
    )
    text = _PANOS_AQ_SECRET_RE.sub(REDACTED, text)
    text = _IPV6_RE.sub(lambda m: _mask_ip_match(m, state), text)
    text = _IPV4_RE.sub(lambda m: _mask_ip_match(m, state), text)
    text = _AUTH_SCHEME_TOKEN_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _SECRET_KEYWORD_RE.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{REDACTED}", text
    )
    text = _IDENTIFIER_KEYWORD_RE.sub(
        lambda m: _sub_identifier_keyword(m, state), text
    )
    text = _LOGIN_USER_RE.sub(lambda m: _sub_login_user(m, state), text)
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


def _redact_obj(obj, key: str | None = None, state: _RedactionState | None = None):
    if state is None:
        state = _RedactionState()

    # Key-aware redaction: any value stored under a secret-carrying key
    # (e.g. {"community": "abc123"}, {"pre-shared-key": {"key": "x"}},
    # {"device-id": 123456789}) is redacted outright, regardless of its
    # shape or type - the key name is the only signal available, and a
    # non-string or nested value is not itself scanned for known secret
    # shapes below.
    if key is not None and obj is not None:
        if _is_secret_key(key):
            return REDACTED
        id_type = _identifier_type_for_key(key)
        if id_type is not None:
            value_for_placeholder = obj if isinstance(obj, str) else repr(obj)
            return state.identifier_placeholder(id_type, value_for_placeholder)

    if isinstance(obj, str):
        return _redact_text(obj, state=state)
    if isinstance(obj, dict):
        return {k: _redact_obj(v, key=k, state=state) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v, key=key, state=state) for v in obj]
    return obj


def redact_manifest(
    manifest: dict, allowed_literals: Iterable[str] | None = None
) -> dict:
    """Return a deep copy of manifest with secret-shaped and identifying
    values redacted.

    Args:
        manifest: Run manifest as produced by run_all_scenarios[_agentic]
        allowed_literals: IP literals (bare addresses or CIDR ranges) that
            must survive unmasked - typically the union of every loaded
            scenario's `expected_literals`, so a value a scorer depends on
            stays visible in the committed artifact.

    Returns:
        A new manifest dict; the input is not mutated.
    """
    state = _RedactionState(frozenset(allowed_literals) if allowed_literals else None)
    return _redact_obj(copy.deepcopy(manifest), state=state)
