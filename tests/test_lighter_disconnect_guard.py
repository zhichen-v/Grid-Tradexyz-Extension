import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "lighter_disconnect_guard_testpkg.adapters"

for package_name in ("lighter_disconnect_guard_testpkg", PACKAGE):
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package

spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.lighter_disconnect_guard",
    ROOT / "core/adapters/exchanges/adapters/lighter_disconnect_guard.py",
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class LighterDisconnectGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_disconnect_uses_original_path(self):
        original = AsyncMock()
        adapter = SimpleNamespace(
            _unguarded_disconnect=original,
            _rest=SimpleNamespace(config={}),
            _connected=True,
            _authenticated=True,
        )

        await module.guarded_lighter_disconnect(adapter)

        original.assert_awaited_once_with()
        self.assertTrue(adapter._connected)
        self.assertTrue(adapter._authenticated)

    async def test_timeout_force_closes_children_and_returns(self):
        async def hang():
            await asyncio.Event().wait()

        websocket_disconnect = AsyncMock()
        rest_close = AsyncMock()
        adapter = SimpleNamespace(
            _unguarded_disconnect=hang,
            _websocket=SimpleNamespace(disconnect=websocket_disconnect),
            _rest=SimpleNamespace(config={}, close=rest_close),
            _connected=True,
            _authenticated=True,
        )

        def timeout(_adapter, name, _default):
            return 0.01 if name == "disconnect_timeout" else 0.1

        with patch.object(module, "_timeout", side_effect=timeout):
            await module.guarded_lighter_disconnect(adapter)

        websocket_disconnect.assert_awaited_once_with()
        rest_close.assert_awaited_once_with()
        self.assertFalse(adapter._connected)
        self.assertFalse(adapter._authenticated)


if __name__ == "__main__":
    unittest.main()
