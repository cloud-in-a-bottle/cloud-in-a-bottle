# Test Cases

Test cases for the OpenHost platform. Automated tests are in:
- `test_e2e.py` — cloud E2E (ephemeral GCE instance, real TLS)
- `test_full_stack.py` — local full-stack (router on host + rootless podman, no VM needed)
- `test_tls.py` — TLS cert acquisition (Pebble ACME + CoreDNS, no VM needed)
- `compute_space/tests/test_integration.py` — podman integration (router on host)

Legend: [x] = automated, [ ] = manual/not yet automated

## Provider

### Deploy & Setup Resilience
- [ ] `deploy.yml` does not overwrite provider DB
- [ ] `setup.yml` does not overwrite provider DB
- [ ] Running VMs survive a provider restart
- [ ] Running VMs survive a setup

### Admin
- [ ] First user is automatically admin
- [ ] Admin can view all users and VM statuses
- [ ] Admin can stop a running VM
- [ ] Admin can toggle admin status of other users
- [ ] Admin cannot remove their own admin status
- [ ] Admin can delete a user (stops VM, removes files, removes from DB)
- [ ] Admin can impersonate a user ("View as" -> sees their dashboard)
- [ ] Impersonation ends on logout

### User Accounts
- [ ] Signup via invite link creates user and sets auth cookies
- [x] Login with email/password sets auth cookies — `test_full_stack.py::TestLoginLogout`, `test_e2e.py::test_11b`
- [x] Expired access tokens are transparently refreshed — `test_integration.py::TestRouterCore::test_expired_token_refresh`
- [x] Logout revokes refresh token and clears cookies — `test_full_stack.py::TestLoginLogout`, `test_e2e.py::test_11c`
- [ ] Reserved usernames are rejected at signup

### VM Lifecycle
- [ ] VM boots and reaches "running" status
- [ ] VM port allocation is race-free (vm_ports table with UNIQUE constraint)
- [ ] VM ports don't collide between users
- [ ] Provider recovers VM states on restart
- [ ] Provider marks orphaned VMs as stopped

## Router (inside VM or standalone)

### Setup Flow
- [ ] Fresh VM: redirects to /setup
- [x] /setup inits DB, creates owner — `test_e2e.py::test_02`, `test_full_stack.py::admin_session`
- [x] /setup returns 403 if instance is already set up — `test_integration.py::TestRouterCore`
- [x] After /setup, all routes work normally — `test_e2e.py::test_03b`, `test_full_stack.py::TestRouter`

### Auth
- [x] /setup sets auth cookies (auto-login after setup) — `test_e2e.py::test_02`, `test_full_stack.py::TestRouter`
- [x] /dashboard requires authentication — `test_e2e.py::test_03`, `test_full_stack.py::TestRouter`
- [x] Unauthenticated requests redirect to /login — `test_e2e.py::test_03`, `test_full_stack.py::TestRouter`
- [x] Claim token gates /setup and is deleted after successful claim — `test_integration.py::test_claim_token_gate_and_delete`
- [x] /setup accepts callers when claim_token_required=False — `test_integration.py::test_setup_open_when_claim_token_not_required`
- [x] Login with wrong password rejected — `test_e2e.py::test_11`, `test_full_stack.py::TestLoginLogout`
- [x] Login with correct password sets cookies — `test_e2e.py::test_11b`, `test_full_stack.py::TestLoginLogout`
- [x] Logout clears session — `test_e2e.py::test_11c`, `test_full_stack.py::TestLoginLogout`

### API Tokens
- [x] Create API token — `test_e2e.py::test_10`, `test_full_stack.py::TestAPITokens`
- [x] Use token with Bearer header (no cookies) — `test_e2e.py::test_10b`, `test_full_stack.py::TestAPITokens`
- [x] Invalid token rejected — `test_e2e.py::test_10c`, `test_full_stack.py::TestAPITokens`
- [x] Delete token and verify invalidated — `test_e2e.py::test_10d`, `test_full_stack.py::TestAPITokens`
- [x] Token with no expiry works — `test_integration.py::test_api_token_no_expiry`

### TLS
- [x] TLS cert acquired via ACME (DNS-01 for wildcard) — `test_e2e.py::test_13`, `test_tls.py::TestCertAcquisition`
- [x] Wildcard cert covers app subdomains — `test_e2e.py::test_13b`, `test_tls.py::TestCertAcquisition::test_cert_covers_wildcard`
- [x] Cert uses ECDSA P-256 key — `test_tls.py::TestCertAcquisition::test_cert_uses_ecdsa_p256`
- [x] Cert and key are a matching pair — `test_tls.py::TestCertAcquisition::test_cert_key_match`
- [x] DNS TXT records cleaned up after acquisition — `test_tls.py::TestCertAcquisition::test_dns_txt_records_cleaned_up`
- [x] acquire_tls_cert writes files with correct permissions — `test_tls.py::TestAcquireTlsCert`
- [x] ACME account key round-trip (generate, save, load) — `test_tls.py::TestAccountKey`
- [ ] Router serves HTTPS in TLS mode — TODO: needs non-privileged Caddy port support
- [x] HTTP on :80 redirects to HTTPS (except ACME challenges) — `test_integration.py::test_caddyfile_http_redirect`

### Security (prod mode)
- [x] Pre-setup audit passes — `test_integration.py::test_pre_setup_security_audit`
- [x] Post-setup audit passes — `test_integration.py::TestRouterCore::test_post_setup_security_audit`
- [ ] SSH port not forwarded in prod mode
- [ ] SSH port forwarded in dev mode
- [x] Security audit endpoint returns results — `test_full_stack.py::TestStorageAndSystem`

### App Deployment
- [x] Deploy app from local path — `test_e2e.py::test_04`, `test_full_stack.py::TestTestAppPathRouting`
- [x] Deploy app from Git URL — `test_integration.py::TestGitUrlDeployE2E`
- [ ] Deploy app from private GitHub repo (with token)
- [ ] Serverless app (Spin WASM) builds and starts
- [x] Container app builds and starts — `test_e2e.py::test_05`, `test_full_stack.py::TestTestAppPathRouting`
- [x] App reaches "running" status — `test_e2e.py::test_05`, `test_full_stack.py`
- [x] App removal stops process and cleans up — `test_e2e.py::test_14`, `test_integration.py::TestContainerE2E`

### App Lifecycle
- [x] Stop app — `test_e2e.py::test_08`, `test_full_stack.py::TestAppLifecycle`
- [x] Reload app (rebuild + restart) — `test_e2e.py::test_08b`, `test_full_stack.py::TestAppLifecycle`
- [x] Rename app, routing updates — `test_full_stack.py::TestAppRename`
- [x] Container engine restart / gone recovery — `test_integration.py::TestContainerRestart`, `test_check_app_status.py`
- [x] Remove with keep_data preserves persistent data — `test_integration.py::TestRemoveKeepData`
- [x] Git-deployed app: clones (not copies) via git — `test_integration.py::TestGitUrlDeployE2E`
- [x] Git-deployed app: reload does git pull — `test_set_app_remote.py::test_git_pull_switches_branch_via_ref`

### App Routing (path-based)
- [x] Requests to /base_path/ are proxied to the app — `test_e2e.py::test_06b`, `test_full_stack.py::TestTestAppPathRouting`
- [x] App health check works through proxy — `test_e2e.py::test_06`, `test_full_stack.py::TestTestAppPathRouting`
- [x] POST requests proxied with body — `test_e2e.py::test_06c`, `test_full_stack.py::TestTestAppPathRouting`
- [x] X-Forwarded-* headers set, spoofed values stripped — `test_e2e.py::test_06d`, `test_full_stack.py::TestTestAppPathRouting`
- [x] Unknown paths return 404 — `test_e2e.py::test_06e`, `test_full_stack.py::TestTestAppPathRouting`
- [x] Unknown app paths return 404 — `test_e2e.py::test_06f`

### App Routing (subdomain-based)
- [x] Requests to app.zone.domain are proxied — `test_e2e.py::test_07`, `test_full_stack.py::TestTestAppSubdomainRouting`
- [x] Subdomain root returns app metadata — `test_e2e.py::test_07b`
- [x] Unauthenticated requests to non-public paths rejected — `test_e2e.py::test_07c`, `test_full_stack.py::TestTestAppSubdomainRouting`

### WebSocket Proxy
- [x] WebSocket echo via subdomain routing — `test_e2e.py::test_12d`, `test_full_stack.py::TestWebSocketProxy`
- [x] WebSocket echo via path-based routing — `test_full_stack.py::TestWebSocketProxy`

### Multiple Apps
- [x] Deploy multiple apps concurrently — `test_e2e.py::test_09`, `test_full_stack.py::TestMultipleApps`
- [x] Both apps route independently — `test_e2e.py::test_09b`, `test_full_stack.py::TestMultipleApps`
- [x] Subdomain isolation between apps — `test_e2e.py::test_09c`, `test_full_stack.py::TestMultipleApps`
- [x] GET /api/apps lists all apps — `test_e2e.py::test_09d`, `test_full_stack.py::TestMultipleApps`
- [x] Remove one app, other still works — `test_e2e.py::test_09e`, `test_full_stack.py::TestMultipleApps`

### Storage & System
- [x] GET /api/storage-status returns disk info — `test_e2e.py::test_12`, `test_full_stack.py::TestStorageAndSystem`
- [x] GET /app_logs returns log content — `test_e2e.py::test_12b`, `test_full_stack.py::TestStorageAndSystem`
- [x] GET /api/compute_space_logs returns router logs — `test_e2e.py::test_12c`, `test_full_stack.py::TestStorageAndSystem`

### SSH Toggle (dev mode)
- [x] GET /api/ssh-status returns state — `test_full_stack.py::TestSSHToggle`
- [x] POST /toggle-ssh toggles SSH — `test_full_stack.py::TestSSHToggle`

### App Data
- [x] App data persists across remove+redeploy (keep_data) — `test_integration.py::TestRemoveKeepData`
- [x] App data cleaned up on full remove — `test_integration.py::TestContainerE2E`
- [x] SQLite databases are provisioned and accessible — `test_integration.py::test_sqlite_provisioning`

## DNS
- [ ] CoreDNS resolves base domain
- [ ] CoreDNS resolves zone subdomains
- [x] CoreDNS handles ACME DNS-01 challenges (prod mode) — `test_e2e.py::test_13` (implicit)
- [ ] DNS negative cache TTL is low enough for fast convergence

## Caddy (reverse proxy)
- [ ] TLS passthrough (SNI routing)
- [ ] HTTP reverse proxy
- [ ] Routes are updated when router starts/stops apps
- [ ] Routes are synced on router restart
