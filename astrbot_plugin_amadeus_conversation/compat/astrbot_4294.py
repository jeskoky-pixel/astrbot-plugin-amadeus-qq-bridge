"""Compatibility boundary for AstrBot 4.29.4 private APIs."""
from __future__ import annotations

from typing import Any

SUPPORTED_ASTRBOT_VERSION = "4.29.4"


def assert_compatible() -> None:
    import astrbot

    version = getattr(astrbot, "__version__", None)
    if version and str(version) != SUPPORTED_ASTRBOT_VERSION:
        raise RuntimeError(
            f"astrbot_plugin_amadeus_conversation requires AstrBot {SUPPORTED_ASTRBOT_VERSION}; "
            f"detected {version}"
        )


def get_data_dir(plugin_name: str) -> str:
    from astrbot.core.star.star_tools import StarTools

    return str(StarTools.get_data_dir(plugin_name))


def message_components() -> dict[str, Any]:
    from astrbot.core.message import components

    return {name: getattr(components, name) for name in (
        "At", "File", "Forward", "Image", "Json", "Plain", "Record", "Reply", "Video"
    )}


from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
from astrbot.core.utils.active_event_registry import active_event_registry

__all__ = ["FunctionTool", "ToolSet", "MainAgentBuildConfig", "build_main_agent", "active_event_registry"]
