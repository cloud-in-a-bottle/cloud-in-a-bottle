-- v14: drop domains.mdns; mDNS is derived from the .local name (is_local_name), not stored.
ALTER TABLE domains DROP COLUMN mdns;
