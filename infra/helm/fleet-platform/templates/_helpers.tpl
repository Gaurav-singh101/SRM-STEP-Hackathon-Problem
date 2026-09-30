{{/*
Shared template helpers. One definition of a name, a label set, an image reference and the
environment block, reused by every service - the Helm equivalent of the rule of three
(CLAUDE.md S7). Without this, four Deployments drift and one of them eventually forgets a
security context.
*/}}

{{- define "fleet.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "fleet.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "fleet.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "fleet.labels" -}}
app.kubernetes.io/name: {{ include "fleet.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: fleet-platform
{{- end -}}

{{- define "fleet.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "fleet.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
One image reference for every service. The four services are ONE image parameterised by
FLEET_SERVICE_MODULE, which is why there is a single Dockerfile and a single tag to bump.
*/}}
{{- define "fleet.image" -}}
{{- printf "%s/fleet-service:%s" .Values.global.imageRegistry .Values.global.imageTag -}}
{{- end -}}

{{/*
The environment block every service shares.

Secrets are referenced, never templated in: a password rendered into a ConfigMap would sit
in `helm get values` output and in the release Secret in plain text (CLAUDE.md S11).
*/}}
{{- define "fleet.env" -}}
- name: APP_ENV
  value: {{ .Values.global.appEnv | default "production" | quote }}
- name: LOG_FORMAT
  value: json
- name: KAFKA_BOOTSTRAP_SERVERS
  value: {{ .Values.externalServices.kafka.bootstrapServers | quote }}
- name: POSTGRES_HOST
  value: {{ .Values.externalServices.postgres.host | quote }}
- name: POSTGRES_PORT
  value: {{ .Values.externalServices.postgres.port | quote }}
- name: POSTGRES_DB
  value: {{ .Values.externalServices.postgres.database | quote }}
- name: POSTGRES_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.externalServices.postgres.existingSecret }}
      key: username
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.externalServices.postgres.existingSecret }}
      key: password
- name: MONGO_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Values.externalServices.mongo.uriSecret }}
      key: uri
- name: REDIS_URL
  value: {{ .Values.externalServices.redis.url | quote }}
- name: OIDC_ISSUER
  value: {{ .Values.auth.oidcIssuer | quote }}
- name: OIDC_AUDIENCE
  value: {{ .Values.auth.oidcAudience | quote }}
- name: OIDC_JWKS_URL
  value: {{ .Values.auth.oidcJwksUrl | quote }}
- name: OIDC_ALLOWED_ALGORITHMS
  value: {{ .Values.auth.allowedAlgorithms | quote }}
- name: IDLE_MIN_SECONDS
  value: {{ .Values.business.idleMinSeconds | quote }}
- name: IDLE_EXCESSIVE_SECONDS
  value: {{ .Values.business.idleExcessiveSeconds | quote }}
- name: IDLE_GAP_TOLERANCE_SECONDS
  value: {{ .Values.business.idleGapToleranceSeconds | quote }}
- name: FUEL_FORMULA_VERSION
  value: {{ .Values.business.fuelFormulaVersion | quote }}
- name: UTILISATION_FORMULA_VERSION
  value: {{ .Values.business.utilisationFormulaVersion | quote }}
- name: DEFAULT_CURRENCY
  value: {{ .Values.business.defaultCurrency | quote }}
- name: ALLOWED_LATENESS_SECONDS
  value: {{ .Values.business.allowedLatenessSeconds | quote }}
{{- end -}}

{{/*
readOnlyRootFilesystem means anything that writes needs an explicit writable mount. Python
wants a cache directory; giving it an emptyDir is cheaper than relaxing the security context.
*/}}
{{- define "fleet.writableVolumes" -}}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "fleet.writableVolumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- end -}}
