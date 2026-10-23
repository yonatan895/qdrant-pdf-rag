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
Export destination. An empty tracing.endpoint selects the bundled OpenObserve
(ADR-0006); a set endpoint is the generic OTLP/HTTP escape hatch (external
collector, no bundled backend and no collector). Tracing off exports nowhere.
*/}}
{{- define "mainframe-rag.otelEndpoint" -}}
{{- if .Values.tracing.enabled -}}
{{- if .Values.tracing.endpoint -}}
{{ .Values.tracing.endpoint }}
{{- else -}}
http://openobserve:5080/api/default
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Bundled OpenObserve: "true" or nothing (a "false" string would be truthy).
Needs tracing on and an empty tracing.endpoint.
*/}}
{{- define "mainframe-rag.openobserveEnabled" -}}
{{- if and .Values.tracing.enabled (not .Values.tracing.endpoint) -}}true{{- end -}}
{{- end -}}

{{- define "mainframe-rag.openobserveSecretName" -}}
{{ required "openobserve.credentialsSecretName is required (Secret with root-user-email, root-user-password, otlp-headers, metrics-auth)" .Values.openobserve.credentialsSecretName }}
{{- end -}}

{{/*
Metrics collector: "true" or nothing. Null metrics.collector.enabled follows
(bundled OpenObserve and metrics.enabled); an explicit boolean overrides, and
true without both prerequisites fails the render (a collector nothing scrapes
or nothing receives).
*/}}
{{- define "mainframe-rag.otelcolEnabled" -}}
{{- $prereq := and .Values.metrics.enabled (include "mainframe-rag.openobserveEnabled" .) -}}
{{- $enabled := $prereq -}}
{{- if kindIs "bool" .Values.metrics.collector.enabled -}}
{{- $enabled = .Values.metrics.collector.enabled -}}
{{- end -}}
{{- if and $enabled (not $prereq) -}}
{{- fail "metrics.collector.enabled=true requires metrics.enabled=true and the bundled OpenObserve (tracing.enabled=true, empty tracing.endpoint)" -}}
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
