"""Plan-time cost and licensing presentation (control-plane side).

The estimator itself lives in :mod:`groundwork_shared.costing` (moved there by T075a so the
orchestrator's execution-time cost re-check could call it without importing the control plane).
This package is what stayed control-plane-only: presentation-layer costing concerns no executor
ever needs.
"""
