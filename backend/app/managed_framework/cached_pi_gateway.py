"""Use the pinned Pi installation cache with OpenEnv's capture provider."""

from cached_pi import CachedPi
from openenv.harbor.install_fixes import InterceptPi


class CachedInterceptPi(CachedPi, InterceptPi):
    """Reuse CachedPi.install and InterceptPi.setup with the native Pi runner."""

    def __init__(self, *args, intercept_config, extra_env=None, **kwargs):
        base_url = intercept_config.get("base_url")
        session_key = intercept_config.get("api_key")
        if not (base_url and session_key and intercept_config.get("model")):
            raise ValueError("CachedInterceptPi requires an OpenEnv capture session")

        # Harbor 0.26 configures Pi's custom provider from its model connection.
        # Keep the upstream key in OpenEnv and give Pi only the capture session key.
        super().__init__(
            *args,
            intercept_config=intercept_config,
            base_url=f"{base_url}/v1",
            model_api="openai-completions",
            model_api_key_name="OPENAI_API_KEY",
            extra_env={**(extra_env or {}), "OPENAI_API_KEY": session_key},
            **kwargs,
        )
