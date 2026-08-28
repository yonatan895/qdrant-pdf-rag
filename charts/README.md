# Vendored Qdrant Helm chart

`qdrant-1.19.0.tgz` was pulled with `helm pull qdrant/qdrant` on a connected
host (architecture.md §3.3). The air-gap never runs `helm repo add`.

The chart is Apache-2.0 licensed by the Qdrant project; the upstream license
and notices are preserved inside the tgz (`qdrant/Chart.yaml`, sources at
https://github.com/qdrant/qdrant-helm). This vendoring is unmodified.

To refresh: `make pull-chart` on a connected host, then re-run
`make helm-lint helm-template` and commit the new tgz with the pin change.
