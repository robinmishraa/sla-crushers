"""Registry tool: resolves an Issue to the runner config.

The scraping repo's `temporal_v2.registry.REGISTRY` is the source of truth for
"given (platform, module), which runner config and spider class is in charge".
This tool exposes that registry to the agent so it doesn't have to grep blindly.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from rca_agent.core.schemas import ToolName
from rca_agent.tools.base import BaseTool, ToolError


_REGISTRY_CACHE: tuple | None = None


def _load_registry():
    """Load temporal_v2.registry.

    Runner modules sometimes open data files via relative paths
    (e.g. ``open("blinkit_categories_subcat.csv")``). Those resolve
    against the *current working directory*, not the runner's directory.
    Temporarily chdir to the scraping repo root while we import.
    """
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE

    import os
    from rca_agent.core.settings import settings as _s

    cwd_before = os.getcwd()
    try:
        os.chdir(_s().SCRAPING_REPO_ROOT)
        from temporal_v2.registry import REGISTRY  # noqa: WPS433
        from temporal_v2.contracts import Platform, Module  # noqa: WPS433
    finally:
        os.chdir(cwd_before)
    _REGISTRY_CACHE = (REGISTRY, Platform, Module)
    return _REGISTRY_CACHE


def _all_pairs() -> list[tuple[str, str]]:
    REGISTRY, _, _ = _load_registry()
    return [(p.value, m.value) for (p, m) in REGISTRY.keys()]


def _resolve_pair(platform: str, module: str):
    REGISTRY, Platform, Module = _load_registry()
    try:
        plat = Platform(platform.lower())
    except ValueError as e:
        raise ToolError(
            f"Unknown platform {platform!r}. Valid: {[p.value for p in Platform]}"
        ) from e
    try:
        mod = Module(module.lower())
    except ValueError as e:
        raise ToolError(
            f"Unknown module {module!r}. Valid: {[m.value for m in Module]}"
        ) from e
    if (plat, mod) not in REGISTRY:
        raise ToolError(
            f"({platform}, {module}) is not registered. "
            f"Available pairs: {_all_pairs()}"
        )
    return REGISTRY[(plat, mod)]


def _config_summary(get_config) -> dict[str, Any]:
    """Best-effort summary of a runner config without executing it."""
    summary: dict[str, Any] = {
        "runner_module": getattr(get_config, "__module__", None),
        "qualname": getattr(get_config, "__qualname__", None),
    }
    # Try to actually call get_config() with no args to inspect it.
    cfg: Any = None
    try:
        cfg = get_config()
    except TypeError:
        try:
            cfg = get_config(None)
        except Exception as e:
            summary["call_error"] = f"get_config() not callable without args: {e}"
    except Exception as e:
        summary["call_error"] = str(e)

    if cfg is not None:
        for attr in (
            "spider_cls",
            "spider_name",
            "is_batch",
            "scrapy_settings",
            "items_cls",
            "item_cls",
            "feed_uri",
            "feed_format",
            "concurrency",
            "task_queue",
        ):
            try:
                v = getattr(cfg, attr, None)
                if v is not None and not callable(v):
                    summary[attr] = repr(v)[:500]
            except Exception:
                pass
        # Pull spider class info
        spider_cls = getattr(cfg, "spider_cls", None) or getattr(cfg, "spider", None)
        if spider_cls is not None:
            try:
                import inspect
                summary["spider_module"] = spider_cls.__module__
                summary["spider_qualname"] = spider_cls.__qualname__
                spider_file = inspect.getsourcefile(spider_cls)
                if spider_file:
                    from rca_agent.core.settings import settings as _s
                    try:
                        rel = str(spider_file)
                        repo = str(_s().SCRAPING_REPO_ROOT.resolve())
                        if rel.startswith(repo):
                            rel = rel[len(repo) + 1:]
                        summary["spider_file"] = rel
                    except Exception:
                        summary["spider_file"] = spider_file
            except Exception as e:
                logger.debug("spider class introspection failed: {}", e)
    return summary


class RegistryResolveTool(BaseTool):
    name = ToolName.REGISTRY_RESOLVE
    description = (
        "Resolve a (platform, module) pair to the runner config registered in "
        "temporal_v2.registry.REGISTRY. Returns the runner module path, spider "
        "class, spider source file, and key config attributes. "
        "If platform/module are omitted, returns the full list of registered pairs."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "platform": {"type": "string", "description": "e.g. blinkit, amazon_uae"},
            "module": {"type": "string", "description": "search | pdp | brand | product_listing"},
        },
    }

    def _execute(self, args: dict[str, Any]) -> dict[str, Any]:
        platform = (args.get("platform") or "").strip()
        module = (args.get("module") or "").strip()
        if not platform or not module:
            pairs = _all_pairs()
            return {
                "registered_pairs": pairs,
                "rows_count": len(pairs),
                "note": "Provide platform AND module to resolve a specific entry.",
            }
        get_config = _resolve_pair(platform, module)
        return {
            "platform": platform,
            "module": module,
            "config": _config_summary(get_config),
            "rows_count": 1,
        }
