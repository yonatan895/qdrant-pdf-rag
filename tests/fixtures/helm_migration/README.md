# Temporary Kustomize migration oracle

`deploy.sh` and `ingest.sh` are the first-party producers from commit
`f3507a9d42c1e4b477d5804b48b315ba3a52df3f`. The shadow comparison suite runs
these copies against the retained Kustomize trees, keeping the old producer
independent after the operational scripts move to Helm.

These are test fixtures, not supported operator launchers. Remove them with
H4 after candidate-bound internal qualification and mapping the parity checks
to retained behavior tests under issue #448.
