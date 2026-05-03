"""FiestaBoard plugin shim — re-exports the plugin class from the package layout.

This file exists for compatibility with FiestaBoard installations that expect
the plugin entry point at the repository root rather than in plugins/network_speed/.
"""
import importlib.util
from pathlib import Path

_sub_init = Path(__file__).parent / "plugins" / "network_speed" / "__init__.py"
_spec = importlib.util.spec_from_file_location("__network_speed_impl__", _sub_init)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("_")})
