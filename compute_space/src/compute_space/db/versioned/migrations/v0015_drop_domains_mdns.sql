-- v15: drop ``domains.mdns``.
--
-- Whether a domain is published over mDNS now follows from its name (every
-- ``.local`` name is, and nothing else), so the stored flag is redundant and
-- could only ever disagree with the name.

ALTER TABLE domains DROP COLUMN mdns;
