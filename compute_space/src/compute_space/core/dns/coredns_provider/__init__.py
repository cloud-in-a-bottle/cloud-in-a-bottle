"""The router's own DNS: CoreDNS, the zones it answers for, and the records it serves.

Import from ``interface`` — it is the whole of what this package offers.  Nothing in here reaches
back into the application: the compute space hands the provider its settings and its zone set, and
gets an object it can start, stop, and write records to.
"""
