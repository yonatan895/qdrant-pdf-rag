# Vendored Qdrant Helm chart

`qdrant-1.19.0.tgz` was pulled with `helm pull qdrant/qdrant` on a connected
host (architecture.md §3.1). The air-gap never runs `helm repo add`.

The chart is Apache-2.0 licensed by the Qdrant project; the upstream license
and notices are preserved inside the tgz (`qdrant/Chart.yaml`, sources at
<https://github.com/qdrant/qdrant-helm>). This vendoring is unmodified.

To refresh: `sh scripts/tools/run-task.sh artifacts:chart-fetch` on a connected host, then re-run
`sh scripts/tools/run-task.sh artifacts:helm-lint artifacts:helm-render` and commit the new tgz with the pin change.
