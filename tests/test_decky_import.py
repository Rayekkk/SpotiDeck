import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


class DeckyImportTests(unittest.TestCase):
    def test_plugin_loads_from_foreign_cwd_in_isolated_interpreter(self):
        plugin = Path(__file__).resolve().parent.parent / "main.py"
        script = textwrap.dedent("""
            import asyncio
            import importlib.util
            import logging
            from pathlib import Path
            import sys
            import types

            plugin_path = Path(sys.argv[1])
            temporary = Path(sys.argv[2])
            assert Path.cwd() != plugin_path.parent
            assert str(plugin_path.parent) not in sys.path
            assert "backend" not in sys.modules
            # Match Decky's only plugin-specific path and its injected module.
            sys.path.append(str(plugin_path.parent / "py_modules"))
            sys.modules["decky"] = types.SimpleNamespace(
                DECKY_PLUGIN_SETTINGS_DIR=str(temporary / "settings"),
                DECKY_PLUGIN_RUNTIME_DIR=str(temporary / "runtime"),
                logger=logging.getLogger("isolated-decky-import"),
            )
            spec = importlib.util.spec_from_file_location("_", plugin_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            async def check():
                plugin = module.Plugin()
                try:
                    await plugin._main()
                    result = await plugin.dispatch("snapshot", {})
                    assert result["ok"], result
                    assert result["data"]["connected"] is False
                    assert result["data"]["playback"] is None
                    assert result["data"]["player"]["installed"] is False
                finally:
                    await plugin._unload()

            asyncio.run(check())
            print("isolated Decky import and lifecycle passed")
        """)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-I", "-c", script, str(plugin), temporary],
                cwd=temporary, env=environment, capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("isolated Decky import and lifecycle passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
