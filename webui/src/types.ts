export type SessionStatus =
  | 'idle'
  | 'running'
  | 'waiting'
  | 'stopping'
  | 'stopped'
  | 'paused'
  | 'interrupted'
  | 'completed'
  | 'failed';
export interface ProcessEntry {
  id: string;
  kind: string;
  title: string;
  content?: string;
  tool_call_id?: string;
  arguments?: string;
  output?: string;
  result_text?: string;
  status?: 'running' | 'completed' | 'failed' | 'interrupted' | 'unknown' | 'waiting';
  device_id?: string;
  device_name?: string;
  task_id?: string;
  progress?: { id: number; content: string }[];
  files?: FileReference[];
  warnings?: string[];
  input_request?: Record<string, unknown> | null;
  continued?: boolean;
  elapsed_ms?: number;
  truncated?: boolean;
  output_truncated?: boolean;
}
export interface DeviceSelection {
  mode: 'auto' | 'selected';
  devices: { id: string; name: string }[];
}
export interface Message {
  id: string;
  source?: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  process?: ProcessEntry[];
  delegations?: Record<string, Delegation>;
  created_at?: number | string;
  mode?: string;
  applied?: boolean;
  invalidated?: boolean;
  skills?: SkillReference[];
  attachments?: FileReference[];
  device_selection?: DeviceSelection;
}
export interface SubagentTask {
  task_index: number;
  goal: string;
  status:
    | 'pending'
    | 'running'
    | 'finalizing'
    | 'completed'
    | 'failed'
    | 'timed_out'
    | 'interrupted'
    | 'completion_unknown';
  messages: Message[];
  model?: string;
  duration_seconds?: number;
  status_text?: string;
  last_tool?: string;
  error?: string;
  legacy?: boolean;
}
export interface Delegation {
  tool_call_id?: string;
  goals?: string[];
  tasks?: Record<string, SubagentTask>;
  events?: { task_index: number; event_type: string; data: Record<string, unknown> }[];
  result?: {
    results: {
      task_index: number;
      goal?: string;
      status?: string;
      summary?: string;
      error?: string;
      model?: string;
      duration_seconds?: number;
    }[];
  };
}
export interface QueueItem {
  id: string;
  content: string;
  model?: string;
  skills?: SkillReference[];
  skill_names?: string[];
  attachments?: FileReference[];
  attachment_ids?: string[];
  device_selection?: DeviceSelection;
}
export interface SecretNeed {
  env_var: string;
  provider?: string;
  purpose?: string;
  has_value?: boolean;
  authorized?: boolean;
  state?: 'missing' | 'authorize' | 'refresh';
}
export interface PendingRequest {
  id: string;
  kind: 'workspace' | 'secret' | 'skill_install';
  title: string;
  workspace?: string;
  description?: string;
  required_for?: string;
  needs?: SecretNeed[];
  drafting_id?: string;
  skill_name?: string;
  source?: SkillSource;
  content?: string;
  files?: unknown;
  review?: unknown;
  dependency_hints?: unknown;
  blocked?: boolean;
  confirmation_token?: string;
}
export interface Session {
  id: string;
  source?: string;
  seq?: number;
  title: string;
  status: SessionStatus;
  workspace?: string;
  workspace_explicit?: boolean;
  model?: string;
  updated_at?: string;
  messages: Message[];
  queue: QueueItem[];
  active_task?: {
    id: string;
    content?: string;
    status?: string;
    device_selection?: DeviceSelection;
  } | null;
  error?: string | { message: string };
  status_text?: string;
  draft?: string;
  draft_skills?: string[];
  draft_attachments?: string[];
  draft_devices?: string[];
  pending_request?: PendingRequest | null;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    cache_tokens?: number;
    total_tokens?: number;
  };
}
export interface ModelOption {
  id: string;
  label: string;
  provider?: string;
  provider_label?: string;
}

export interface ToolBackend {
  id: string;
  label: string;
  credentials: { id: string; configured: boolean }[];
}
export interface ManagedTool {
  id: string;
  label: string;
  description: string;
  required: boolean;
  enabled: boolean;
  ready: boolean;
  tools: string[];
  backends: ToolBackend[];
  backend: string;
  credential_source: string;
  model: string;
  channel: string;
}
export interface ToolCatalog {
  items: ManagedTool[];
  revision: string;
}
export interface ToolForm {
  revision: string;
  enabled: boolean;
  backend?: string;
  credential_source?: string;
  api_key?: string;
  model?: string;
}
export interface Capability {
  id: string;
  label: string;
  available: boolean;
  reason?: string;
}
export interface Bootstrap {
  setup_required: boolean;
  version: string;
  device: { name: string; platform: string };
  models: ModelOption[];
  default_model?: string;
  output_root?: string;
  capabilities: Capability[];
}
export interface SetupOption {
  id: string;
  label: string;
  description?: string;
  category?: string;
}
export interface SetupPrompt {
  id: string;
  kind: 'text' | 'secret' | 'single_select' | 'multi_select';
  title: string;
  hint?: string;
  options?: SetupOption[];
  default?: string | string[];
  secret_configured?: boolean;
  presentation?:
    | 'welcome'
    | 'providers'
    | 'profiles'
    | 'models'
    | 'capabilities'
    | 'channels'
    | 'voice'
    | 'skills'
    | 'notice'
    | '';
  provider?: string;
  field_label?: string;
  placeholder?: string;
  credential_path?: string;
  key_url?: string;
}
export interface SetupRun {
  id: string;
  status: 'running' | 'waiting' | 'completed' | 'cancelled' | 'failed';
  prompt: SetupPrompt | null;
  messages: string[];
  error?: { code: string; message: string } | null;
  saved: boolean;
  qr_code?: string | null;
  configuration: { ready: boolean; required: boolean; provider?: string; model?: string };
}
export interface SetupStatus {
  required: boolean;
  ready: boolean;
  provider?: string;
  model?: string;
  active_run_id?: string | null;
  providers: { id: string; label: string; configured: boolean }[];
  steps: { id: string; label: string }[];
}
export interface SessionEvent {
  type: string;
  seq?: number;
  session?: Session;
  content?: string;
  message_id?: string;
}

export interface FileReference {
  id: string;
  name: string;
  path: string;
  source: 'upload' | 'local' | 'remote';
  remote_device_id?: string;
  remote_task_id?: string;
  size: number;
  modified_at: number;
  created_at: number;
  device: string;
  workspace: string;
  mime_type: string;
}
export interface FilePreview {
  file: FileReference;
  kind: 'text' | 'markdown' | 'image' | 'pdf' | 'download';
  content?: string;
  truncated?: boolean;
}
export interface FileBrowse {
  path: string;
  parent: string;
  entries: { name: string; path: string; directory: boolean; size: number }[];
  truncated: boolean;
  max_upload_bytes: number;
}

export interface ProviderProfile {
  id: string;
  label: string;
  runtime_provider: string;
  api_mode: string;
  base_url: string;
  base_url_editable: boolean;
  credential_configured: boolean;
  key_url: string;
  model: string;
  models: string[];
}
export interface ProviderCandidate {
  id: string;
  label: string;
  keywords: string;
  profiles: ProviderProfile[];
}
export interface ConfiguredProvider {
  provider: string;
  profile: string;
  label: string;
  region: string;
  runtime_provider: string;
  model: string;
  credential_configured: boolean;
  is_default: boolean;
}
export interface ProviderCatalog {
  catalog: ProviderCandidate[];
  configured: ConfiguredProvider[];
  revision: string;
  library?: ModelLibraryStatus;
}
export interface ModelLibraryStatus {
  updated_at: number | null;
  providers: number;
  models: number;
  refreshing: boolean;
  last_refresh: { ok: boolean; finished_at: number } | null;
}
export interface ModelLibrarySnapshot {
  status: ModelLibraryStatus;
  providers: {
    id: string;
    label: string;
    profiles: {
      provider: string;
      label: string;
      profile: string;
      region: string;
      callable: boolean;
    }[];
  }[];
  models: {
    id: string;
    name: string;
    provider: string;
    provider_label: string;
    context: number | null;
  }[];
  total: number;
  offset: number;
  limit: number;
}
export interface ProviderForm {
  provider: string;
  profile: string;
  model: string;
  api_key: string;
  base_url: string;
  make_default: boolean;
  revision: string;
}

export interface VoiceStatus {
  enabled: boolean;
  ready: boolean;
  credential_configured: boolean;
  authorized: boolean;
  provider: 'qwen' | 'qwen-intl';
  region: 'cn' | 'intl';
  model: string;
  max_seconds: number;
  max_bytes: number;
  mime_types: string[];
  notice: string;
}

export type SkillSource = string | { type: string; original?: string; url?: string; path?: string };
export interface SkillReference {
  name: string;
  source?: SkillSource;
  version?: string;
}
export interface SkillSummary extends SkillReference {
  description: string;
  enabled: boolean;
}
export interface SkillFileEntry {
  path: string;
  type: string;
  size_bytes?: number;
  system?: boolean;
  mutable_with_skill_manage?: boolean;
}
export interface SkillDetail extends SkillSummary {
  content: string;
  metadata?: Record<string, unknown>;
  evolution?: unknown;
  tree: { entries: SkillFileEntry[]; truncated?: boolean };
}
export interface SkillFile {
  name: string;
  path: string;
  content: string;
  version: string;
  editable: boolean;
}
export interface SkillDraft {
  drafting_id: string;
  skill_name: string;
  source: SkillSource;
  content: string;
  files?: unknown;
  review?: unknown;
  dependency_hints?: unknown;
  blocked: boolean;
  confirmation_token: string;
}
export interface MemoryDocument {
  target: 'memory' | 'user';
  label: string;
  content: string;
  revision: string;
  limit: number;
  updated_at?: string | number;
  history: { at: string | number; action: string; summary: string }[];
}
export interface Diagnostics {
  checked_at: string;
  summary: { passed: number; warnings: number; failed: number };
  checks: {
    section: string;
    name: string;
    status: 'OK' | 'WARN' | 'FAIL';
    detail: string;
    fix?: string;
  }[];
  report: string;
}
export interface ScheduleSpec {
  trigger_type: 'once' | 'daily' | 'weekly' | 'monthly' | 'interval' | 'cron';
  timezone: string;
  at?: string;
  time?: string;
  weekday?: number;
  day?: number;
  every?: string;
  expression?: string;
  parsed?: Record<string, unknown>;
}
export interface ScheduleForm {
  name: string;
  prompt: string;
  schedule: ScheduleSpec;
  model?: string;
  workdir?: string;
  enabled?: boolean;
  session_policy?: 'task_thread' | 'new_session';
  concurrency_policy?: 'skip' | 'queue' | 'parallel' | 'replace';
  enabled_toolsets?: string[];
  max_iterations?: number;
  timeout_seconds?: number;
  delivery?: {
    target_id: string;
    retry_count?: number;
    final_response_path?: string;
    final_response_filename_template?: string;
  };
}
export interface ScheduledJob extends ScheduleForm {
  id: string;
  status: string;
  session_id?: string;
  next_run_at?: number;
  last_run_at?: number;
  failure_count?: number;
  created_at?: number;
  updated_at?: number;
  schedule_label?: string;
  next_runs?: number[];
}
export interface ScheduleRun {
  id: string;
  job_id: string;
  run_no: number;
  status: string;
  scheduled_for?: number;
  started_at?: number;
  finished_at?: number;
  session_id?: string;
  final_response?: string;
  error?: string;
  tool_calls?: unknown;
  token_usage?: unknown;
  output_path?: string;
  delivery_result?: unknown;
}
export interface ScheduleTarget {
  id: string;
  type: 'local' | 'dingtalk_group' | 'dingtalk_private' | 'weixin_private';
  display_name: string;
  enabled: boolean;
  route_status: 'ready' | 'needs_route' | 'disabled';
  account_id?: string;
  chat_id?: string;
  chat_type?: string;
}
export interface SchedulePairing {
  code: string;
  status: string;
  expires_at: number;
  command: string;
  target_id?: string;
  error?: string;
}
export type ConfigurationPage = 'general' | 'memory' | 'execution' | 'scheduler';
export type ConfigurationValue = string | number | boolean | null;
export type ConfigurationField = {
  path: string;
  label: string;
  kind: 'boolean' | 'number' | 'select' | 'text' | 'url' | 'timezone' | 'provider';
  description: string;
  advanced: boolean;
  minimum: number | null;
  maximum: number | null;
  step: number | null;
  options: { value: string; label: string }[];
  value: ConfigurationValue;
  credential_url?: boolean;
};
export type ConfigurationSnapshot = {
  revision: string;
  groups: {
    id: string;
    page: ConfigurationPage;
    title: string;
    effect: string;
    fields: ConfigurationField[];
  }[];
};
export type ScheduleDefaults = {
  timezone: string;
  timeout_seconds: number;
  max_iterations: number;
};
export interface NearbyDevice {
  deviceIdSha256: string;
  deviceName: string;
  deviceTypeId: number;
  publicDeviceId: string;
}

export interface DeviceSnapshot {
  supported: boolean;
  enabled: boolean;
  state: string;
  provider_ready: boolean;
  error: { code: string; message: string; outcome_unknown?: boolean } | null;
  operation: {
    id: string;
    action: 'discover' | 'pair' | 'unpair';
    status: 'running' | 'completed' | 'failed';
    device_id: string;
    error: { code: string; message: string } | null;
  } | null;
  localDevice: {
    deviceName: string;
    model: string;
    osName: string;
    osVersion: string;
    apiLevel: number;
    arch: string;
  } | null;
  devices: {
    deviceId: string;
    deviceName: string;
    agentAvailability: string;
    connectionState: string;
    devicePresence: string;
  }[];
  trustedDevices: (NearbyDevice & { online: boolean })[];
  pairableDevices: NearbyDevice[];
  discoveryWarning: { code: string } | null;
}
