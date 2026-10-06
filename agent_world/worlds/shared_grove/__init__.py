"""The official finite-material, spatial Shared Grove world package."""
from .world import make_world

WORLD = make_world()

__all__ = ["WORLD", "make_world"]
