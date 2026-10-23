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
{{- else if include "mainframe-rag.openobserveEnabled" . -}}
http://openobserve:5080/api/default
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
{{- if eq (include "mainframe-rag.tracingBackend" .) "openobserve" -}}
{{- if eq (.Values.tracing.jaeger.enabled | toString) "true" -}}
{{- fail "tracing.jaeger.enabled=true contradicts tracing.backend=openobserve: one bundled trace backend" -}}
{{- end -}}
{{- $enabled = false -}}
{{- end -}}
{{- if kindIs "bool" .Values.tracing.jaeger.enabled -}}
{{- $enabled = .Values.tracing.jaeger.enabled -}}
{{- end -}}
{{- if and $enabled (not .Values.tracing.enabled) -}}
{{- fail "tracing.jaeger.enabled=true requires tracing.enabled=true: refusing to deploy a bundled Jaeger nothing exports to" -}}
{{- end -}}
{{- if $enabled -}}true{{- end -}}
{{- end -}}

{{/*
Trace backend selector (issue #956). Default jaeger; the schema enum is the
only other value. A missing key reads as jaeger so existing values keep
rendering byte-identically.
*/}}
{{- define "mainframe-rag.tracingBackend" -}}
{{- .Values.tracing.backend | default "jaeger" -}}
{{- end -}}

{{/*
Bundled OpenObserve (issue #956, ADR-0006): "true" or nothing. Needs tracing
on (nothing else exports to it) and an empty tracing.endpoint (#568 rule:
a bundled backend only when the endpoint is the bundled one).
*/}}
{{- define "mainframe-rag.openobserveEnabled" -}}
{{- if and (eq (include "mainframe-rag.tracingBackend" .) "openobserve") .Values.tracing.enabled -}}
{{- if .Values.tracing.endpoint -}}
{{- fail "tracing.backend=openobserve renders the bundled backend: leave tracing.endpoint empty" -}}
{{- end -}}
true
{{- end -}}
{{- end -}}

{{- define "mainframe-rag.openobserveSecretName" -}}
{{ required "openobserve.credentialsSecretName is required (Secret with root-user-email, root-user-password, otlp-headers)" .Values.openobserve.credentialsSecretName }}
{{- end -}}

{{/* Digest-only image reference; a tag is never accepted (ADR-0006). */}}
{{- define "mainframe-rag.openobserveImage" -}}
{{ required "images.openobserve.repository is required" .Values.images.openobserve.repository }}@{{ required "images.openobserve.digest is required: set the pinned sha256 digest (pin PR, issue #956)" .Values.images.openobserve.digest }}
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

{{/*
Feedback service (issue #804, ADR-0005). Default off; "true" or nothing, like
the helpers above. The content store's ingress limit is the agent-only
NetworkPolicy, so enabling the service without that policy would leave the
bearer token as the only control: refuse at render, before any mutation.
*/}}
{{- define "mainframe-rag.feedbackEnabled" -}}
{{- if .Values.feedback.enabled -}}
{{- if not .Values.networkPolicy.enabled -}}
{{- fail "feedback.enabled=true requires networkPolicy.enabled=true: the feedback store's agent-only ingress limit is the NetworkPolicy; refusing to expose it on a bearer token alone" -}}
{{- end -}}
{{- if not .Values.feedback.secretName -}}
{{- fail "feedback.secretName is required when feedback.enabled=true" -}}
{{- end -}}
true
{{- end -}}
{{- end -}}
