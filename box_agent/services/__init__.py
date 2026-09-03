"""Runtime services exposed through the stable Agent Service contract."""

from importlib import import_module

_EXPORTS = {
    "KernelChildAgentRunner": (".delegation", "KernelChildAgentRunner"),
    "KernelAgentService": (".kernel", "KernelAgentService"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(target[0], __name__), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = list(_EXPORTS)
