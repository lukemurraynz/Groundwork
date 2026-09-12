#!/usr/bin/env pwsh
# Groundwork predeploy check (docs/waf-assessment.md §1.4).
#
# Advisory only, never blocking: warns if a deployment is currently EXECUTING before `azd deploy`
# restarts the pod. Auto-recovery already handles this correctly; this just gives the operator a
# chance to wait if it's convenient. Always exits 0 - even a failure to run this check (Cosmos
# unreachable, credential issue) must never block a legitimate deploy over an advisory warning.

$python = if (Test-Path '.venv/Scripts/python.exe') { '.venv/Scripts/python.exe' } else { 'python' }
& $python scripts/check_in_flight_deployments.py
exit 0
