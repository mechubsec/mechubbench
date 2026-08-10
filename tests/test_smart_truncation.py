"""Tests for smart config truncation."""

from mechubbench import runner


def test_no_truncation_under_limit():
    """Text under limit is not truncated"""
    text = "set system host-name test"
    result, was_truncated = runner.smart_truncate_config(text, max_len=8000)
    assert result == text
    assert was_truncated is False


def test_config_path_specified_not_truncated():
    """If config_path specified, assume already scoped - no truncation"""
    text = "x" * 10000  # Over limit
    result, was_truncated = runner.smart_truncate_config(
        text,
        max_len=8000,
        config_path="security policies"
    )
    # Should not truncate when path specified
    assert result == text
    assert was_truncated is False


def test_domain_section_extraction():
    """Extract relevant section when domain specified"""
    # Generate large config to force extraction
    config = "set unrelated line\n" * 1000  # Lots of noise
    config += """
set system ntp server 132.163.97.1
set system ntp server 128.138.140.44
"""
    config += "set more unrelated\n" * 1000

    result, was_truncated = runner.smart_truncate_config(
        config,
        max_len=8000,  # Under full size but extraction helps
        domain="system ntp"
    )

    # Should contain ntp section
    assert "132.163.97.1" in result
    assert "128.138.140.44" in result
    # Was truncated because section extraction was used
    assert was_truncated is True or len(result) < len(config)


def test_head_tail_fallback():
    """Falls back to head+tail when no domain or section not found"""
    text = "x" * 10000
    result, was_truncated = runner.smart_truncate_config(text, max_len=8000)

    assert len(result) < len(text)
    assert "[truncated" in result
    assert was_truncated is True
    # Should have head and tail
    assert result.startswith("x")
    assert result.endswith("x")


def test_section_extraction_policies():
    """Extract security policies section"""
    # Large config with policies buried in it
    config = "set unrelated\n" * 1000
    config += """
set security policies policy trust-to-untrust match source zone trust
set security policies policy trust-to-untrust match destination zone untrust
set security policies policy trust-to-untrust then permit
"""
    config += "set more unrelated\n" * 1000

    result, was_truncated = runner.smart_truncate_config(
        config,
        max_len=8000,
        domain="security policies"
    )

    # Should contain policies section
    assert "trust-to-untrust" in result
    assert "permit" in result


def test_section_extraction_system_name_server():
    """Extract system name-server section"""
    # Large config
    config = "set unrelated\n" * 1000
    config += """
set system name-server 1.1.1.2
set system name-server 1.0.0.2
"""
    config += "set more unrelated\n" * 1000

    result, was_truncated = runner.smart_truncate_config(
        config,
        max_len=8000,
        domain="system name-server"
    )

    # Should contain name-server section
    assert "1.1.1.2" in result
    assert "1.0.0.2" in result
