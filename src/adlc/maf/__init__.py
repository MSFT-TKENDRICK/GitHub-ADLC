"""Governed agent invocation for ADLC (Microsoft Agent Framework + AGT).

MAF is **not** an orchestrator here. The ADLC spine runs the task DAG with its
own topological asyncio executor. This package exists solely so that when an
agent does call a tool, an Agent Governance Toolkit policy decision happens
first, in deterministic application code, before the call reaches the wire.

See ``docs/governance.md``.
"""

from __future__ import annotations

from adlc.maf.middleware import (
    AGT_INSTALL_HINT,
    MAF_INSTALL_HINT,
    DecisionRecord,
    GovernanceBlocked,
    GovernanceDecision,
    GovernanceMiddleware,
    GovernanceUnavailable,
    PolicyEngine,
    detect_agt,
    detect_governance,
    detect_maf,
    govern_tools,
    governance_function_middleware,
    normalize_verdict,
    resolve_policy_path,
)

__all__ = [
    "AGT_INSTALL_HINT",
    "MAF_INSTALL_HINT",
    "DecisionRecord",
    "GovernanceBlocked",
    "GovernanceDecision",
    "GovernanceMiddleware",
    "GovernanceUnavailable",
    "PolicyEngine",
    "detect_agt",
    "detect_governance",
    "detect_maf",
    "govern_tools",
    "governance_function_middleware",
    "normalize_verdict",
    "resolve_policy_path",
]
