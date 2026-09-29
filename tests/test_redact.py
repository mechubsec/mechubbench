"""Tests for secret redaction applied to result manifests before they hit disk.

All secret shapes below are fabricated for this test file; none are derived
from real device output or real crypt/SSH-key material.
"""

import json

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
    doesn't run by default (see MEC-885)."""
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "FAKEbase64keymaterialAAAABBBBCCCCDDDDEEEEFFFF\n"
        "FAKEbase64keymaterialGGGGHHHHIIIIJJJJKKKKLLLL\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text = f"tool output:\n{pem}\nmore output"
    result = redact._redact_text(text)
    assert "FAKEbase64keymaterial" not in result
    assert "BEGIN RSA PRIVATE KEY" not in result
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
    text = "Authorization: Bearer FAKEbearer.tok3n.jwtlike12345"
    result = redact._redact_text(text)
    assert "FAKEbearer.tok3n.jwtlike12345" not in result
    assert "Bearer" in result


def test_basic_auth_credentials_redacted():
    """R3: only Bearer was handled; Basic auth leaked in full."""
    text = "Authorization: Basic FAKEbase64creds=="
    result = redact._redact_text(text)
    assert "FAKEbase64creds==" not in result
    assert "Basic" in result


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
    redacted even when its value has no recognizable secret shape - the
    key name is the only signal available (no CLI keyword text to scan)."""
    obj = {"community": "d3adbeefopaque"}
    result = redact._redact_obj(obj)
    assert result == {"community": redact.REDACTED}


def test_dict_key_secret_redacted_case_and_underscore_insensitive():
    obj = {"Encrypted_Password": "opaquevalue"}
    result = redact._redact_obj(obj)
    assert result == {"Encrypted_Password": redact.REDACTED}


def test_non_secret_non_identifier_key_left_to_text_scanning():
    obj = {"vendor": "junos"}
    result = redact._redact_obj(obj)
    assert result == {"vendor": "junos"}


def test_dict_key_secret_redacted_nested_dict_value():
    """R2: a secret-carrying key whose value is itself a dict (not a str)
    must still be redacted outright, rather than surviving unredacted."""
    obj = {"pre-shared-key": {"key": "FAKEnestedsecret"}}
    result = redact._redact_obj(obj)
    assert result == {"pre-shared-key": redact.REDACTED}


def test_dict_key_secret_redacted_int_value():
    """R2: a secret-carrying key whose value is a non-str scalar (e.g. an
    integer device-id) must still be redacted, not passed through untouched."""
    obj = {"device-id": 123456789}
    result = redact._redact_obj(obj)
    assert result == {"device-id": redact.REDACTED}


def test_dict_key_secret_redacted_list_value():
    """R2: a secret-carrying key whose value is a list of dicts must be
    redacted outright rather than recursing into the list's contents."""
    obj = {"community": [{"name": "FAKEcommunityname"}]}
    result = redact._redact_obj(obj)
    assert result == {"community": redact.REDACTED}


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
        obj = {key: "FAKEvalue"}
        result = redact._redact_obj(obj)
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
        obj = {key: "FAKEcompoundvalue"}
        result = redact._redact_obj(obj)
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
