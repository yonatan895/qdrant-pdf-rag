{{/*
Shared helpers for mainframe-rag.
Keep this file small: no general-purpose PodSpec framework (stop condition).
Protected implementation defaults (selectors, ports, probes, keys) stay in
the resource templates, not in values.
*/}}
{{- define "mainframe-rag.labels" -}}
app: rag-agent
{{- end -}}

{{- define "mainframe-rag.selectorLabels" -}}
app: rag-agent
{{- end -}}

{{- define "mainframe-rag.agentImage" -}}
{{ required "images.agent.repository is required" .Values.images.agent.repository }}:{{ required "images.agent.tag is required (full git SHA)" .Values.images.agent.tag }}
{{- end -}}

{{- define "mainframe-rag.ingestImage" -}}
{{ required "images.ingest.repository is required" .Values.images.ingest.repository }}:{{ required "images.ingest.tag is required (full git SHA)" .Values.images.ingest.tag }}
{{- end -}}

{{- define "mainframe-rag.jaegerImage" -}}
{{ required "images.jaeger.repository is required" .Values.images.jaeger.repository }}:{{ required "images.jaeger.tag is required" .Values.images.jaeger.tag }}
{{- end -}}

{{- define "mainframe-rag.oauthProxyImage" -}}
{{ required "images.oauthProxy.repository is required" .Values.images.oauthProxy.repository }}:{{ required "images.oauthProxy.tag is required" .Values.images.oauthProxy.tag }}
{{- end -}}

{{- define "mainframe-rag.qdrantUrl" -}}
http://{{ required "qdrantRelease is required" .Values.qdrantRelease }}:6333
{{- end -}}

{{- define "mainframe-rag.qdrantSecretName" -}}
{{ required "qdrantRelease is required" .Values.qdrantRelease }}-apikey
{{- end -}}

{{- define "mainframe-rag.otelEndpoint" -}}
{{- if .Values.tracing.enabled -}}
{{ required "tracing.endpoint is required when tracing.enabled is true" .Values.tracing.endpoint }}
{{- else -}}
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
