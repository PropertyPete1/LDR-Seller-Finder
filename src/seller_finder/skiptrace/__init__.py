"""Skip-trace providers (pluggable).

Every provider implements SkipTraceProvider. To add a provider, subclass it,
register it in PROVIDERS, and set SKIPTRACE_PROVIDER env/setting.
"""
from .base import SkipTraceProvider, SkipTraceResult
from .batchdata import BatchDataProvider

PROVIDERS = {
    "batchdata": BatchDataProvider,
}


def get_provider(name: str = "batchdata") -> SkipTraceProvider:
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown skip-trace provider '{name}'. Available: {list(PROVIDERS)}")
    return cls()
