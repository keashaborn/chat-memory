# Retired Memory V1 inference topology

These units describe the retired `seebx -> RESSE -> resse-train` inference
path and the RESSE CPU fallback. They are retained for audit and disaster
recovery only.

Production cut over on 2026-07-19 to:

`seebx -> VS-Memory-GPU (172.31.41.29)`

The active seebx tunnel is
`ops/systemd/memory-v1-v5-local-inference-tunnel.service`. The active GPU
runtime is `ops/systemd/vs-memory-gpu-inference.service`.

Do not install or enable the retired units without an explicit rollback plan.
