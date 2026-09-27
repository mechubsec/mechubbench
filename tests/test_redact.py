"""Tests for secret redaction applied to result manifests before they hit disk.

All secret shapes below are fabricated for this test file; none are derived
from real device output or real crypt/SSH-key material.
"""

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
    text = "syslog host 198.51.100.150 port 514"
    result = redact._redact_text(text)
    assert "198.51.100.150" not in result
    assert redact.REDACTED_IP in result


def test_rfc1918_ranges_all_redacted():
    for ip in ["10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.0.1"]:
        result = redact._redact_text(f"address {ip}")
        assert ip not in result, f"{ip} should have been redacted"


def test_public_address_not_redacted():
    """Only RFC1918 ranges are private; public IPs are left alone."""
    text = "ntp server 129.6.15.28"
    result = redact._redact_text(text)
    assert "129.6.15.28" in result


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


def test_large_config_blob_reduced_to_digest():
    blob = "set system host-name test\n" * 200
    result = redact._redact_text(blob)
    assert result != blob
    assert "CONFIG DIGEST" in result
    assert "sha256:" in result


def test_short_text_not_digested():
    text = "set system host-name test"
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
