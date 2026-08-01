#!/usr/bin/env python3
"""Backward-compatible entrypoint for the Agent Orchestrator dashboard."""

from agent_orchestrator.dashboard import create_app, main

__all__ = ["create_app", "main"]


if __name__ == "__main__":
    main()
