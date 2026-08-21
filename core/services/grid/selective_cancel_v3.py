"""Version bridge for the pagination-safe Lighter selective cancel patch."""

PATCH_VERSION = "2026-08-21.3"


def install_grid_selective_cancel_v3() -> None:
    """Align grid route diagnostics with the installed Lighter v3 patch."""
    # Existing selective-cancel functions resolve PATCH_VERSION from their
    # module globals at call time, so updating the version keeps their route
    # diagnostics aligned with the adapter implementation without duplicating
    # ownership and shutdown logic.
    from . import selective_cancel as existing
    from .coordinator.grid_coordinator import GridCoordinator
    from .coordinator.order_operations import OrderOperations
    from .implementations.grid_engine_impl import GridEngineImpl

    existing.PATCH_VERSION = PATCH_VERSION
    GridEngineImpl._selective_cancel_version = PATCH_VERSION
    OrderOperations._selective_cancel_version = PATCH_VERSION
    GridCoordinator._selective_cancel_version = PATCH_VERSION
