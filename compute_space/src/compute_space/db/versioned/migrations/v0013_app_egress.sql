-- v13: per-app egress routing state.
--
-- When an app declares ``[runtime.container].egress = "<profile>"`` its
-- outbound traffic is forced through a WireGuard tunnel and it runs inside an
-- infra netns with no datacenter route (fail-closed).  The router then reaches
-- the app over a host<->netns veth instead of loopback, so we persist:
--
--   egress_profile  - the profile name the app requested ('' = normal egress).
--   ingress_index   - small per-app index selecting the veth /30
--                     (10.199.<index>.0/30); NULL for non-egress apps.
--   upstream_host    - the address the reverse proxy / health checks connect
--                     to.  '127.0.0.1' for normal apps (loopback publish);
--                     the netns veth IP (10.199.<index>.2) for egress apps.
--                     NULL is treated as '127.0.0.1' by the proxy.
--
-- Existing rows default to non-egress: empty profile, no index, loopback host.

ALTER TABLE apps ADD COLUMN egress_profile TEXT NOT NULL DEFAULT '';
ALTER TABLE apps ADD COLUMN ingress_index INTEGER;
ALTER TABLE apps ADD COLUMN upstream_host TEXT;
