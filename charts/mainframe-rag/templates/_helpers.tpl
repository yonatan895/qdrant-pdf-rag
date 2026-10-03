{{/*
Shared helpers for mainframe-rag.
Keep this file small: no general-purpose PodSpec framework (stop condition).
Protected implementation defaults (selectors, ports, probes, keys) stay in
the resource templates, not in values.
*/}}
{{- define "mainframe-rag.image" -}}
{{- $tagHint := ternary " (full git SHA)" "" (has .name (list "agent" "ingest")) -}}
{{ required (printf "images.%s.repository is required" .name) .image.repository }}{{ with .image.digest }}@{{ . }}{{ else }}:{{ required (printf "images.%s.tag is required%s" $.name $tagHint) $.image.tag }}{{ end }}
{{- end -}}

{{- define "mainframe-rag.qdrantUrl" -}}
http://{{ required "qdrantRelease is required" .Values.qdrantRelease }}:6333
{{- end -}}

{{- define "mainframe-rag.qdrantSecretName" -}}
{{ required "qdrantRelease is required" .Values.qdrantRelease }}-apikey
{{- end -}}

{{/*
Export destination (issue #568). An empty endpoint selects the bundled
Jaeger Service and is valid only while that backend renders; with the
bundled backend disabled the operator must name a collector (any http(s)
URL, the former hostname included) or turn tracing off.
*/}}
{{- define "mainframe-rag.otelEndpoint" -}}
{{- if .Values.tracing.enabled -}}
{{- if .Values.tracing.endpoint -}}
{{ .Values.tracing.endpoint }}
{{- else if include "mainframe-rag.jaegerEnabled" . -}}
http://jaeger:4318
{{- else -}}
{{- fail "tracing.jaeger.enabled=false needs an intentional trace destination: set tracing.endpoint to the external collector's http(s) URL, or tracing.enabled=false" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Decoupled backend choices (issue #529 OBS-2): whether to deploy the bundled
Jaeger and whether to render the ServiceMonitor are independent of export
and exposition. A null sub-flag follows its parent leg (the historical
coupling); an explicit boolean overrides. kindIs (not default) reads the
override: Helm's default treats an explicit false as empty. Contradictions
fail the render before any mutation. Renders "true" or nothing so callers
use {{- if include ... }} directly (a "false" string would be truthy).
*/}}
{{- define "mainframe-rag.jaegerEnabled" -}}
{{- $enabled := .Values.tracing.enabled -}}
{{- if kindIs "bool" .Values.tracing.jaeger.enabled -}}
{{- $enabled = .Values.tracing.jaeger.enabled -}}
{{- end -}}
{{- if and $enabled (not .Values.tracing.enabled) -}}
{{- fail "tracing.jaeger.enabled=true requires tracing.enabled=true: refusing to deploy a bundled Jaeger nothing exports to" -}}
{{- end -}}
{{- if $enabled -}}true{{- end -}}
{{- end -}}

{{- define "mainframe-rag.serviceMonitorEnabled" -}}
{{- $enabled := .Values.metrics.enabled -}}
{{- if kindIs "bool" .Values.metrics.serviceMonitor.enabled -}}
{{- $enabled = .Values.metrics.serviceMonitor.enabled -}}
{{- end -}}
{{- if and $enabled (not .Values.metrics.enabled) -}}
{{- fail "metrics.serviceMonitor.enabled=true requires metrics.enabled=true: refusing to monitor a disabled exposition endpoint" -}}
{{- end -}}
{{- if $enabled -}}true{{- end -}}
{{- end -}}
