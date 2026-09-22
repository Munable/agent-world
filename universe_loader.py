from __future__ import annotations

from typing import Callable

from runtime_core import WorldRuntime


UniverseInstaller = Callable[[WorldRuntime, str], None]


def get_universe_installer(profile: str) -> UniverseInstaller:
    normalized = profile.strip().lower()
    if normalized == "demo":
        from demo_universe import install_demo_universe

        return install_demo_universe
    if normalized == "commons":
        from commons_universe import install_commons_universe

        return install_commons_universe
    raise ValueError(f"unknown WORLD_PROFILE: {profile}")
