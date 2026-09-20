{{/*
Shared helpers for mainframe-rag (issue #448 H1b).
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
