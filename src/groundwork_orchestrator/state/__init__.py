"""Durable state: Cosmos client, tenant-scoped repositories, and the audit-before-action gate.

Container names and partition keys here must match ``infra/modules/cosmos.bicep`` exactly — this
package does not create containers, it only talks to ones that already exist with a declared
partition key path of ``/tenantId``.
"""
