# Forward recovery

Verify that all three exact admission function signatures are absent, the
governed package and registry hashes still match, the route-event SELECT grant
to `memory_v5_writer` is present, and every forced-RLS dependency is unchanged.
Then reapply the three exact forward artifacts in manifest order in one
transaction using the package advisory lock and timeouts. Revalidate each
definition, owner, search path, ACL, prior-absence rollback, exact replay,
cross-owner denial, zero provider and Qdrant access, and deterministic
reapplication before any production execution is authorized.
