from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AllowedCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: list[int | str]
    default: int | str

    @model_validator(mode="after")
    def default_is_allowed(self) -> AllowedCapability:
        if self.default not in self.allowed:
            raise ValueError("capability default must be included in allowed values")
        return self


class L2MCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["disabled", "optional"]
    default: int
    allowed: list[int] | None = None
    supports_auto: bool = False
    minimum_bytes: int | None = Field(default=None, ge=0)
    maximum_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_mode(self) -> L2MCapability:
        if self.mode == "disabled":
            if self.allowed != [0] or self.default != 0:
                raise ValueError("disabled L2M must allow and default to zero")
        else:
            if self.minimum_bytes is None or self.maximum_bytes is None:
                raise ValueError("optional L2M requires minimum_bytes and maximum_bytes")
            if self.minimum_bytes > self.maximum_bytes:
                raise ValueError("minimum_bytes cannot exceed maximum_bytes")
            if not self.minimum_bytes <= self.default <= self.maximum_bytes:
                raise ValueError("L2M default is outside the supported range")
        return self


class TargetCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    core_num: AllowedCapability
    max_l2m_size: L2MCapability
    compile_mode: AllowedCapability
    optimize_level: AllowedCapability


class TargetProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"]
    profile_id: str
    display_name: str
    toolchain_adapter: Literal["openexplorer-3.7.0"]
    platform: Literal["s100", "s600"]
    march: Literal["nash-e", "nash-p"]
    operator_catalog: Literal["j6em", "j6p"]
    capabilities: TargetCapabilities

    @model_validator(mode="after")
    def validate_platform_lock(self) -> TargetProfile:
        expected = {
            "s100": ("nash-e", "j6em", [1]),
            "s600": ("nash-p", "j6p", [1, 2]),
        }[self.platform]
        if (self.march, self.operator_catalog, self.capabilities.core_num.allowed) != expected:
            raise ValueError(f"profile fields do not match locked {self.platform} capabilities")
        return self

    def snapshot(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return {
            "profile": payload,
            "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        }

    def validate_compile_options(self, *, core_num: int, max_l2m_size: int | str) -> None:
        if core_num not in self.capabilities.core_num.allowed:
            raise ValueError(f"core_num={core_num} is not supported by {self.profile_id}")
        l2m = self.capabilities.max_l2m_size
        if l2m.mode == "disabled":
            if max_l2m_size != 0:
                raise ValueError(f"max_l2m_size must be 0 for {self.profile_id}")
            return
        if max_l2m_size == "auto":
            if not l2m.supports_auto:
                raise ValueError(f"automatic L2M is not supported by {self.profile_id}")
            return
        if not isinstance(max_l2m_size, int):
            raise ValueError("max_l2m_size must be an integer or 'auto'")
        assert l2m.minimum_bytes is not None and l2m.maximum_bytes is not None
        if not l2m.minimum_bytes <= max_l2m_size <= l2m.maximum_bytes:
            raise ValueError(f"max_l2m_size={max_l2m_size} is outside the supported range")


class ProfileRegistry:
    def __init__(self, profiles: dict[str, TargetProfile]) -> None:
        if not profiles:
            raise ValueError("at least one Target Profile is required")
        self._profiles = profiles

    @classmethod
    def load(cls, directory: Path) -> ProfileRegistry:
        profiles: dict[str, TargetProfile] = {}
        for path in sorted(directory.glob("*.yaml")):
            with path.open(encoding="utf-8") as handle:
                raw = yaml.safe_load(handle)
            profile = TargetProfile.model_validate(raw)
            if profile.profile_id in profiles:
                raise ValueError(f"duplicate profile ID: {profile.profile_id}")
            profiles[profile.profile_id] = profile
        return cls(profiles)

    def get(self, profile_id: str) -> TargetProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise KeyError(f"unknown Target Profile: {profile_id}") from exc

    def list(self) -> list[TargetProfile]:
        return list(self._profiles.values())
