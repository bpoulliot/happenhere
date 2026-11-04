"""Local scraper plugins: extra source types loaded from a directory outside the repo.

A deployment keeps scrapers written for one particular site in its own directory
(``plugins_dir`` / ``PLUGINS_DIR``); this module imports them at start-up and registers each
scraper class under its ``SOURCE_TYPE``.

A plugin is code that runs with the app's privileges. It is loaded only from the configured
directory (never from the database, an upload or a URL), so that directory must be writable
only by the operator and should be mounted read-only.

Nothing here may stop the app: each module is imported on its own, and a module that fails to
import, or a type that cannot be registered, is skipped, logged at ERROR and listed in the
report that the admin page shows.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

from .scrapers import registry
from .scrapers.base import BaseScraper

log = logging.getLogger(__name__)

PACKAGE = "happenhere_plugins"
_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass
class LoadedPlugin:
    module: str
    source_type: str
    label: str
    class_name: str


@dataclass
class PluginError:
    module: str
    message: str


@dataclass
class PluginReport:
    directory: str = ""
    loaded: list[LoadedPlugin] = field(default_factory=list)
    errors: list[PluginError] = field(default_factory=list)


_report = PluginReport()
_registered: list[str] = []  # type keys this module added to the registry


def get_report() -> PluginReport:
    return _report


def unload() -> None:
    """Remove every plugin type from the registry and forget the modules."""
    global _report
    for key in _registered:
        registry.unregister_type(key)
    _registered.clear()
    for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
        del sys.modules[name]
    _report = PluginReport()


def load_plugins(plugins_dir: str | None) -> PluginReport:
    """Import every ``*.py`` directly in ``plugins_dir`` and register its scrapers.

    Safe to call again: the previous plugins are removed first. Unset means no plugins.
    """
    global _report
    unload()
    if not plugins_dir:
        return _report
    report = _report = PluginReport(directory=str(plugins_dir))

    root = Path(plugins_dir)
    if not root.is_dir():
        _fail(
            report, PACKAGE, f"plugins directory {str(root)!r} is not a directory", exc_info=False
        )
        return report

    pkg = types.ModuleType(PACKAGE)
    pkg.__path__ = [str(root)]  # lets one plugin import a sibling helper
    sys.modules[PACKAGE] = pkg

    try:
        files = sorted(p for p in root.iterdir() if p.suffix == ".py" and p.is_file())
    except OSError as exc:
        _fail(report, PACKAGE, f"cannot list {str(root)!r}: {exc}")
        return report

    for path in files:
        _load_module(report, path)
    log.info(
        "Plugins: %d type(s) loaded, %d error(s) from %s",
        len(report.loaded),
        len(report.errors),
        root,
    )
    return report


def _fail(report: PluginReport, module: str, message: str, *, exc_info: bool = True) -> None:
    log.error("Plugin %s: %s", module, message, exc_info=exc_info)
    report.errors.append(PluginError(module, message))


def _load_module(report: PluginReport, path: Path) -> None:
    name = f"{PACKAGE}.{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path.name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except (Exception, SystemExit) as exc:  # a plugin must never stop the app
        sys.modules.pop(name, None)
        _fail(report, name, f"{type(exc).__name__}: {exc}")
        return

    for _, cls in inspect.getmembers(module, inspect.isclass):
        if (
            cls.__module__ != name  # imported from elsewhere, not defined here
            or not issubclass(cls, BaseScraper)
            or inspect.isabstract(cls)
            or "SOURCE_TYPE" not in vars(cls)  # an inherited type is not this class's own
        ):
            continue
        _register(report, name, cls)


def _register(report: PluginReport, module: str, cls: type[BaseScraper]) -> None:
    key = cls.SOURCE_TYPE
    if not isinstance(key, str) or not _TYPE_RE.match(key):
        _fail(
            report,
            module,
            f"{cls.__name__}: SOURCE_TYPE {key!r} must be lowercase letters, digits, '_' or '-'",
            exc_info=False,
        )
        return
    if key in registry._TYPE_MAP:
        _fail(
            report,
            module,
            f"{cls.__name__}: SOURCE_TYPE {key!r} collides with an already registered type; refused",
            exc_info=False,
        )
        return
    label = getattr(cls, "TYPE_LABEL", None) or cls.__name__
    registry.register_type(key, cls, str(label))
    _registered.append(key)
    report.loaded.append(LoadedPlugin(module, key, str(label), cls.__name__))
    log.info("Plugin %s: registered source type %r (%s)", module, key, label)
