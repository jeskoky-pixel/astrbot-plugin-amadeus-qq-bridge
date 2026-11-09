"""Thin compatibility boundary for AstrBot 4.29.4."""
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
from astrbot.core.utils.active_event_registry import active_event_registry

__all__ = ["FunctionTool", "ToolSet", "MainAgentBuildConfig", "build_main_agent", "active_event_registry"]
