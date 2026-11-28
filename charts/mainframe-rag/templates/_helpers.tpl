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

{{/*
Open WebUI docs front end (ADR-0007, #1074). Default on (maintainer decision 2026-10-26); "true" or nothing.
The schema checks shapes only. Missing site inputs fail the render here with
one fixed message that names the missing values and the opt-out, so a
half-configured front end never renders. Also refused: no enforced retention
window, no repository-owned docs Pipe (#1072).
*/}}
{{/*
Open WebUI ratings (#1255), the one owner of the switch: "true" or nothing.
openwebui.ratings.enabled when it is a boolean (an explicit false stays off:
Helm's `default` would read false as unset), else feedback.enabled (#1274: on
wherever the feedback store is on). An explicit true without the store fails
in mainframe-rag.openwebuiEnabled.
*/}}
{{- define "mainframe-rag.openwebuiRatings" -}}
{{- $r := .Values.openwebui.ratings | default dict -}}
{{- if kindIs "bool" $r.enabled -}}
{{- if $r.enabled }}true{{ end -}}
{{- else if (.Values.feedback | default dict).enabled -}}
true
{{- end -}}
{{- end -}}

{{- define "mainframe-rag.openwebuiEnabled" -}}
{{- if .Values.openwebui.enabled -}}
{{- $o := .Values.openwebui -}}
{{- if and ($o.ratings | default dict).enabled (not .Values.feedback.enabled) -}}
{{- fail "openwebui.ratings.enabled=true requires feedback.enabled=true: Open WebUI ratings are stored only by the agent's feedback store (ADR-0005)" -}}
{{- end -}}
{{- $oidc := $o.oidc | default dict -}}
{{- $egress := $o.idpEgress | default dict -}}
{{- $missing := list -}}
{{- if not $o.secretName }}{{ $missing = append $missing "openwebui.secretName" }}{{ end -}}
{{- if not $o.host }}{{ $missing = append $missing "openwebui.host" }}{{ end -}}
{{- if not $oidc.providerUrl }}{{ $missing = append $missing "openwebui.oidc.providerUrl" }}{{ end -}}
{{- if not $oidc.clientId }}{{ $missing = append $missing "openwebui.oidc.clientId" }}{{ end -}}
{{- if not $oidc.allowedRoles }}{{ $missing = append $missing "openwebui.oidc.allowedRoles" }}{{ end -}}
{{- if not $oidc.adminRoles }}{{ $missing = append $missing "openwebui.oidc.adminRoles" }}{{ end -}}
{{- if not $egress.cidrs }}{{ $missing = append $missing "openwebui.idpEgress.cidrs" }}{{ end -}}
{{- if not (.Values.route.destinationCA | default "" | trim) }}{{ $missing = append $missing "route.destinationCA" }}{{ end -}}
{{- if $missing -}}
{{- fail (printf "Open WebUI is enabled by default (ADR-0008) but required site inputs are missing: %s. Set them, or opt out with --set openwebui.enabled=false (OPENWEBUI_ENABLED=false in airgap.env)." (join ", " $missing)) -}}
{{- end -}}
{{- $shape := list -}}
{{- if not $oidc.groupClaim }}{{ $shape = append $shape "openwebui.oidc.groupClaim" }}{{ end -}}
{{- if not $oidc.rolesClaim }}{{ $shape = append $shape "openwebui.oidc.rolesClaim" }}{{ end -}}
{{- if not ($oidc.emailClaim | default "" | trim) }}{{ $shape = append $shape "openwebui.oidc.emailClaim" }}{{ end -}}
{{- if not ($oidc.usernameClaim | default "" | trim) }}{{ $shape = append $shape "openwebui.oidc.usernameClaim" }}{{ end -}}
{{- if not ($oidc.scopes | default "" | trim) }}{{ $shape = append $shape "openwebui.oidc.scopes" }}{{ end -}}
{{- if not $egress.port }}{{ $shape = append $shape "openwebui.idpEgress.port" }}{{ end -}}
{{- if $shape -}}
{{- fail (printf "Open WebUI settings must not be empty or null: %s (a null port would open the IdP egress rule to every TCP port)." (join ", " $shape)) -}}
{{- end -}}
{{- /* Revocation through the IdP needs a roles claim on every sign-in, so the IdP
issues the baseline value signed-in to every user (docs/deploy.md, AD FS). It,
or *, in a role list would admit every signed-in user. */ -}}
{{- range $list := list ($oidc.allowedRoles | toString) ($oidc.adminRoles | toString) -}}
{{- range $role := splitList "," $list -}}
{{- if or (eq $role "*") (eq $role "signed-in") -}}
{{- fail "openwebui.oidc.allowedRoles and adminRoles must not contain * or the baseline role signed-in (every signed-in user carries it, so removing a user from the role groups would revoke nothing)" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- /* IdP CA: certificates only. A fixed message, never the value: a schema
pattern would make Helm print the rejected value (a pasted key included).
The ca-trust init step then loads every certificate (files/openwebui/ca_trust.py). */ -}}
{{- with $oidc.caBundle -}}
{{- if not (regexMatch "^\\s*(-----BEGIN CERTIFICATE-----\\r?\\n[A-Za-z0-9+/=\\r\\n]+-----END CERTIFICATE-----\\s*)+$" (toString .)) -}}
{{- fail "openwebui.oidc.caBundle must hold only PEM certificates (-----BEGIN CERTIFICATE----- blocks, no key or other text); the value is not shown" -}}
{{- end -}}
{{- end -}}
{{- if not $o.retentionDays -}}
{{- fail "openwebui.retentionDays is required when openwebui.enabled=true: the purge sidecar enforces the chat retention window (ADR-0007 S2)" -}}
{{- end -}}
{{- if not (.Files.Get "files/openwebui/docs_pipe.py") -}}
{{- fail "openwebui.enabled=true needs the repository-owned docs Pipe (#1072) at charts/mainframe-rag/files/openwebui/docs_pipe.py; refusing a front end without its only model" -}}
{{- end -}}
true
{{- end -}}
{{- end -}}

{{/*
The Open WebUI allow-list proxy config (ADR-0007 E1): the repository file
files/openwebui/proxy.nginx.conf verbatim when ratings and chat images are
on. Without ratings, the file loses the rating Action block (#1255), so the
default allow-list admits no /api/chat/actions path. Without chat images, it
loses the completions body-cap location (#1287), so the default render is the
pre-#1287 file byte for byte. Tests pin that each block occurs exactly once in
the file.
*/}}
{{- define "mainframe-rag.openwebuiProxyConf" -}}
{{- $conf := .Files.Get "files/openwebui/proxy.nginx.conf" -}}
{{- if not (include "mainframe-rag.openwebuiRatings" .) -}}
{{- $conf = replace (include "mainframe-rag.openwebuiRatingsAllow" .) "" $conf -}}
{{- end -}}
{{- if not (include "mainframe-rag.chatImages" .) -}}
{{- $conf = replace (include "mainframe-rag.openwebuiChatImagesLocation" .) "" $conf -}}
{{- end -}}
{{- $conf -}}
{{- end -}}

{{/*
Pasted images in Open WebUI temporary chats (#1287), the one owner of the
switch: "true" or nothing. True when models.reasoning.chatImagesEnabled is
true (it also renders the agent's CHAT_IMAGES_ENABLED) and Open WebUI is
enabled. Every Open WebUI template reads it: the image caps, the banner and
the Pipe switch when on, the not-vision-capable model default when off, and
the proxy's completions body cap.
*/}}
{{- define "mainframe-rag.chatImages" -}}
{{- if and .Values.openwebui.enabled (eq (toString ((.Values.models).reasoning).chatImagesEnabled) "true") -}}
true
{{- end -}}
{{- end -}}

{{- define "mainframe-rag.openwebuiChatImagesLocation" -}}
{{- print `
        # Pasted images (#1287), rendered only while chat images are on. The
        # browser resends every earlier image of a temporary chat each turn, so
        # the completions body carries the conversation's images: 8 images
        # (decision 7) x ceil(5242880 * 4 / 3) base64 bytes = 53.3 MiB, plus
        # the 1 MiB text cap above, is 54.3 MiB; rounded up to 55m. Every
        # directive of "location /" is repeated, the allow-list check included,
        # so the allow-list (E1) is unchanged: only this path's body cap grows.
        location = /api/chat/completions {
            client_max_body_size 55m;
            if ($owui_allowed = 0) {
                return 403 "denied\n";
            }
            proxy_pass http://owui_upstream;
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $owui_connection;
            proxy_buffering off;
            proxy_read_timeout 600s;
            proxy_send_timeout 600s;
        }
` -}}
{{- end -}}

{{- define "mainframe-rag.openwebuiRatingsAllow" -}}
{{- print "        # The repository rating Action's two buttons only (#1255): the handler\n" -}}
{{- print "        # checks the Function is an active, surfaced Action; its id is fixed.\n" -}}
{{- print "        ~^/api/chat/actions/mainframe_rag_rating\\.(up|down)$ 1;\n" -}}
{{- end -}}


{{/*
Team-tool gateways (ADR-0010, #1075). One helper per gateway decides and
validates its switch ("true" or nothing, like the helpers above); every
missing input fails the render with one fixed message naming keys, never
values. mainframe-rag.teamGateways lists the enabled gateways as JSON specs,
which the shared templates (team-gateways.yaml, the Open WebUI environment and
NetworkPolicy) range over, so a later gateway (GitLab, #1078) adds one spec.
*/}}
{{- define "mainframe-rag.splunkGatewayConfigPath" -}}/etc/mainframe-rag/splunk-gateway{{- end -}}

{{- define "mainframe-rag.splunkGatewayEnabled" -}}
{{- $t := .Values.teamTools | default dict -}}
{{- $s := $t.splunk | default dict -}}
{{- if $s.enabled -}}
{{- if not .Values.openwebui.enabled -}}
{{- fail "teamTools.splunk.enabled=true requires openwebui.enabled=true: the gateway is offered only through an Open WebUI Team model (ADR-0010 TD1)" -}}
{{- end -}}
{{- $mode := $s.authMode | default "" -}}
{{- if not (has $mode (list "oidc" "bearer")) -}}
{{- fail "teamTools.splunk.authMode must be oidc or bearer" -}}
{{- end -}}
{{- $model := $t.model | default dict -}}
{{- $missing := list -}}
{{- if not $s.secretName }}{{ $missing = append $missing "teamTools.splunk.secretName" }}{{ end -}}
{{- if not ($s.config | default "" | trim) }}{{ $missing = append $missing "teamTools.splunk.config" }}{{ end -}}
{{- if not ($s.caBundle | default "" | trim) }}{{ $missing = append $missing "teamTools.splunk.caBundle" }}{{ end -}}
{{- if not ($s.splunkEgress | default dict).cidrs }}{{ $missing = append $missing "teamTools.splunk.splunkEgress.cidrs" }}{{ end -}}
{{- if and (eq $mode "oidc") (not ($s.idpEgress | default dict).cidrs) }}{{ $missing = append $missing "teamTools.splunk.idpEgress.cidrs" }}{{ end -}}
{{- if not ($model.egress | default dict).cidrs }}{{ $missing = append $missing "teamTools.model.egress.cidrs" }}{{ end -}}
{{- if not .Values.models.reasoning.baseUrl }}{{ $missing = append $missing "models.reasoning.baseUrl" }}{{ end -}}
{{- if not .Values.models.reasoning.model }}{{ $missing = append $missing "models.reasoning.model" }}{{ end -}}
{{- if not .Values.gateway.apiKeySecretName }}{{ $missing = append $missing "gateway.apiKeySecretName" }}{{ end -}}
{{- if $missing -}}
{{- fail (printf "teamTools.splunk.enabled=true but required inputs are missing: %s. Set them, or leave the gateway off (SPLUNK_GATEWAY_ENABLED=false in airgap.env)." (join ", " $missing)) -}}
{{- end -}}
{{- $shape := list -}}
{{- if not ($s.splunkEgress | default dict).port }}{{ $shape = append $shape "teamTools.splunk.splunkEgress.port" }}{{ end -}}
{{- if and (eq $mode "oidc") (not ($s.idpEgress | default dict).port) }}{{ $shape = append $shape "teamTools.splunk.idpEgress.port" }}{{ end -}}
{{- if not ($model.egress | default dict).port }}{{ $shape = append $shape "teamTools.model.egress.port" }}{{ end -}}
{{- if not ($model.apiKeySecretKey | default "") }}{{ $shape = append $shape "teamTools.model.apiKeySecretKey" }}{{ end -}}
{{- if $shape -}}
{{- fail (printf "Team-tool settings must not be empty or null: %s (a null port would open the egress rule to every TCP port)." (join ", " $shape)) -}}
{{- end -}}
{{- /* ADR-0010 TD3: the Team model reads Open WebUI's own per-leg key, never the
shared key or another leg's (maintainer decision on #1374, 2026-11-25). */ -}}
{{- if has ($model.apiKeySecretKey | default "") (compact (list .Values.gateway.apiKeySecretKey "llm-api-key" "embed-api-key" "rerank-api-key" "context-llm-api-key")) -}}
{{- fail "teamTools.model.apiKeySecretKey must name Open WebUI's own LiteLLM key (ADR-0010 TD3), not the shared gateway.apiKeySecretKey or another model leg's key" -}}
{{- end -}}
{{- if and .Values.gateway.caConfigMapName (.Values.openwebui.oidc | default dict).caBundle -}}
{{- fail "teamTools.splunk.enabled=true with gateway.caConfigMapName: Open WebUI then trusts that complete bundle to reach LiteLLM, so put the IdP CA into it and leave openwebui.oidc.caBundle unset (OPENWEBUI_IDP_CA_FILE)" -}}
{{- end -}}
{{- if not (regexMatch "^\\s*(-----BEGIN CERTIFICATE-----\\r?\\n[A-Za-z0-9+/=\\r\\n]+-----END CERTIFICATE-----\\s*)+$" (toString $s.caBundle)) -}}
{{- fail "teamTools.splunk.caBundle must hold only PEM certificates (-----BEGIN CERTIFICATE----- blocks, no key or other text); the value is not shown" -}}
{{- end -}}
{{- /* The configuration is the site's file; the chart checks only what ties it
to this render. The gateway checks the rest at start (exit 2 names the key). */ -}}
{{- $cfg := fromJson (toString $s.config) -}}
{{- if or (not (kindIs "map" $cfg)) (hasKey $cfg "Error") -}}
{{- fail "teamTools.splunk.config must be one JSON object (the gateway configuration file); the value is not shown" -}}
{{- end -}}
{{- if ne (toString $cfg.gateway) "splunk" -}}
{{- fail "teamTools.splunk.config: key gateway must be \"splunk\"" -}}
{{- end -}}
{{- $backend := $cfg.backend | default dict -}}
{{- if not (kindIs "map" $backend) }}{{ $backend = dict }}{{ end -}}
{{- if ne (toString $backend.ca_file) (printf "%s/ca-bundle.crt" (include "mainframe-rag.splunkGatewayConfigPath" .)) -}}
{{- fail (printf "teamTools.splunk.config: backend.ca_file must be %s/ca-bundle.crt, where the chart mounts teamTools.splunk.caBundle" (include "mainframe-rag.splunkGatewayConfigPath" .)) -}}
{{- end -}}
{{- if and (eq $mode "oidc") (not $cfg.auth) -}}
{{- fail "teamTools.splunk.config: key auth is required in oidc mode" -}}
{{- end -}}
{{- with $cfg.token_exchange -}}
{{- if ne (toString (. | default dict).client_secret_env) "SPLUNK_GATEWAY_TOKEN_EXCHANGE_SECRET" -}}
{{- fail "teamTools.splunk.config: token_exchange.client_secret_env must be SPLUNK_GATEWAY_TOKEN_EXCHANGE_SECRET (the chart feeds it from the Secret's token-exchange-client-secret)" -}}
{{- end -}}
{{- end -}}
true
{{- end -}}
{{- end -}}

{{/*
Enabled team-tool gateways as a JSON list of specs (see above). Each spec:
name (object suffix and Open WebUI tool-server id), module, the gateway's
own environment variable names and Secret data keys, its values and backend
egress. Empty list ("[]") when every gateway is off.
*/}}
{{- define "mainframe-rag.teamGateways" -}}
{{- $out := list -}}
{{- if include "mainframe-rag.splunkGatewayEnabled" . -}}
{{- $s := .Values.teamTools.splunk -}}
{{- $out = append $out (dict
  "name" "splunk"
  "title" "Splunk"
  "description" "Read-only Splunk searches from site templates (team gateway)"
  "module" "mainframe_rag.gateways.splunk"
  "configPath" (include "mainframe-rag.splunkGatewayConfigPath" .)
  "authMode" $s.authMode
  "authModeEnv" "SPLUNK_AUTH_MODE"
  "tokenEnv" "SPLUNK_MCP_TOKEN"
  "tokenKey" "splunk-mcp-token"
  "bearerEnv" "SPLUNK_GATEWAY_BEARER_SECRET"
  "exchangeEnv" "SPLUNK_GATEWAY_TOKEN_EXCHANGE_SECRET"
  "secretName" $s.secretName
  "config" $s.config
  "caBundle" $s.caBundle
  "backendEgress" $s.splunkEgress
  "idpEgress" $s.idpEgress
  "resources" $s.resources) -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{/*
Open WebUI's TOOL_SERVER_CONNECTIONS for the enabled gateways (v0.13.4 schema:
type mcp, tool id server:mcp:<info.id>). bearer mode: Open WebUI sends the
shared secret, which v0.13.4 reads only inline, so the value is the
Kubernetes dependent-variable reference $(MFR_<NAME>_GATEWAY_KEY), defined
just before it from the gateway Secret; the manifest never holds it. oidc
mode: system_oauth forwards the signed-in user's access token. config is
always an object (null crashes v0.13.4 at start) and access_grants names the
team group by id; empty grants are admin-only.
*/}}
{{- define "mainframe-rag.teamToolServerConnections" -}}
{{- $grants := list -}}
{{- with (.Values.teamTools | default dict).groupId -}}
{{- $grants = append $grants (dict "principal_type" "group" "principal_id" . "permission" "read") -}}
{{- end -}}
{{- $conns := list -}}
{{- range $g := (include "mainframe-rag.teamGateways" . | fromJsonArray) -}}
{{- $bearer := eq $g.authMode "bearer" -}}
{{- $conns = append $conns (dict
  "type" "mcp"
  "url" (printf "http://rag-%s-gateway:8080/mcp" $g.name)
  "path" ""
  "auth_type" (ternary "bearer" "system_oauth" $bearer)
  "key" (ternary (printf "$(MFR_%s_GATEWAY_KEY)" (upper $g.name)) "" $bearer)
  "config" (dict "enable" true "access_grants" $grants)
  "info" (dict "id" $g.name "name" $g.title "description" $g.description)) -}}
{{- end -}}
{{- toJson $conns -}}
{{- end -}}

{{/*
Read-only z/OS dataset reads (ADR-0012, #1294): "true" or nothing, like the
helpers above. The one owner of the render refusals; every message is fixed
and names keys, never values. D3 guard (b): every Open WebUI sign-in and admin
role is an approved role (an admin role alone signs a user in). D3 guard (c):
no reader outside signed-in team members (console Route, agent policy off,
extra namespaces). D4: the bridge renders only with its bearer Secret and its
own NetworkPolicy (zos-bridge.yaml renders both whenever this is true).
D5: the transport is pinned, no default and no fallback. D7: the tool subset.
FEEDBACK_ENABLED=true is not refused (D6, Option 7A).
*/}}
{{- define "mainframe-rag.zosBridgeCaPath" -}}/etc/mainframe-rag/mcp-ftp{{- end -}}

{{- define "mainframe-rag.zosReadEnabled" -}}
{{- $z := .Values.zosRead | default dict -}}
{{- if $z.enabled -}}
{{- $guard := "z/OS reads are limited to signed-in team members (ADR-0012 D3 guard (c)); change it, or keep z/OS reads off (ZOS_READ_ENABLED=false in airgap.env)" -}}
{{- if .Values.route.enabled -}}
{{- fail (printf "zosRead.enabled=true refused with route.enabled=true (AGENT_ROUTE): %s" $guard) -}}
{{- end -}}
{{- if not .Values.networkPolicy.enabled -}}
{{- fail (printf "zosRead.enabled=true refused with networkPolicy.enabled=false (AGENT_NETWORK_POLICY): %s" $guard) -}}
{{- end -}}
{{- if .Values.networkPolicy.allowedNamespaces -}}
{{- fail (printf "zosRead.enabled=true refused with networkPolicy.allowedNamespaces set (AGENT_NETWORK_POLICY_ALLOWED_NAMESPACES): %s" $guard) -}}
{{- end -}}
{{- if .Values.openwebui.enabled -}}
{{- $audit := $z.auditKey | default dict -}}
{{- if not $audit.secretName -}}
{{- fail "zosRead.enabled=true refused without zosRead.auditKey.secretName: the docs Pipe tags every agent call with a keyed audit tag (ADR-0012 D3(d)) and answers nothing without its key. Create the Secret and set it, or keep z/OS reads off (ZOS_READ_ENABLED=false in airgap.env)" -}}
{{- end -}}
{{- if eq (toString $audit.secretName) (toString $z.secretName) -}}
{{- fail "zosRead.auditKey.secretName must not be zosRead.secretName: the Open WebUI pod must not reference the Secret that holds the z/OS ID" -}}
{{- end -}}
{{- if not $audit.key -}}
{{- fail "zosRead.auditKey.key must not be empty" -}}
{{- end -}}
{{- $approved := $z.approvedRoles | default list -}}
{{- $oidc := .Values.openwebui.oidc | default dict -}}
{{- range $raw := concat (splitList "," (toString ($oidc.allowedRoles | default ""))) (splitList "," (toString ($oidc.adminRoles | default ""))) -}}
{{- $role := trim $raw -}}
{{- if and $role (not (has $role $approved)) -}}
{{- fail "zosRead.enabled=true refused: every role in openwebui.oidc.allowedRoles and openwebui.oidc.adminRoles must be listed in zosRead.approvedRoles (ADR-0012 D3 guard (b)); role names are not shown. Approve the role after the premise review, or keep z/OS reads off (ZOS_READ_ENABLED=false in airgap.env)" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $f := $z.ftp | default dict -}}
{{- $pp := $f.passivePorts | default dict -}}
{{- $mode := $f.transportMode | default "" -}}
{{- if not (has $mode (list "ftps" "plain")) -}}
{{- fail "zosRead.ftp.transportMode must be ftps or plain: the operator pins the transport, with no fallback (ADR-0012 D5)" -}}
{{- end -}}
{{- $missing := list -}}
{{- if not $z.secretName }}{{ $missing = append $missing "zosRead.secretName" }}{{ end -}}
{{- if not $z.datasetAllowlist }}{{ $missing = append $missing "zosRead.datasetAllowlist" }}{{ end -}}
{{- if not $f.host }}{{ $missing = append $missing "zosRead.ftp.host" }}{{ end -}}
{{- if not $f.hostIp }}{{ $missing = append $missing "zosRead.ftp.hostIp" }}{{ end -}}
{{- if or (not $pp.first) (not $pp.last) }}{{ $missing = append $missing "zosRead.ftp.passivePorts" }}{{ end -}}
{{- if and (eq $mode "ftps") (not ($f.caBundle | default "" | trim)) }}{{ $missing = append $missing "zosRead.ftp.caBundle" }}{{ end -}}
{{- if $missing -}}
{{- fail (printf "zosRead.enabled=true but required inputs are missing: %s. Set them, or leave z/OS reads off (ZOS_READ_ENABLED=false in airgap.env)." (join ", " $missing)) -}}
{{- end -}}
{{- if or (not $f.port) (not $f.encoding) -}}
{{- fail "zosRead.ftp.port and zosRead.ftp.encoding must not be empty or null (a null port would open the egress rule to every TCP port)" -}}
{{- end -}}
{{- if gt (int $pp.first) (int $pp.last) -}}
{{- fail "zosRead.ftp.passivePorts.first must not be above zosRead.ftp.passivePorts.last" -}}
{{- end -}}
{{- if and (regexMatch "^([0-9]{1,3}\\.){3}[0-9]{1,3}$" (toString $f.host)) (ne (toString $f.host) (toString $f.hostIp)) -}}
{{- fail "zosRead.ftp.host is an IPv4 address that differs from zosRead.ftp.hostIp: the bridge reaches one z/OS host" -}}
{{- end -}}
{{- if and (eq $mode "plain") (not $f.plainAccepted) -}}
{{- fail "zosRead.ftp.transportMode=plain requires zosRead.ftp.plainAccepted=true: the site security owner's written acceptance of plain FTP (ADR-0012 D5)" -}}
{{- end -}}
{{- if and (eq $mode "ftps") (not (regexMatch "^\\s*(-----BEGIN CERTIFICATE-----\\r?\\n[A-Za-z0-9+/=\\r\\n]+-----END CERTIFICATE-----\\s*)+$" (toString $f.caBundle))) -}}
{{- fail "zosRead.ftp.caBundle must hold only PEM certificates (-----BEGIN CERTIFICATE----- blocks, no key or other text); the value is not shown" -}}
{{- end -}}
{{- $tools := $z.enabledTools | default list -}}
{{- if not $tools -}}
{{- fail "zosRead.enabledTools must name at least one approved tool (ADR-0012 D7)" -}}
{{- end -}}
{{- range $tools -}}
{{- if not (has . (list "dataset_read" "job_status" "jes_spool_read")) -}}
{{- fail "zosRead.enabledTools must be a subset of dataset_read, job_status, jes_spool_read (ADR-0012 D7)" -}}
{{- end -}}
{{- end -}}
{{- if and (or (has "job_status" $tools) (has "jes_spool_read" $tools)) (not $z.jesJobnamePrefixes) (not $z.jesOwnerPrefixes) -}}
{{- fail "zosRead.enabledTools names a JES tool but zosRead.jesJobnamePrefixes and zosRead.jesOwnerPrefixes are both empty: the bridge would deny every job" -}}
{{- end -}}
true
{{- end -}}
{{- end -}}
