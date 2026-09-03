"""Contract-invullers voor onderdeel A en B, alleen zolang die er nog niet zijn.

Onderdeel C (`agency_os/app/*`) wordt gebouwd tegen de bevroren contracten uit
`docs/architecture.md` sectie 3. Die contracten zijn afgesproken, maar de code
erachter komt van twee andere engineers op twee andere branches. Zonder deze map
kan C op zijn eigen branch niet één keer draaien, en dat is precies de situatie
waarin fouten pas bij het samenvoegen zichtbaar worden.

`install()` doet daarom één ding: als `agency_os.linear.models` (A) of
`agency_os.executors.base` (B) niet te importeren is, hangt hij de modules
hieronder onder die naam in `sys.modules`. Zodra A en B gemerged zijn, doet
`install()` niets meer en verandert er geen letter aan de tests.

De invullers zijn geen tweede implementatie: ze zijn zo klein mogelijk gehouden
en volgen de handtekeningen uit sectie 3 letterlijk, zodat een afwijking meteen
opvalt in plaats van weggemoffeld te worden.
"""

from __future__ import annotations

import importlib
import sys
import types

# Volgorde is importvolgorde: een invuller mag alleen leunen op wat er al staat.
LINEAR_MODULES = (
    "models", "client", "machine", "comments", "store", "ledger", "claim", "killswitch", "poll", "gates",
)
EXECUTOR_MODULES = {"base": "executors_base", "cost": "executors_cost", "worktree": "executors_worktree"}


def install() -> None:
    """Vult ontbrekende A- en B-modules aan. Idempotent, en stil als er niets hoeft."""
    if _missing("agency_os.linear.models"):
        _install_package("agency_os.linear", {name: name for name in LINEAR_MODULES})
    if _missing("agency_os.executors.base"):
        _install_package("agency_os.executors", EXECUTOR_MODULES)


def _missing(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return True
    return False


def _install_package(package_name: str, modules: dict[str, str]) -> None:
    """Hangt een nep-pakket met de gevraagde submodules in `sys.modules`."""
    package = sys.modules.get(package_name)
    if package is None or not hasattr(package, "__path__"):
        package = types.ModuleType(package_name)
        package.__path__ = []  # type: ignore[attr-defined]
        package.__doc__ = f"Invuller voor {package_name} zolang het echte onderdeel ontbreekt."
        sys.modules[package_name] = package
        setattr(sys.modules["agency_os"], package_name.rsplit(".", 1)[1], package)

    for public, stub in modules.items():
        full = f"{package_name}.{public}"
        if full in sys.modules:
            continue
        module = importlib.import_module(f"tests.stubs.{stub}")
        sys.modules[full] = module
        setattr(package, public, module)
