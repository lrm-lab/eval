"""Install the published Pi runtime with Harbor's native Pi harness."""

from benchpi.harbor_agent import InstallationRetry
from harbor.agents.installed.pi import Pi


class CachedPi(InstallationRetry, Pi):
    CACHE_KIND = "pi"
