"""Workflow orchestration boundaries.

Keep package import side-effect free: Temporal imports deterministic workflow
definitions inside its sandbox and must not transitively load the local
orchestrator, HTTP clients, LLM SDKs, or database code.
"""
