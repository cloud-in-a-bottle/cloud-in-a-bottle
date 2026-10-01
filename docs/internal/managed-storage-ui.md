# Managed archive usage UI

The archive settings show a cloud-storage allowance panel only for a persisted managed-storage binding matching the active S3 bucket and endpoint. An arbitrary R2 endpoint or bucket name does not establish that binding.

The browser calls the owner-authenticated local GET /api/storage/managed_usage endpoint. The router uses its existing Imbue client credentials to obtain a Keycloak access token and fetch the backend's stored usage snapshot. Credentials never reach the browser. Usage requests do not query Cloudflare, allocate buckets, or change permissions.

The companion hosted-spaces endpoint is GET /api/storage/allocations/{allocation_id}/usage. Tokens must have audience bottle-storage and a fixed, issuer-assigned storage_allocation_id claim matching the requested allocation. A domain claim alone is insufficient: the existing Connect flow accepts asserted domains. Trusted provisioning must attach the audience and allocation claim, and must never let the generic Connect flow assign or transfer that claim.

The local managed_storage_binding setting contains allocation_id, service_url, s3_bucket and s3_endpoint. Trusted provisioning writes it after assigning the allocation. The UI work does not automatically enroll instances or migrate data. Changing to a different bucket/endpoint hides the panel even if an old binding remains in settings.

The version-1 snapshot contains allocation_id, phase, capacity_bytes, desired_access, applied_access, reason, enforcement_enabled, stale, observed_at, applied_at, reported_at, and a nullable usage object. Usage contains used_bytes, operation_microcents, storage_microcents, read_only_at_microcents, suspend_at_microcents, sample_at, period_start and resets_at. Timestamps are Unix seconds; period dates use UTC. Monetary values are estimated provider cost in microcents, not customer charges.

The UI distinguishes initial loading, usage unavailable, stale observations, provisioning, available access, approaching limits, read-only, suspended, pending permission changes, and observation-only enforcement. Missing usage is never rendered as zero. Refresh failure preserves the last snapshot with an explicit stale warning and a retry action. Dynamic values are inserted as text. Capacity and activity allowances are separate; a monthly activity reset does not reset stored bytes.

Deploy the hosted-spaces usage API after its allocation backend, then release this Bottle UI. Before activating the panel on an instance, trusted provisioning must configure the allocation-bound Keycloak claim and persist the matching managed-storage binding.
