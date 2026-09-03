# Latency notes

*Internal notes, not part of the manual.*

## Single-server latency optimizations

centralized, global web services do some things to get latency down:
- multiple servers around the world, with routing to get users to the closest one
- if they don't do that, they'll do something like have cloudflare terminate TLS at the edge and reverse proxy back to the origin server. this cuts down on roundtrips to negotiate TLS. but this lets cloudflare see all the traffic.


for a single server setup, there's some optimizations you can do:
- OCSP stapling: some clients will add a check that the cert isn't revoked before accepting it. OCSP stapling lets the server check the OCSP status itself and "staple" it to the TLS handshake, so the client doesn't have to do a separate request to the CA's OCSP server.
- TLS session resumption: after the first TLS handshake, the client and server can cache the session parameters. then on subsequent connections, they can do a shorter handshake that just references the cached session, which saves roundtrips. this is tricky because it is only properly secure on GET requests.
- TLS 1.3 has less roundtrips
- HTTP/3 has less roundtrips
- use fast ECDSA P-256 keys (we do this — see `compute_space/src/compute_space/core/tls/util.py`)
