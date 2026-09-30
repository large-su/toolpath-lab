"""Exceptions shared by every layer."""

from __future__ import annotations


class ToolpathLabError(Exception):
    """Base class for every error raised by ToolpathLab."""


class ParameterError(ToolpathLabError, ValueError):
    """A caller supplied parameter is missing, malformed or out of range."""


class PlanningError(ToolpathLabError, RuntimeError):
    """A toolpath could not be generated from the given inputs."""


class RegistryError(ToolpathLabError, KeyError):
    """An unknown tool, region shape, surface or planner identifier was requested."""
