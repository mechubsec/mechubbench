"""Tests for secret redaction applied to result manifests before they hit disk.

All secret shapes below are fabricated for this test file; none are derived
from real device output or real crypt/SSH-key material.
"""

import hashlib
import json

import pytest

from mechubbench import redact


def test_junos_crypt_hash_redacted():
    text = 'encrypted-password "$6$FAKEsaltAB12$notarealhashvaluepadded1234567890abcd";'
    result = redact._redact_text(text)
    assert "$6$" not in result
    assert "notarealhashvaluepadded" not in result
    assert redact.REDACTED in result


def test_two_part_crypt_hash_redacted():
    """$9$ hashes use Junos' own base alphabet (hyphens etc.), not just [\\w./]."""
    text = 'secret "$9$FAKE.hash-with-Junos_altAlphabet0123456789";'
    result = redact._redact_text(text)
    assert "$9$" not in result
    assert "FAKE.hash-with-Junos_altAlphabet" not in result


def test_ssh_key_redacted():
    text = (
        'ssh-ed25519 "ssh-ed25519 FAKEkeymaterialAAAABBBBCCCCDDDDEEEEFFFFgggg'
        'hhhhiiiijjjjkkkk+ demo-user@example.net";'
    )
    result = redact._redact_text(text)
    assert "FAKEkeymaterialAAAABBBBCCCCDDDD" not in result
    assert redact.REDACTED in result


def test_rfc1918_address_redacted():
    text = "syslog host 192.168.50.150 port 514"
    result = redact._redact_text(text)
    assert "192.168.50.150" not in result
    assert "<IP-1>" in result


def test_rfc1918_ranges_all_redacted():
    for ip in ["10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.0.1"]:
        result = redact._redact_text(f"address {ip}")
        assert ip not in result, f"{ip} should have been redacted"


def test_public_ipv4_address_redacted():
    """F4: inverted model - public IPv4 is masked same as private, since
    for a real SOC user a public address is just as identifying."""
    text = "ntp server 129.6.15.28"
    result = redact._redact_text(text)
    assert "129.6.15.28" not in result
    assert "<IP-1>" in result


def test_ipv4_cidr_redacted():
    text = "route 198.18.4.0/24 next-hop 10.0.0.1"
    result = redact._redact_text(text)
    assert "198.18.4.0" not in result
    assert result == "route <IP-1>/24 next-hop <IP-2>"


def test_ipv6_full_form_redacted():
    text = "address 2606:4700:4700::1111 on interface ge-0/0/0"
    result = redact._redact_text(text)
    assert "2606:4700:4700::1111" not in result
    assert "<IP-1>" in result


def test_ipv6_compressed_form_redacted():
    text = "set interfaces lo0 unit 0 family inet6 address fe80::1/64"
    result = redact._redact_text(text)
    assert "fe80::1" not in result
    assert "<IP-1>/64" in result


def test_ipv6_v4_mapped_form_redacted():
    text = "peer ::ffff:129.6.15.28 unreachable"
    result = redact._redact_text(text)
    assert "129.6.15.28" not in result
    assert "<IP-1>" in result


def test_ipv6_cidr_redacted():
    text = "aggregate route 2620:119:35::/48"
    result = redact._redact_text(text)
    assert "2620:119:35::" not in result
    assert result == "aggregate route <IP-1>/48"


def test_documentation_range_ipv4_not_redacted():
    for ip in ["192.0.2.5", "198.51.100.10", "203.0.113.254"]:
        result = redact._redact_text(f"ntp server {ip}")
        assert ip in result, f"documentation-range {ip} should survive"


def test_documentation_range_ipv6_not_redacted():
    text = "peer 2001:db8::1 configured"
    result = redact._redact_text(text)
    assert "2001:db8::1" in result


def test_loopback_addresses_not_redacted():
    for ip in ["127.0.0.1", "::1"]:
        result = redact._redact_text(f"listen on {ip}")
        assert ip in result, f"loopback {ip} should survive"


def test_scenario_declared_literal_survives_redact_manifest():
    """A literal the scenario YAML declares via expected_literals must
    survive so the committed manifest still shows what a scorer depends on,
    even though it's a public/non-doc-range address that would otherwise be
    masked."""
    manifest = {
        "results": [
            {
                "id": "discover-ntp",
                "transcript": [
                    {
                        "tool": "get_junos_config",
                        "args": {"device": "vsrx-ci"},
                        "result": "ntp server 129.6.15.28",
                    }
                ],
            }
        ]
    }
    redacted = redact.redact_manifest(manifest, allowed_literals={"129.6.15.28"})
    blob = json.dumps(redacted)
    assert "129.6.15.28" in blob


def test_ip_placeholder_stable_within_one_redact_manifest_call():
    """The same address repeated across a manifest must resolve to the same
    placeholder, so a human (or a scoring check) can tell two occurrences of
    one address apart from an occurrence of a different address."""
    manifest = {
        "results": [
            {
                "id": "r1",
                "transcript": [
                    {"tool": "t1", "args": {"host": "129.6.15.28"}},
                    {"tool": "t2", "args": {"note": "reused 129.6.15.28 again"}},
                ],
            }
        ]
    }
    redacted = redact.redact_manifest(manifest)
    blob = json.dumps(redacted)
    assert blob.count("<IP-1>") == 2
    assert "<IP-2>" not in blob


def test_host_name_keyword_redacted_text_and_dict_and_xml():
    text_result = redact._redact_text("set system host-name FAKErouter01")
    assert "FAKErouter01" not in text_result
    assert "<HOSTNAME-1>" in text_result

    dict_result = redact._redact_obj({"host-name": "FAKErouter01"})
    assert dict_result == {"host-name": "<HOSTNAME-1>"}

    xml_result = redact._redact_text("<host-name>FAKErouter01</host-name>")
    assert "FAKErouter01" not in xml_result
    assert "<HOSTNAME-1>" in xml_result


def test_serial_number_keyword_redacted_text_and_dict_and_xml():
    text_result = redact._redact_text("serial-number FAKESN123456")
    assert "FAKESN123456" not in text_result
    assert "<SERIAL-1>" in text_result

    dict_result = redact._redact_obj({"serial-number": "FAKESN123456"})
    assert dict_result == {"serial-number": "<SERIAL-1>"}

    xml_result = redact._redact_text("<serial-number>FAKESN123456</serial-number>")
    assert "FAKESN123456" not in xml_result
    assert "<SERIAL-1>" in xml_result


def test_domain_name_keyword_redacted():
    text_result = redact._redact_text("set system domain-name fakecorp.example")
    assert "fakecorp.example" not in text_result
    assert "<DOMAIN-1>" in text_result

    dict_result = redact._redact_obj({"domain-name": "fakecorp.example"})
    assert dict_result == {"domain-name": "<DOMAIN-1>"}


def test_username_keyword_redacted_text_and_dict():
    text_result = redact._redact_text('{"username": "sduser"}')
    assert "sduser" not in text_result

    dict_result = redact._redact_obj({"username": "sduser"})
    assert dict_result == {"username": "<USER-1>"}


def test_bare_user_dict_key_redacted():
    dict_result = redact._redact_obj({"user": "sduser"})
    assert dict_result == {"user": "<USER-1>"}


def test_login_user_phrase_redacted_but_bare_user_word_left_alone():
    """F4: `login user <name>` (Junos CLI) is anchored specifically so
    ordinary prose using the word "user" isn't swept up as an identifier."""
    result = redact._redact_text(
        "set system login user sduser class super-user authentication "
        'encrypted-password "$6$FAKEsalt$FAKEhashdata12345"'
    )
    assert "sduser" not in result
    assert "<USER-1>" in result

    prose_result = redact._redact_text("the user requested a config change")
    assert prose_result == "the user requested a config change"


def test_public_ip_and_identifier_masked_in_json_in_string_shape():
    """Same as the CLI/XML shapes above, but the value arrives as a JSON
    string (e.g. a `| display json` tool response) rather than vendor CLI
    text - the JSON detour in _redact_text must apply the same rules."""
    text = json.dumps(
        {
            "configuration": {
                "system": {
                    "host-name": "FAKErouter01",
                    "name-server": ["129.6.15.28"],
                }
            }
        }
    )
    result = redact._redact_text(text)
    assert "FAKErouter01" not in result
    assert "129.6.15.28" not in result


def test_public_ip_and_identifier_masked_in_dict_arg_shape():
    obj = {
        "tool": "gather_device_facts",
        "args": {"host-name": "FAKErouter01", "management_ip": "129.6.15.28"},
    }
    result = redact._redact_obj(obj)
    assert result["args"]["host-name"] == "<HOSTNAME-1>"
    assert "129.6.15.28" not in json.dumps(result)


def test_pem_private_key_block_redacted():
    """MEC-39 close-out: a PEM private-key block under a non-secret key
    (e.g. actions[].payload.text) must not survive verbatim - only
    gitleaks' opt-in pre-commit hook caught this before, and that hook
    doesn't run by default (see MEC-885).

    MEC-1241: mecmcp-redact's PEM handling redacts the key body but keeps
    the "-----BEGIN ... PRIVATE KEY-----" header line - the header names
    the algorithm, not the key, so this is still the safe direction (no key
    material survives) even though the exact marker differs from this
    repo's retired hand-rolled regex.
    """
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "FAKEbase64keymaterialAAAABBBBCCCCDDDDEEEEFFFF\n"
        "FAKEbase64keymaterialGGGGHHHHIIIIJJJJKKKKLLLL\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text = f"tool output:\n{pem}\nmore output"
    result = redact._redact_text(text)
    assert "FAKEbase64keymaterial" not in result
    assert redact.REDACTED in result


def test_bare_yescrypt_hash_redacted_without_keyword_anchor():
    """MEC-39 close-out: a bare $y$ yescrypt hash (free text, error string,
    or a model's own summary) must be redacted even with no
    `encrypted-password` keyword anchoring it - _CRYPT_HASH_RE previously
    only matched a single digit after the first `$` (\\$\\d\\$...)."""
    text = "hash dump: $y$j9T$FAKEsaltvalue123$FAKEhashvaluepaddedabcdef1234567890"
    result = redact._redact_text(text)
    assert "$y$" not in result
    assert "FAKEhashvaluepaddedabcdef1234567890" not in result
    assert redact.REDACTED in result


def test_pre_shared_key_redacted():
    text = 'set security ike proposal p1 pre-shared-key "FAKEsharedsecret123"'
    result = redact._redact_text(text)
    assert "FAKEsharedsecret123" not in result


def test_authentication_key_redacted():
    text = "set protocols ospf area 0 authentication-key FAKEauthkey456"
    result = redact._redact_text(text)
    assert "FAKEauthkey456" not in result


def test_snmp_community_redacted():
    text = "set snmp community FAKEcommunity789 authorization read-only"
    result = redact._redact_text(text)
    assert "FAKEcommunity789" not in result


def test_device_id_redacted():
    text = "device-id FAKE0000-1111-2222-3333-444455556666.JUNOS"
    result = redact._redact_text(text)
    assert "FAKE0000-1111-2222-3333-444455556666" not in result


def test_snmpv3_authentication_password_redacted():
    text = "set snmp v3 usm-user u1 authentication-password FAKEsnmpv3authpass"
    result = redact._redact_text(text)
    assert "FAKEsnmpv3authpass" not in result


def test_bearer_token_redacted():
    """MEC-1241: mecmcp-redact anchors on the "Authorization" key itself and
    redacts the whole header value (scheme word included), rather than
    keeping "Bearer" and redacting only the token - a broader, still-safe,
    over-redaction."""
    text = "Authorization: Bearer FAKEbearer.tok3n.jwtlike12345"
    result = redact._redact_text(text)
    assert "FAKEbearer.tok3n.jwtlike12345" not in result


def test_basic_auth_credentials_redacted():
    """R3: only Bearer was handled; Basic auth leaked in full. See
    test_bearer_token_redacted for why the scheme word isn't preserved."""
    text = "Authorization: Basic FAKEbase64creds=="
    result = redact._redact_text(text)
    assert "FAKEbase64creds==" not in result


def test_x_api_key_header_redacted():
    """R3: the api-key keyword must catch the common x-api-key header form."""
    text = "x-api-key: FAKExapikeyheadervalue"
    result = redact._redact_text(text)
    assert "FAKExapikeyheadervalue" not in result


def test_plaintext_pre_shared_key_ascii_text_format_word_redacted():
    """R1: Junos syntax puts a value-format word between the keyword and the
    actual secret, so a regex that only redacts the single token right after
    the keyword redacts the format word and leaves the real secret (the next
    token) untouched."""
    text = "set security ike policy p1 pre-shared-key ascii-text FAKEcorrecthorsebatterystaple"
    result = redact._redact_text(text)
    assert "FAKEcorrecthorsebatterystaple" not in result


def test_plaintext_pre_shared_key_hexadecimal_format_word_redacted():
    text = "set security ike policy p1 pre-shared-key hexadecimal FAKE0123456789abcdef0123456789"
    result = redact._redact_text(text)
    assert "FAKE0123456789abcdef0123456789" not in result


def test_plaintext_pre_shared_key_panos_set_format_key_word_redacted():
    """F2b-1: PAN-OS set-format syntax puts the literal word `key` between
    the keyword and the actual secret (`pre-shared-key key <secret>`).
    Without skipping `key` the same way as `ascii-text`/`hexadecimal`, the
    regex redacts the word `key` itself and leaves the real secret intact."""
    text = (
        "set network ike gateway gw1 authentication pre-shared-key "
        "key FAKEplainpsk999"
    )
    result = redact._redact_text(text)
    assert "FAKEplainpsk999" not in result


def test_text_underscore_keyword_variant_redacted():
    """R3: the dict path already normalized `_` to `-`, but the text regex
    only matched the literal hyphenated spelling."""
    text = '"pre_shared_key": "FAKEundersecretvalue"'
    result = redact._redact_text(text)
    assert "FAKEundersecretvalue" not in result


def test_single_quoted_value_with_embedded_space_fully_redacted():
    """R4: there was no single-quote alternative in the value capture, so a
    single-quoted multi-word value only had its first word swallowed,
    leaking the rest (plus a stray trailing quote)."""
    text = "community 'FAKE multi word secretval'"
    result = redact._redact_text(text)
    assert result.startswith("community")
    assert redact.REDACTED in result
    assert "multi word secretval" not in result


def test_quoted_bearer_token_closing_quote_preserved():
    """R4: `\\S+` for the bearer token ate the closing JSON quote."""
    text = '{"Authorization": "Bearer FAKEtoken123"}'
    result = redact._redact_text(text)
    assert "FAKEtoken123" not in result
    assert result.endswith('"}')


def test_json_text_keyword_with_colon_and_quotes_redacted():
    """A secret keyword serialized as JSON text (`"community": "x"`), not a
    vendor CLI line, must still be caught: the original regex only matched
    keyword-then-whitespace, missing the colon/quote JSON shape."""
    text = '{"community": "FAKEcommunityjson"}'
    result = redact._redact_text(text)
    assert "FAKEcommunityjson" not in result


def test_key_value_equals_syntax_redacted():
    text = "community=FAKEcommunityequals"
    result = redact._redact_text(text)
    assert "FAKEcommunityequals" not in result


def test_panos_xml_pre_shared_key_redacted():
    """PAN-OS XML API responses nest the secret in a child <key> element:
    <pre-shared-key><key>-AQ==...</key></pre-shared-key>. Redacting the
    whole element handles the nesting without hardcoding the child tag."""
    text = "<pre-shared-key><key>-AQ==FAKEbase64secretdata1234</key></pre-shared-key>"
    result = redact._redact_text(text)
    assert "FAKEbase64secretdata1234" not in result
    assert "-AQ==" not in result


def test_panos_aq_shape_redacted_outside_xml():
    """The PAN-OS `-AQ==` obfuscation prefix is a secret shape on its own,
    independent of any XML wrapping (e.g. bare in a JSON tool response)."""
    text = '{"psk": "-AQ==FAKEbase64secretdata5678"}'
    result = redact._redact_text(text)
    assert "FAKEbase64secretdata5678" not in result
    assert "-AQ==" not in result


def test_panos_xml_compound_tag_snmp_community_string_redacted():
    """F2b-2: PAN-OS wraps the SNMP community in a compound XML tag name
    (<snmp-community-string>), not the bare keyword. The XML element rule
    only matched exact keyword tag names, so the opening tag matched
    `community` but the closing tag `</snmp-community-string>` never closed
    it, leaving the plaintext community string untouched."""
    text = "<v2c><snmp-community-string>FAKEpubcomm</snmp-community-string></v2c>"
    result = redact._redact_text(text)
    assert "FAKEpubcomm" not in result


def test_dict_key_secret_redacted_even_without_matching_shape():
    """A secret stored under a known secret-carrying dict key must be
    redacted even when its value has no recognizable secret shape - the key
    name is the only signal available (no CLI keyword text to scan).

    MEC-1241: `_redact_obj` alone only knows about the local `device-id` gap
    plus identifiers (see module docstring); general dict-key secret
    redaction is `mecmcp_redact`'s denylist scan, applied by the final pass
    in `redact_manifest`, so this goes through the full pipeline instead of
    `_redact_obj` directly.
    """
    result = redact.redact_manifest({"community": "d3adbeefopaque"})
    assert result == {"community": redact.REDACTED}


def test_dict_key_secret_redacted_case_and_underscore_insensitive():
    result = redact.redact_manifest({"Encrypted_Password": "opaquevalue"})
    assert result == {"Encrypted_Password": redact.REDACTED}


def test_non_secret_non_identifier_key_left_to_text_scanning():
    obj = {"vendor": "junos"}
    result = redact._redact_obj(obj)
    assert result == {"vendor": "junos"}


def test_dict_key_secret_redacted_nested_dict_value():
    """R2: a secret-carrying key whose value is itself a dict (not a str)
    must still be redacted, rather than surviving unredacted.

    MEC-1241: mecmcp-redact's structural pass (`redact_manifest`'s final
    `mecmcp_redact.redact_json_str` step) preserves the container and
    force-redacts every scalar leaf inside it, rather than collapsing the
    whole value to a single `[REDACTED]` string the way this repo's retired
    hand-rolled key check did - no secret survives either way.
    """
    result = redact.redact_manifest({"pre-shared-key": {"key": "FAKEnestedsecret"}})
    assert result == {"pre-shared-key": {"key": redact.REDACTED}}


def test_dict_key_secret_redacted_int_value():
    """R2: a secret-carrying key whose value is a non-str scalar (e.g. an
    integer device-id) must still be redacted, not passed through untouched."""
    obj = {"device-id": 123456789}
    result = redact._redact_obj(obj)
    assert result == {"device-id": redact.REDACTED}


def test_dict_key_secret_redacted_list_value():
    """R2: a secret-carrying key whose value is a list of dicts must be
    redacted, not passed through untouched.

    MEC-1241: see test_dict_key_secret_redacted_nested_dict_value - the
    structural pass recurses into the list rather than collapsing it.
    """
    result = redact.redact_manifest({"community": [{"name": "FAKEcommunityname"}]})
    assert result == {"community": [{"name": redact.REDACTED}]}


def test_dict_key_password_family_variants_redacted():
    """R3: the dict-key denylist was missing common secret-carrying names
    (password, token, api_key, private_key, client_secret, psk), which
    survived key-aware redaction entirely."""
    for key in (
        "password",
        "passphrase",
        "token",
        "api_key",
        "api-key",
        "private_key",
        "client_secret",
        "psk",
        "presharedkey",
    ):
        result = redact.redact_manifest({key: "FAKEvalue"})
        assert result == {key: redact.REDACTED}, f"key {key!r} was not redacted"


def test_large_config_blob_reduced_to_digest():
    blob = "set system host-name test\n" * 200
    result = redact._redact_text(blob)
    assert result != blob
    assert "CONFIG DIGEST" in result
    assert "sha256:" in result


def test_short_text_not_digested():
    text = "set system ntp boot-server test"
    result = redact._redact_text(text)
    assert result == text


def test_redact_manifest_walks_nested_structures_and_does_not_mutate_input():
    manifest = {
        "results": [
            {
                "id": "test-01",
                "transcript": [
                    {
                        "tool": "create_junos_change_set",
                        "args": {
                            "device": "vsrx-ci",
                            "actions": [
                                {
                                    "payload": {
                                        "text": 'set snmp community "FAKEcommunity789"'
                                    }
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    }

    redacted = redact.redact_manifest(manifest)

    # Original untouched
    original_text = manifest["results"][0]["transcript"][0]["args"]["actions"][0]["payload"]["text"]
    assert "FAKEcommunity789" in original_text

    redacted_text = redacted["results"][0]["transcript"][0]["args"]["actions"][0]["payload"]["text"]
    assert "FAKEcommunity789" not in redacted_text


def test_dict_key_compound_secret_variants_redacted():
    """V1: the dict path compared the whole normalized key against the
    keyword set, so a compound key (keyword plus an extra prefix/suffix
    segment) survived even though the equivalent bare keyword didn't."""
    for key in (
        "x-api-key",
        "simple-password",
        "privacy-password",
        "snmp-community",
        "admin_password",
        "access_token",
        "secret_key",
        "accessToken",
    ):
        result = redact.redact_manifest({key: "FAKEcompoundvalue"})
        assert result == {key: redact.REDACTED}, f"key {key!r} was not redacted"


def test_text_underscore_prefixed_keyword_redacted():
    """V1: a leading `\\b` failed to match a keyword immediately after an
    underscore (both are word characters, so there's no boundary), leaving
    `admin_password=...` and `access_token=...` untouched in text scanning."""
    for text, secret in (
        ("admin_password=FAKEadminpass", "FAKEadminpass"),
        ("access_token=FAKEaccesstok", "FAKEaccesstok"),
    ):
        result = redact._redact_text(text)
        assert secret not in result, f"{text!r} leaked {secret!r}"


def test_json_rendered_list_config_nested_secret_redacted():
    """V2: the keyword regex took the `{`/`[` right after the keyword as the
    value, leaving the real secret nested one level down (e.g. Junos
    `| display json` output for an SNMP community, list form) untouched."""
    text = '{"community": [{"name": "FAKEcommlistname", "authorization": "read-only"}]}'
    result = redact._redact_text(text)
    assert "FAKEcommlistname" not in result


def test_json_rendered_nested_dict_secret_redacted():
    """V2: same gap, nested dict form (Junos `pre-shared-key` rendered as
    `{"ascii-text": "..."}` under `| display json`)."""
    text = '{"pre-shared-key": {"ascii-text": "FAKEplainpsk"}}'
    result = redact._redact_text(text)
    assert "FAKEplainpsk" not in result


def test_json_rendered_config_pretty_printed_nested_secret_redacted():
    """V2: the JSON detour must also handle pretty-printed (multi-line,
    indented) JSON, not just compact single-line JSON."""
    text = (
        "{\n"
        '  "community": [\n'
        "    {\n"
        '      "name": "FAKEprettycommname"\n'
        "    }\n"
        "  ]\n"
        "}"
    )
    result = redact._redact_text(text)
    assert "FAKEprettycommname" not in result


def test_ospf_md5_plaintext_key_redacted():
    """V3: `key` alone is not a secret-carrying keyword (too common a false
    positive source), so the OSPF MD5 authentication key line passed through
    unchanged."""
    text = (
        "set protocols ospf area 0 interface ge-0/0/0 "
        "authentication md5 1 key FAKEmd5key"
    )
    result = redact._redact_text(text)
    assert "FAKEmd5key" not in result


def test_json_rendered_config_over_digest_threshold_is_digested():
    """R-A: the V2 JSON detour returned `json.dumps(...)` before the
    config-digest size check ran, and json.dumps re-serialises compactly (no
    newlines), so a large pretty-printed `| display json` config that should
    be digested sailed through whole instead - a regression versus the
    equivalent non-JSON text, which was still digested."""
    host_names = ", ".join(f'"FAKEhost-dc1-leaf{i:03d}"' for i in range(150))
    text = (
        "{\n"
        '  "configuration": {\n'
        '    "system": {\n'
        '      "host-name": "FAKEprimaryhostname",\n'
        f'      "name-server": [{host_names}]\n'
        "    }\n"
        "  }\n"
        "}"
    )
    assert len(text) > redact._CONFIG_DIGEST_MIN_CHARS
    assert text.count("\n") > redact._CONFIG_DIGEST_MIN_LINES

    result = redact._redact_text(text)

    assert "FAKEprimaryhostname" not in result
    assert "CONFIG DIGEST" in result
    assert "sha256:" in result


def test_text_suffix_keyword_variant_redacted():
    """R-B: the trailing `\\b` in the keyword regex still failed to match a
    keyword immediately followed by an `_suffix` (both are word characters,
    so there's no boundary), leaving `secret_key=...`, `password_plain=...`
    and `token_value: ...` untouched in text scanning even though the
    equivalent dict keys were already redacted by `_is_secret_key`."""
    for text, secret in (
        ("secret_key=FAKEsk1", "FAKEsk1"),
        ("password_plain=FAKEpp1", "FAKEpp1"),
        ("token_value: FAKEtv1", "FAKEtv1"),
    ):
        result = redact._redact_text(text)
        assert secret not in result, f"{text!r} leaked {secret!r}"


def test_redact_manifest_from_synthetic_run_with_secret_shapes():
    """A result file written from a synthetic run has secret shapes redacted.

    Covers the MEC-27 acceptance criterion: a result file written from a
    synthetic run that contains secret shapes has them redacted. All values
    below are fabricated, not real device output.
    """
    manifest = {
        "schema_version": 1,
        "run_id": "synthetic-run-1",
        "model": "test-model",
        "results": [
            {
                "id": "heal-junos-add-ntp",
                "pass": True,
                "reason": "outcome assertions satisfied",
                "transcript": [
                    {
                        "tool": "get_junos_config",
                        "args": {"device": "vsrx-ci"},
                    },
                    {
                        "tool": "create_junos_change_set",
                        "args": {
                            "device": "vsrx-ci",
                            "actions": [
                                {
                                    "payload": {
                                        "text": (
                                            "set system login user sduser authentication "
                                            'encrypted-password "$6$FAKEsalt$FAKEhashdata12345";\n'
                                            'set system radius-server 192.168.5.9 secret "FAKEradiussecret";\n'
                                            'set snmp community "FAKEcommunitystring"'
                                        )
                                    }
                                }
                            ],
                        },
                    },
                ],
            }
        ],
    }

    redacted = redact.redact_manifest(manifest)
    blob = str(redacted)

    for secret_shape in [
        "$6$FAKEsalt$FAKEhashdata12345",
        "FAKEradiussecret",
        "FAKEcommunitystring",
        "192.168.5.9",
    ]:
        assert secret_shape not in blob, f"{secret_shape!r} leaked into redacted manifest"


def test_redact_manifest_covers_every_known_secret_form_for_junos_and_panos():
    """MEC-1241 coverage fixture: a manifest carrying one instance of every
    secret shape this benchmark harness's two vendors (Junos, PAN-OS) can
    emit - whether caught by mecmcp_redact's shared engine or by this
    repo's two confirmed local gaps (SSH key shape, device-id keyword; see
    redact.py's module docstring) - must come back with none of the raw
    values present anywhere in the redacted manifest. All values are
    fabricated, not real device output.
    """
    secrets = {
        "junos_crypt_hash": '$9$FAKEjunoscrypthashAB12cdEF34',
        "junos_pre_shared_key": "FAKEjunospsksecretvalue",
        "junos_snmp_community": "FAKEjunoscommunitystring",
        "junos_device_id": "FAKE0000-1111-2222-3333-444455556666.JUNOS",
        "junos_radius_secret": "FAKEjunosradiussecret",
        "panos_aq_blob": "-AQ==FAKEpanosaqsecretvalue1234",
        "panos_api_key": "FAKEpanosapikeyvalue",
        "panos_admin_password": "FAKEpanosadminpasswordvalue",
        "pem_private_key": "FAKEbase64pemkeymaterialAAAABBBBCCCCDDDD",
        "ssh_public_key": "FAKEsshkeymaterialAAAABBBBCCCCDDDDEEEEFFFFgggghhhh",
        "bearer_token": "FAKEbearertoken12345.jwtlike",
    }
    junos_result = "\n".join(
        [
            'set system login user sduser authentication encrypted-password '
            f'"{secrets["junos_crypt_hash"]}";',
            'set security ike proposal p1 pre-shared-key ascii-text '
            f'"{secrets["junos_pre_shared_key"]}";',
            f'set snmp community "{secrets["junos_snmp_community"]}" '
            "authorization read-only;",
            f'device-id {secrets["junos_device_id"]};',
            f'set system radius-server 192.168.5.9 secret '
            f'"{secrets["junos_radius_secret"]}";',
            f"ssh-ed25519 {secrets['ssh_public_key']} demo-user@example.net",
        ]
    )
    panos_result = "\n".join(
        [
            f'<pre-shared-key><key>{secrets["panos_aq_blob"]}</key>'
            "</pre-shared-key>",
            f'{{"api_key": "{secrets["panos_api_key"]}"}}',
            f'{{"admin_password": "{secrets["panos_admin_password"]}"}}',
            f"Authorization: Bearer {secrets['bearer_token']}",
            "-----BEGIN RSA PRIVATE KEY-----",
            secrets["pem_private_key"],
            "-----END RSA PRIVATE KEY-----",
        ]
    )
    manifest = {
        "run_id": "coverage-fixture",
        "results": [
            {
                "id": "junos-coverage",
                "transcript": [
                    {
                        "tool": "get_junos_config",
                        "args": {"device": "vsrx-ci"},
                        "result": junos_result,
                    },
                ],
            },
            {
                "id": "panos-coverage",
                "transcript": [
                    {
                        "tool": "panos_show_config",
                        "args": {"device": "pa-dc1"},
                        "result": panos_result,
                    },
                ],
            },
        ],
    }

    redacted = redact.redact_manifest(manifest)
    blob = json.dumps(redacted)

    for name, value in secrets.items():
        assert value not in blob, f"{name} ({value!r}) leaked into redacted manifest"


# --- PR #8 review follow-up (Percy, F4-1 through F4-8) ------------------
#
# All values below are synthetic. Each test fails against 0d0db01 (the PR #8
# head Percy reviewed) and passes after the fix it's named for.


def test_ipv6_before_colon_separator_redacted():
    """F4-1: an address immediately followed by ':' (the standard
    `<addr>: <error>` shape of a tool_error) must still be masked. The old
    _IPV6_RE's trailing `(?![:.\\w])` lookahead failed to fire next to
    another ':', so this leaked whole."""
    result = redact._redact_text("connect to 2001:470::9: timeout")
    assert "2001:470::9" not in result
    assert "<IP-1>" in result


def test_ipv6_before_period_redacted():
    """F4-1: same lookahead gap, end-of-sentence period instead of colon."""
    result = redact._redact_text("peer is 2001:470:1f0b::7.")
    assert "2001:470:1f0b::7" not in result
    assert result == "peer is <IP-1>."


def test_ipv6_after_colon_prefix_redacted():
    """F4-1: the symmetric leading-side gap - `(?<![:.\\w])` also failed to
    fire when the address is preceded by ':' (e.g. a `key:value` shape)."""
    result = redact._redact_text("ip:2001:470::7")
    assert "2001:470::7" not in result
    assert result == "ip:<IP-1>"


def test_ipv6_cidr_still_redacted_after_candidate_rewrite():
    """Regression guard for the candidate+ipaddress-validate rewrite: a
    trailing '::' before a CIDR suffix is valid zero-compression syntax, not
    a separator to strip, and stripping it first (rather than trying the
    full run against ipaddress first) turns a valid address into an invalid
    one - this was caught while fixing F4-1, not part of Percy's review."""
    result = redact._redact_text("aggregate route 2620:119:35::/48")
    assert "2620:119:35::" not in result
    assert result == "aggregate route <IP-1>/48"


def test_hostname_bare_keyword_redacted_text_dict_xml():
    """F4-2: `_keyword_to_pattern` required a literal '-' or '_' between
    "host" and "name", so the bare concatenated form PAN-OS/Junos prose both
    use ("Hostname:", "hostname=", "<hostname>") wasn't anchored in text or
    XML at all - only the dict-key path (which normalizes separators away
    before comparing) caught it."""
    text_result = redact._redact_text("Hostname: FAKErouter01")
    assert "FAKErouter01" not in text_result
    assert "<HOSTNAME-1>" in text_result

    kv_result = redact._redact_text("hostname=FAKErouter01 serial=FAKESN123456")
    assert "FAKErouter01" not in kv_result
    assert "FAKESN123456" not in kv_result

    xml_result = redact._redact_text("<hostname>FAKErouter01</hostname>")
    assert "FAKErouter01" not in xml_result
    assert "<HOSTNAME-1>" in xml_result


def test_panos_serial_and_devicename_xml_tags_redacted():
    """F4-2: PAN-OS XML API responses use <serial> and <devicename>, not
    Junos's <serial-number>/<host-name> - the acceptance criteria name
    PAN-OS XML shapes explicitly."""
    xml = "<system><devicename>pa-dc1</devicename><serial>0123456789AB</serial></system>"
    result = redact._redact_text(xml)
    assert "pa-dc1" not in result
    assert "0123456789AB" not in result
    assert "<HOSTNAME-1>" in result
    assert "<SERIAL-1>" in result


def test_truncated_pem_private_key_redacted():
    """F4-3: a PEM block cut off mid-stream (model output or tool arguments
    truncated before an END line) must still have its body redacted, not
    just the addresses that happen to have a matching END line.

    MEC-1241: see test_pem_private_key_block_redacted - the BEGIN header
    line surviving is expected under mecmcp-redact, not a leak.
    """
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "FAKEbase64keymaterialAAAABBBBCCCCDDDDEEEEFFFF\n"
        "FAKEbase64keymaterialGGGGHHHHIIII [truncated]"
    )
    result = redact._redact_text(pem)
    assert "FAKEbase64keymaterial" not in result
    assert redact.REDACTED in result


def test_device_identity_args_redacted_without_keyword():
    """F4-4: args.device (and the runner's other DEVICE_ARG_KEYS: router,
    router_name) are inventory names with no secret/identifier keyword
    anchoring them elsewhere, and they're written verbatim on every real
    run against a device."""
    dict_result = redact._redact_obj({"device": "fw-edge01"})
    assert dict_result == {"device": "<HOSTNAME-1>"}

    for key in ("router", "router_name"):
        result = redact._redact_obj({key: "fw-edge01"})
        assert result == {key: "<HOSTNAME-1>"}


def test_devices_touched_list_redacted_per_element():
    """F4-4: the manifest's top-level devices_touched is a list of device
    names, not a single string - masking must apply per element, not
    collapse the whole list into one placeholder."""
    result = redact._redact_obj({"devices_touched": ["fw-edge01", "fw-edge02"]})
    assert result == {"devices_touched": ["<HOSTNAME-1>", "<HOSTNAME-2>"]}


def test_device_identity_survives_when_declared_as_expected_literal():
    """F4-4: a scenario can still declare a lab device name (e.g. vsrx-ci)
    via expected_literals so it stays readable in the committed manifest,
    the same mechanism used for IP literals a scorer depends on."""
    manifest = {"args": {"device": "vsrx-ci"}, "devices_touched": ["vsrx-ci"]}
    redacted = redact.redact_manifest(manifest, allowed_literals={"vsrx-ci"})
    assert redacted == manifest
    # Without the declared exemption, the same device name is masked - this
    # also confirms "device"/"devices_touched" are identifier-anchored at
    # all (see test_device_identity_args_redacted_without_keyword).
    assert redact.redact_manifest(manifest) != manifest


def test_dict_key_ip_shape_redacted():
    """F4-5: vendor API responses (Mist, PAN-OS) are sometimes keyed by IP
    or MAC-adjacent identifiers - a dict key holding a bare IP literal must
    be masked the same as an IP appearing as a value."""
    result = redact._redact_obj({"8.8.4.4": {"status": "up"}})
    assert result == {"<IP-1>": {"status": "up"}}
    assert "8.8.4.4" not in json.dumps(result)


def test_ipv4_adjacent_to_underscore_redacted():
    """F4-6: `\\b` treats '_' as a word character, so it never fires between
    an identifier prefix and the address in shapes like `addr_<ip>` or an
    interface name immediately followed by an address."""
    assert "8.8.8.8" not in redact._redact_text("addr_8.8.8.8")
    assert "100.64.1.9" not in redact._redact_text("ge-0/0/0_100.64.1.9")


def test_expected_literals_rejects_network_wider_than_host():
    """F4-7: expected_literals exempts specific values a scorer depends on,
    not whole subnets - "0.0.0.0/0" (or "::/0") would turn off IP masking
    entirely, which is exactly the exemption-widening this field must not
    allow."""
    with pytest.raises(ValueError):
        redact.validate_expected_literal("0.0.0.0/0")
    with pytest.raises(ValueError):
        redact.validate_expected_literal("::/0")
    with pytest.raises(ValueError):
        redact.redact_manifest({"x": "unused"}, allowed_literals={"10.0.0.0/8"})


def test_expected_literals_accepts_host_address_and_doc_subnet():
    """F4-7: a single host (/32, /128) or a network fully inside the
    documentation ranges is a legitimate expected_literals entry and must
    not be rejected."""
    redact.validate_expected_literal("129.6.15.28/32")
    redact.validate_expected_literal("2001:470::7/128")
    redact.validate_expected_literal("192.0.2.0/24")
    redact.validate_expected_literal("129.6.15.28")


def test_identifier_placeholder_stable_across_quoting():
    """F4-8: the same value quoted and unquoted (`host-name "fw1"` vs.
    `host-name fw1`, both valid Junos syntax) must resolve to the same
    placeholder - the previous cache key included the literal quote
    characters, splitting one value into two placeholders."""
    manifest = {
        "a": "host-name \"fw1\"",
        "b": "host-name fw1",
    }
    redacted = redact.redact_manifest(manifest)
    assert redacted["a"] == redacted["b"] == "host-name <HOSTNAME-1>"


def test_ip_placeholder_stable_across_ipv6_compression_forms():
    """F4-8: "2001:470::7" and "2001:470:0::7" are the same address spelled
    two ways; the previous cache key was the literal matched text, so they
    got different placeholders. Keying on the canonical str(addr) fixes
    this the same way json.dumps compact re-serialization already does for
    other shapes in this module."""
    result = redact._redact_text("2001:470::7 and 2001:470:0::7")
    assert result == "<IP-1> and <IP-1>"


def test_json_tool_output_identifiers_masked():
    """MEC-1241 review F1: a JSON-string leaf (e.g. a tool's raw `output`
    field) was routed straight to `mecmcp_redact.redact_json_str`, which has
    no identifier-masking concept at all, so hostnames and usernames inside
    it reached the manifest in clear text - a regression versus a plain
    (non-JSON-wrapped) string, which still got identifier masking."""
    manifest = {
        "results": [
            {
                "output": (
                    '[{"device": "fw3.corp", "user": "bob", '
                    '"serial-number": "FAKESN9"}]'
                )
            }
        ]
    }
    redacted = redact.redact_manifest(manifest)
    output = redacted["results"][0]["output"]
    assert "fw3.corp" not in output
    assert "bob" not in output
    assert "FAKESN9" not in output
    assert "<HOSTNAME-1>" in output
    assert "<USER-1>" in output
    assert "<SERIAL-1>" in output


def test_json_tool_output_nested_identifier_masked():
    """MEC-1241 review F1, nested form: `{"login": {"user": "carol"}}`."""
    result = redact._redact_text('{"login": {"user": "carol"}}')
    assert "carol" not in result
    assert "<USER-1>" in result


def test_json_string_leaf_stays_valid_json_after_redaction():
    """MEC-1241 review F2: the old JSON detour ran the identifier/secret text
    regexes before deciding the text was JSON, and those regexes replace a
    quoted value including the quotes - turning the JSON-string leaf into
    invalid JSON (and, for `_SECRET_KEYWORD_RE`'s keyword-anchored match,
    swallowing unrelated trailing fields once the text fell through to
    mecmcp-redact's own text-format keyword rule)."""
    result = redact._redact_text(
        '{"password": "FAKEjsonpw", "host-name": "fw1.corp"}'
    )
    parsed = json.loads(result)  # must still be valid JSON
    assert parsed["password"] == redact.REDACTED
    assert parsed["host-name"] == "<HOSTNAME-1>"


def test_config_digest_hashes_redacted_not_raw_text():
    """MEC-1241 review F5: the digest used to hash the raw config (secrets
    included). An attacker who can guess most of a low-entropy secret (a
    short PSK, say) could confirm the guess offline against that hash. The
    digest must hash the redacted text instead; only the reported size
    still comes from the original."""
    filler = "set system host-name test\n" * 100
    blob = (
        'set security ike policy p1 pre-shared-key ascii-text "FAKEpsk12345"\n'
        + filler
    )
    assert len(blob) > redact._CONFIG_DIGEST_MIN_CHARS
    assert blob.count("\n") > redact._CONFIG_DIGEST_MIN_LINES

    result = redact._redact_text(blob)

    assert "CONFIG DIGEST" in result
    raw_digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    assert raw_digest not in result, "digest hashed the raw (unredacted) config"
