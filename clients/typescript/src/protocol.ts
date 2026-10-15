// Wire format of the camera service: JSON-RPC 2.0 over one websocket.
// See docs/remote.md ("RPC reference" and "Wire format").

import type {
  CameraCommands,
  FirmwareVersion,
  FunctionFeedback,
  GimbalAttitude,
  LockConfig,
} from "./generated";

export type Controller = "angle" | "rate" | "firmware";

/** `error.data.kind` from the service, plus `disconnected` and `timeout` raised locally. */
export type ErrorKind =
  | "bad_args"
  | "unsupported"
  | "camera_timeout"
  | "camera_offline"
  | "rejected"
  | "not_controller"
  | "unavailable"
  | "conflict"
  | "failed"
  | "unknown_method"
  | "internal"
  | "bad_request"
  | "disconnected"
  | "timeout";

/** A failed call. Branch on `kind`; `type` is the Python exception class name. */
export class RemoteError extends Error {
  readonly kind: ErrorKind;
  readonly type: string;
  readonly fields: Record<string, unknown>;

  constructor(kind: ErrorKind, message: string, type = "RemoteError", fields = {}) {
    super(message);
    this.name = "RemoteError";
    this.kind = kind;
    this.type = type;
    this.fields = fields;
  }
}

/** Point-lock status: `lock`/`release`/`get_lock` results and `lock_status` events. */
export interface LockStatus {
  state: "locked" | "searching" | "idle";
  controller?: Controller;
  score?: number;
  on_screen?: boolean;
  /** Spot position, -0.5 (left/top) to 0.5 (right/bottom) from the image centre. */
  x?: number;
  y?: number;
  update_ms?: number;
  error_deg?: [number, number];
  command?: number[];
  compensated?: boolean;
  target_rate_deg_s?: [number, number];
  turn_scale?: [number, number];
  gain_scale?: number;
  /** Why an idle lock ended (tracking lost, manual override, ...). */
  reason?: string | null;
  exit_confirmed?: boolean;
  /** Service clock (seconds); present on events. */
  t?: number;
}

export interface LinkState {
  camera: boolean;
  video: boolean;
  source: string | null;
  firmware_tracking: boolean;
}

export interface ControlState {
  /** This client has control. */
  controller: boolean;
  /** Name of the client that has control, or null when nobody does. */
  holder: string | null;
}

export interface ServiceState {
  t: number;
  camera: {
    ip: string;
    alive: boolean;
    firmware: FirmwareVersion | null;
    firmware_tracking: boolean;
    attitude: GimbalAttitude | null;
    attitude_age_ms: number | null;
    mount: string | null;
    zoom: number | null;
    zoom_max: number | null;
  };
  video: { mode: string; live: boolean; source: string | null; buffered_frames: number };
  motion: Record<string, number>;
  lock: LockStatus;
  calibrating: boolean;
  control: ControlState & { clients: number };
}

export interface CommandDescription {
  name: string;
  group: string;
  description: string;
  schema: Record<string, unknown>;
  confirmation: string | null;
  read_only: boolean;
}

/** Service methods other than the SIYIClient passthrough commands. */
export interface ServiceMethods {
  hello: {
    params: { name?: string };
    result: { protocol: number; sdk: string; t: number; controller: boolean };
  };
  ping: { params: Record<string, never>; result: { t: number } };
  get_state: { params: Record<string, never>; result: ServiceState };
  list_commands: { params: Record<string, never>; result: CommandDescription[] };
  take_control: { params: { force?: boolean }; result: { controller: true } };
  release_control: { params: Record<string, never>; result: { controller: false } };
  stop: { params: Record<string, never>; result: { stopped: true } };
  lock: {
    params: {
      x: number;
      y: number;
      frame_time?: number | null;
      controller?: Controller | null;
      size?: number | null;
    };
    result: LockStatus;
  };
  release: { params: { reason?: string }; result: LockStatus };
  get_lock: { params: Record<string, never>; result: LockStatus };
  calibrate: {
    params: Record<string, never>;
    result: { config: LockConfig; notes: string[]; fit_error_deg: number };
  };
  get_lock_config: { params: Record<string, never>; result: LockConfig };
  set_lock_config: { params: Partial<LockConfig>; result: LockConfig };
  get_learned_loop: { params: Record<string, never>; result: Record<string, unknown> };
  apply_learned_loop: {
    params: Record<string, never>;
    result: { config: LockConfig; applied: Record<string, unknown> };
  };
}

export type Methods = CameraCommands & ServiceMethods;
export type MethodName = keyof Methods;
export type Params<M extends MethodName> = Methods[M]["params"];
export type Result<M extends MethodName> = Methods[M]["result"];

/** Events, as delivered to `RemoteCamera.on` listeners. */
export interface Events {
  attitude: GimbalAttitude;
  feedback: FunctionFeedback;
  lock_status: LockStatus;
  link: LinkState;
  zoom: { zoom: number; max: number | null };
  control: ControlState;
  /** Client-side: the websocket to the service opened or closed. */
  connection: { connected: boolean };
}
export type EventName = keyof Events;

// ---------------------------------------------------------------------------
// Values
// ---------------------------------------------------------------------------

/**
 * Unwrap tagged SDK values: dataclasses (`$type`) become plain objects, enums
 * (`$enum`) their numeric value (compare with the generated enum constants), and
 * `$bytes` a Uint8Array.
 */
export function decodeValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(decodeValue);
  if (value === null || typeof value !== "object") return value;
  const obj = value as Record<string, unknown>;
  if ("$bytes" in obj) return base64ToBytes(String(obj.$bytes));
  if ("$enum" in obj) return obj.value;
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(obj)) {
    if (key !== "$type") out[key] = decodeValue(item);
  }
  return out;
}

/** Arguments are plain JSON; only bytes need a tag. */
export function encodeValue(value: unknown): unknown {
  if (value instanceof Uint8Array) return { $bytes: bytesToBase64(value) };
  if (Array.isArray(value)) return value.map(encodeValue);
  if (value === null || typeof value !== "object") return value;
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (item !== undefined) out[key] = encodeValue(item);
  }
  return out;
}

export function decodeError(error: {
  message?: string;
  data?: { kind?: string; type?: string; fields?: Record<string, unknown> };
}): RemoteError {
  const data = error.data ?? {};
  return new RemoteError(
    (data.kind ?? "internal") as ErrorKind,
    error.message ?? "Remote error",
    data.type ?? "RemoteError",
    data.fields ?? {},
  );
}

function base64ToBytes(text: string): Uint8Array {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
