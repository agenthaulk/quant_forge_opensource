"""Public runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from quant_forge.core.contracts import SimulationProfile


@dataclass(frozen=True)
class PathSettings:
    data_root: Path = Path("data")
    factor_root: Path = Path("factor_root")
    artifact_root: Path = Path("artifacts")
    output_root: Path = Path("outputs")


@dataclass(frozen=True)
class WebSettings:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class ResearchSettings:
    default_horizon_days: int = 5
    default_top_quantile: float = 0.3


@dataclass(frozen=True)
class LLMProviderSettings:
    provider: str = "rule"
    model: str = "deterministic"
    base_url: str = ""
    api_key_env: str = ""
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "rule"
    model: str = "deterministic"
    base_url: str = ""
    api_key_env: str = ""
    timeout_seconds: float = 30.0
    providers: dict[str, LLMProviderSettings] = field(default_factory=dict)

    def select_provider(self, provider: str | None = None) -> "LLMSettings":
        selected = _normalize_provider_name(provider or self.provider)
        if selected in {"rule", "deterministic"}:
            return LLMSettings(
                provider="rule",
                model="deterministic",
                timeout_seconds=self.timeout_seconds,
                providers=self.providers,
            )

        configured = self.providers.get(selected)
        if configured is None and selected == _normalize_provider_name(self.provider):
            configured = LLMProviderSettings(
                provider=self.provider,
                model=self.model,
                base_url=self.base_url,
                api_key_env=self.api_key_env,
                timeout_seconds=self.timeout_seconds,
            )
        if configured is None:
            available = ", ".join(sorted(self.providers)) or "none"
            raise ValueError(
                f"LLM provider '{selected}' is not configured. "
                f"Add llm.providers.{selected} to the config file. Available providers: {available}."
            )
        _validate_llm_provider_settings(configured)
        return LLMSettings(
            provider=configured.provider,
            model=configured.model,
            base_url=configured.base_url,
            api_key_env=configured.api_key_env,
            timeout_seconds=configured.timeout_seconds,
            providers=self.providers,
        )

    def public_provider_options(self) -> tuple[dict[str, str], ...]:
        options: list[dict[str, str]] = []
        for name, settings in self.providers.items():
            if name in {"rule", "deterministic"}:
                continue
            options.append(
                {
                    "provider": name,
                    "model": settings.model,
                    "base_url": settings.base_url,
                    "api_key_env": settings.api_key_env,
                }
            )
        return tuple(options)


@dataclass(frozen=True)
class QuantForgeConfig:
    paths: PathSettings = PathSettings()
    web: WebSettings = WebSettings()
    research: ResearchSettings = ResearchSettings()
    simulation: SimulationProfile = SimulationProfile()
    llm: LLMSettings = LLMSettings()

    def resolve(self, workspace: Path | None = None) -> "QuantForgeConfig":
        if workspace is None:
            return self
        root = workspace.expanduser()
        return QuantForgeConfig(
            paths=PathSettings(
                data_root=_under(root, self.paths.data_root),
                factor_root=_under(root, self.paths.factor_root),
                artifact_root=_under(root, self.paths.artifact_root),
                output_root=_under(root, self.paths.output_root),
            ),
            web=self.web,
            research=self.research,
            simulation=self.simulation,
            llm=self.llm,
        )


def load_config(config_path: Path | None = None, workspace: Path | None = None) -> QuantForgeConfig:
    """Load config from YAML, falling back only to documented public defaults."""

    raw: dict[str, Any] = {}
    if config_path is not None:
        path = config_path.expanduser()
        if not path.exists():
            raise FileNotFoundError(f"config file does not exist: {path}")
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("config file must contain a mapping")
        raw = loaded

    config = QuantForgeConfig(
        paths=PathSettings(
            data_root=_path_setting(raw, "data_root", default="data"),
            factor_root=_path_setting(raw, "factor_root", default="factor_root"),
            artifact_root=_path_setting(raw, "artifact_root", default="artifacts"),
            output_root=_path_setting(raw, "output_root", default="outputs"),
        ),
        web=WebSettings(
            host=str(_nested(raw, "web", "host", default="127.0.0.1")),
            port=int(_nested(raw, "web", "port", default=8765)),
        ),
        research=ResearchSettings(
            default_horizon_days=int(_nested(raw, "research", "default_horizon_days", default=5)),
            default_top_quantile=float(_nested(raw, "research", "default_top_quantile", default=0.3)),
        ),
        simulation=simulation_profile_from_mapping(raw.get("simulation"), SimulationProfile()),
        llm=llm_settings_from_mapping(raw.get("llm")),
    )
    return config.resolve(workspace)


def llm_settings_from_mapping(raw: Any) -> LLMSettings:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("llm config section must be a mapping")

    default_timeout = float(raw.get("timeout_seconds", 30.0))
    active_provider = str(raw.get("provider", "rule"))
    legacy_settings = LLMProviderSettings(
        provider=active_provider,
        model=str(raw.get("model", "deterministic")),
        base_url=str(raw.get("base_url", "")),
        api_key_env=str(raw.get("api_key_env", "")),
        timeout_seconds=default_timeout,
    )

    providers: dict[str, LLMProviderSettings] = {
        "rule": LLMProviderSettings(provider="rule", model="deterministic", timeout_seconds=default_timeout)
    }
    providers_raw = raw.get("providers", {})
    if providers_raw is None:
        providers_raw = {}
    if not isinstance(providers_raw, dict):
        raise ValueError("llm.providers config section must be a mapping")
    for provider_name, provider_raw in providers_raw.items():
        settings = _llm_provider_from_mapping(str(provider_name), provider_raw, default_timeout)
        providers[_normalize_provider_name(provider_name)] = settings

    active_name = _normalize_provider_name(active_provider)
    if active_name not in {"rule", "deterministic"} and active_name not in providers:
        providers[active_name] = legacy_settings

    for provider_settings in providers.values():
        _validate_llm_provider_settings(provider_settings)

    settings = LLMSettings(
        provider=active_provider,
        model=legacy_settings.model,
        base_url=legacy_settings.base_url,
        api_key_env=legacy_settings.api_key_env,
        timeout_seconds=default_timeout,
        providers=providers,
    )
    return settings


def simulation_profile_from_mapping(raw: Any, base: SimulationProfile | None = None) -> SimulationProfile:
    profile = base or SimulationProfile()
    if raw is None:
        return profile
    if not isinstance(raw, dict):
        raise ValueError("simulation config section must be a mapping")
    test_period = raw.get("test_period", {})
    if test_period is None:
        test_period = {}
    if not isinstance(test_period, dict):
        raise ValueError("simulation test_period must be a mapping")
    return SimulationProfile(
        market=str(raw.get("market", profile.market)),
        instrument_type=str(raw.get("instrument_type", profile.instrument_type)),
        universe=str(raw.get("universe", profile.universe)),
        execution_delay_days=int(raw.get("execution_delay_days", profile.execution_delay_days)),
        top_quantile=float(raw.get("top_quantile", profile.top_quantile)),
        nan_policy=str(raw.get("nan_policy", profile.nan_policy)),  # type: ignore[arg-type]
        neutralization=str(raw.get("neutralization", profile.neutralization)),  # type: ignore[arg-type]
        truncation=raw.get("truncation", profile.truncation),
        decay_days=int(raw.get("decay_days", profile.decay_days)),
        test_period_start=_optional_str(test_period.get("start", profile.test_period_start)),
        test_period_end=_optional_str(test_period.get("end", profile.test_period_end)),
    )


def _llm_provider_from_mapping(
    provider_name: str,
    raw: Any,
    default_timeout_seconds: float,
) -> LLMProviderSettings:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"llm.providers.{provider_name} must be a mapping")
    provider = str(raw.get("provider", provider_name))
    settings = LLMProviderSettings(
        provider=provider,
        model=str(raw.get("model", "")),
        base_url=str(raw.get("base_url", "")),
        api_key_env=str(raw.get("api_key_env", "")),
        timeout_seconds=float(raw.get("timeout_seconds", default_timeout_seconds)),
    )
    if _normalize_provider_name(provider) in {"rule", "deterministic"}:
        return LLMProviderSettings(provider="rule", model="deterministic", timeout_seconds=settings.timeout_seconds)
    return settings


def _validate_llm_provider_settings(settings: LLMProviderSettings) -> None:
    provider = _normalize_provider_name(settings.provider)
    if provider in {"rule", "deterministic"}:
        return
    missing: list[str] = []
    if not settings.model.strip() or settings.model.strip() == "deterministic":
        missing.append("model")
    if not settings.base_url.strip():
        missing.append("base_url")
    if not settings.api_key_env.strip():
        missing.append("api_key_env")
    if missing:
        joined = ", ".join(f"llm.providers.{provider}.{name}" for name in missing)
        raise ValueError(
            f"LLM provider '{provider}' is missing required config: {joined}. "
            "Set these fields in the config file; api_key_env must name an environment variable, not contain the key."
        )


def _normalize_provider_name(value: object) -> str:
    return str(value).strip().lower()


def _nested(raw: dict[str, Any], section: str, key: str, *, default: Any) -> Any:
    section_value = raw.get(section, {})
    if section_value is None:
        return default
    if not isinstance(section_value, dict):
        raise ValueError(f"config section must be a mapping: {section}")
    return section_value.get(key, default)


def _path_setting(raw: dict[str, Any], key: str, *, default: str) -> Path:
    value = _nested(raw, "paths", key, default=default)
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"paths.{key} is required in the config file")
    return Path(normalized)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _under(root: Path, child: Path) -> Path:
    if child.is_absolute():
        return child
    return root / child
