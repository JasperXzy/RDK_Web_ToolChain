from __future__ import annotations

from pathlib import Path

import pytest
from rdkwt_controller.profiles import ProfileRegistry


@pytest.fixture(scope="module")
def profiles() -> ProfileRegistry:
    root = Path(__file__).resolve().parents[2]
    return ProfileRegistry.load(root / "profiles" / "targets")


def test_s100_is_locked_to_nash_e(profiles: ProfileRegistry) -> None:
    profile = profiles.get("s100-oe-3.7.0")
    assert profile.march == "nash-e"
    profile.validate_compile_options(core_num=1, max_l2m_size=0)
    with pytest.raises(ValueError, match="core_num"):
        profile.validate_compile_options(core_num=2, max_l2m_size=0)
    with pytest.raises(ValueError, match="must be 0"):
        profile.validate_compile_options(core_num=1, max_l2m_size=1024)


def test_s600_allows_dual_core_and_bounded_l2m(profiles: ProfileRegistry) -> None:
    profile = profiles.get("s600-oe-3.7.0")
    assert profile.march == "nash-p"
    profile.validate_compile_options(core_num=2, max_l2m_size="auto")
    profile.validate_compile_options(core_num=2, max_l2m_size=25_165_824)
    with pytest.raises(ValueError, match="outside"):
        profile.validate_compile_options(core_num=2, max_l2m_size=25_165_825)


def test_profile_snapshot_hash_is_stable(profiles: ProfileRegistry) -> None:
    profile = profiles.get("s100-oe-3.7.0")
    assert profile.snapshot() == profile.snapshot()
    assert len(profile.snapshot()["sha256"]) == 64
