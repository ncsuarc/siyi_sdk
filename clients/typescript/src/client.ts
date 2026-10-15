// Browser/Node client for the camera service; the TypeScript counterpart of the
// Python siyi_sdk.remote.RemoteCamera. JSON-RPC requests and replies go through
// json-rpc-2.0; this class adds the websocket, setpoints, control, events and clock.

import {
  JSONRPCClient,
  JSONRPCErrorCode,
  JSONRPCErrorException,
  createJSONRPCErrorResponse,
  isJSONRPCResponse,
} from "json-rpc-2.0";

import {
  RemoteError,
  decodeError,
  decodeValue,
  encodeValue,
  type Controller,
  type EventName,
  type Events,
  type LockStatus,
  type MethodName,
  type Params,
  type Result,
} from "./protocol";

/** Resend held setpoints this often; the service stops them after 0.3 s of silence. */
const SETPOINT_PERIOD_MS = 100;
/** Ping for the clock offset and to notice a dead link. */
const PING_PERIOD_MS = 1000;
/** Drop the connection when nothing has arrived for this long. */
const SILENCE_TIMEOUT_MS = 3000;
const CLOCK_SAMPLES = 8;
const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 5000;

/** Longer reply timeouts for slow methods. */
const SLOW_METHODS: Partial<Record<MethodName, number>> = {
  calibrate: 60_000,
  set_encoding_params: 15_000,
  format_sd_card: 65_000,
};

export interface RemoteCameraOptions {
  /** Shown to other clients when this one has control. */
  name?: string;
  /** Default reply timeout in milliseconds. */
  timeout?: number;
  /** Ask for control on every (re)connect if nobody else has it. */
  takeControl?: boolean;
  /** Reconnect after the link drops. */
  reconnect?: boolean;
  /** Stop held setpoints when the browser tab is hidden (timers are throttled there). */
  stopWhenHidden?: boolean;
  /** WebSocket implementation; defaults to the global one. */
  WebSocket?: typeof WebSocket;
}

export interface CallOptions {
  /** Reply timeout in milliseconds. */
  timeout?: number;
}

type CallArgs<M extends MethodName> = Record<string, never> extends Params<M>
  ? [params?: Params<M>, options?: CallOptions]
  : [params: Params<M>, options?: CallOptions];

type Listener<E extends EventName> = (value: Events[E]) => void;

/**
 * Control an A8 Mini through the camera service on the aircraft.
 *
 * ```ts
 * const cam = new RemoteCamera("ws://192.168.144.30:8765", { name: "shimmer" });
 * await cam.connect();
 * cam.on("attitude", (att) => console.log(att.yaw_deg));
 * await cam.call("set_attitude", { yaw_deg: 10, pitch_deg: -30 });
 * cam.rate(40, 0);   // keeps turning while you keep calling it
 * ```
 */
export class RemoteCamera {
  readonly url: string;
  readonly name: string;
  /** This client has control; updated live. */
  isController = false;
  /** Name of the client that has control. */
  controlHolder: string | null = null;
  connected = false;
  serverInfo: Result<"hello"> | null = null;

  private readonly options: Required<Omit<RemoteCameraOptions, "WebSocket" | "name">>;
  private readonly WS: typeof WebSocket;
  private ws: WebSocket | null = null;
  private readonly rpc = new JSONRPCClient((payload) => this.send(payload));
  /** Bumped on every disconnect, to tell a lost reply from other failures. */
  private generation = 0;
  private readonly listeners = new Map<EventName, Set<Listener<any>>>();
  private readonly latestValues = new Map<EventName, unknown>();
  private rateSetpoint: [number, number] = [0, 0];
  private zoomSetpoint = 0;
  private clock: Array<[rtt: number, offset: number]> = [];
  private lastMessageAt = 0;
  private reconnectDelay = RECONNECT_MIN_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private timers: Array<ReturnType<typeof setInterval>> = [];
  private started = false;
  private closing = false;
  private firstConnection: { resolve: () => void; reject: (e: Error) => void } | null = null;
  private ready: Promise<void> | null = null;
  private readonly onVisibility = () => {
    if (document.visibilityState === "hidden") this.holdNothing();
  };

  constructor(url: string, options: RemoteCameraOptions = {}) {
    this.url = url;
    this.name = options.name ?? "";
    this.options = {
      timeout: options.timeout ?? 5000,
      takeControl: options.takeControl ?? true,
      reconnect: options.reconnect ?? true,
      stopWhenHidden: options.stopWhenHidden ?? true,
    };
    const WS = options.WebSocket ?? globalThis.WebSocket;
    if (!WS) throw new Error("No WebSocket implementation; pass options.WebSocket");
    this.WS = WS;
  }

  // ======================================================================
  // Connection
  // ======================================================================

  /**
   * Start connecting, and keep the connection up until `close()`. Resolves once the
   * first connection is ready; keeps retrying (with `reconnect`) until then.
   */
  connect(): Promise<void> {
    if (this.connected) return Promise.resolve();
    if (this.ready) return this.ready;
    const ready = new Promise<void>((resolve, reject) => {
      this.firstConnection = { resolve, reject };
    }).finally(() => {
      this.ready = null;
    });
    this.ready = ready;
    if (!this.started) {
      this.started = true;
      this.closing = false;
      this.timers.push(
        setInterval(() => this.flushSetpoints(), SETPOINT_PERIOD_MS),
        setInterval(() => this.heartbeat(), PING_PERIOD_MS),
      );
      if (this.options.stopWhenHidden && typeof document !== "undefined") {
        document.addEventListener("visibilitychange", this.onVisibility);
      }
      this.open();
    }
    return ready;
  }

  /** Stop held motion (if in control) and disconnect for good. */
  async close(): Promise<void> {
    if (this.isController && this.connected) {
      try {
        await this.stop();
      } catch {
        /* closing anyway */
      }
    }
    this.closing = true;
    this.started = false;
    for (const timer of this.timers) clearInterval(timer);
    this.timers = [];
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.onVisibility);
    }
    this.firstConnection?.reject(new RemoteError("disconnected", "Closed before connecting"));
    this.firstConnection = null;
    const ws = this.ws;
    if (ws) {
      this.detach(ws);
      ws.close();
      this.lost();
    }
  }

  private open(): void {
    const ws = new this.WS(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.lastMessageAt = performance.now();
      void this.handshake(ws);
    };
    ws.onmessage = (event) => this.receive(String(event.data));
    ws.onclose = () => {
      this.detach(ws);
      this.lost();
    };
    ws.onerror = () => {
      /* onclose follows */
    };
  }

  private async handshake(ws: WebSocket): Promise<void> {
    try {
      const info = await this.call("hello", { name: this.name });
      this.serverInfo = info;
      this.isController = info.controller;
      if (this.options.takeControl && !info.controller) {
        try {
          await this.takeControl();
        } catch (error) {
          if (!(error instanceof RemoteError && error.kind === "not_controller")) throw error;
        }
      }
    } catch {
      if (this.ws === ws) ws.close();
      return;
    }
    if (this.ws !== ws) return;
    this.reconnectDelay = RECONNECT_MIN_MS;
    this.connected = true;
    this.emit("connection", { connected: true });
    void this.ping().catch(() => undefined);
    this.firstConnection?.resolve();
    this.firstConnection = null;
  }

  private detach(ws: WebSocket): void {
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
    if (this.ws === ws) this.ws = null;
  }

  private lost(): void {
    const wasConnected = this.connected;
    this.connected = false;
    this.isController = false;
    this.clock = [];
    this.generation++;
    this.rpc.rejectAllPendingRequests("Connection to camera service lost");
    if (wasConnected) {
      this.emit("connection", { connected: false });
      this.emit("control", { controller: false, holder: this.controlHolder });
    }
    if (this.closing) return;
    if (!this.options.reconnect) {
      this.firstConnection?.reject(new RemoteError("disconnected", `Cannot reach ${this.url}`));
      this.firstConnection = null;
      return;
    }
    this.reconnectTimer = setTimeout(() => this.open(), this.reconnectDelay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, RECONNECT_MAX_MS);
  }

  private heartbeat(): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== 1) return;
    // Browsers can take minutes to notice a dead TCP link; the service talks at
    // least once a second, so silence means the radio link is gone.
    if (performance.now() - this.lastMessageAt > SILENCE_TIMEOUT_MS) {
      this.detach(ws);
      ws.close();
      this.lost();
      return;
    }
    if (this.connected) void this.ping().catch(() => undefined);
  }

  private receive(text: string): void {
    this.lastMessageAt = performance.now();
    let msg: any;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (!msg || typeof msg !== "object") return;
    if (typeof msg.method === "string") {
      // An event: the service never sends requests.
      this.dispatch(msg.method, decodeValue(msg.params ?? {}) as Record<string, any>);
    } else if (isJSONRPCResponse(msg)) {
      this.rpc.receive(msg);
    }
  }

  private send(payload: unknown): void {
    const ws = this.ws;
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(payload));
  }

  // ======================================================================
  // Calls
  // ======================================================================

  /**
   * Invoke a service method by name. Every SIYIClient passthrough command and
   * service method is typed, e.g. `call("set_attitude", { yaw_deg: 0, pitch_deg: -90 })`.
   *
   * Rejects with `RemoteError`; branch on `error.kind`.
   */
  async call<M extends MethodName>(
    method: M,
    ...[params, options]: CallArgs<M>
  ): Promise<Result<M>> {
    if (!this.ws || this.ws.readyState !== 1) {
      throw new RemoteError("disconnected", "Not connected to the camera service");
    }
    const timeout = options?.timeout ?? SLOW_METHODS[method] ?? this.options.timeout;
    const generation = this.generation;
    const timedOut = (id: Parameters<typeof createJSONRPCErrorResponse>[0]) =>
      createJSONRPCErrorResponse(
        id,
        JSONRPCErrorCode.InternalError,
        `No reply to ${method} after ${timeout} ms`,
        { kind: "timeout" },
      );
    try {
      const result = await this.rpc
        .timeout(timeout, timedOut)
        .request(method, params && Object.keys(params).length ? encodeValue(params) : undefined);
      return decodeValue(result) as Result<M>;
    } catch (error) {
      if (error instanceof JSONRPCErrorException && error.data?.kind) {
        throw decodeError({ message: error.message, data: error.data });
      }
      if (generation !== this.generation) {
        throw new RemoteError("disconnected", "Connection to camera service lost");
      }
      throw new RemoteError("internal", error instanceof Error ? error.message : String(error));
    }
  }

  private notify(method: string, params: Record<string, unknown>): void {
    this.rpc.notify(method, params);
  }

  // ======================================================================
  // Control
  // ======================================================================

  /** Become the controlling ground station; `force` takes it from another one. */
  async takeControl(force = false): Promise<void> {
    await this.call("take_control", { force });
    this.isController = true;
  }

  /** Let another ground station take control. */
  async releaseControl(): Promise<void> {
    await this.call("release_control");
    this.isController = false;
  }

  /** Camera, video, motion, lock and control state. */
  getState(): Promise<Result<"get_state">> {
    return this.call("get_state");
  }

  // ======================================================================
  // Speed setpoints
  // ======================================================================

  /**
   * Turn at 0x07 speeds (-100..100); (0, 0) stops. Call on every stick move.
   *
   * The speed is resent every 100 ms while non-zero. If this page or the link
   * stops, the service stops the gimbal within 0.3 s. Manual motion releases a
   * point lock.
   */
  rate(yaw: number, pitch: number): void {
    this.rateSetpoint = [clampSpeed(yaw), clampSpeed(pitch)];
    this.notify("rate", { yaw: this.rateSetpoint[0], pitch: this.rateSetpoint[1] });
  }

  /** Zoom in (1), out (-1) or stop (0); held like `rate`. */
  zoomRate(direction: number): void {
    this.zoomSetpoint = Math.sign(direction);
    this.notify("zoom_rate", { direction: this.zoomSetpoint });
  }

  /** Stop turning and zooming now. */
  async stop(): Promise<void> {
    this.rateSetpoint = [0, 0];
    this.zoomSetpoint = 0;
    await this.call("stop");
  }

  private flushSetpoints(): void {
    const [yaw, pitch] = this.rateSetpoint;
    if (yaw || pitch) this.notify("rate", { yaw, pitch });
    if (this.zoomSetpoint) this.notify("zoom_rate", { direction: this.zoomSetpoint });
  }

  private holdNothing(): void {
    if (this.rateSetpoint[0] || this.rateSetpoint[1]) this.rate(0, 0);
    if (this.zoomSetpoint) this.zoomRate(0);
  }

  // ======================================================================
  // Point lock
  // ======================================================================

  /**
   * Lock the spot at (x, y) from the image centre, each -0.5 (left/top) to 0.5
   * (right/bottom).
   *
   * `frameTime` is the `performance.now()` time (ms) at which the clicked frame was
   * shown or received, e.g. from `video.requestVideoFrameCallback`, so the service
   * locks on the same picture. `controller` overrides the stored default.
   */
  lock(
    x: number,
    y: number,
    options: { frameTime?: number; controller?: Controller; size?: number } = {},
  ): Promise<LockStatus> {
    const frame_time = options.frameTime === undefined ? null : this.toServiceTime(options.frameTime);
    return this.call("lock", {
      x,
      y,
      frame_time,
      controller: options.controller ?? null,
      size: options.size ?? null,
    });
  }

  /** Release the point lock; returns its final status. */
  release(): Promise<LockStatus> {
    return this.call("release");
  }

  /** Measure the loop (turns the gimbal about 20 degrees per axis) and store it. */
  calibrate(): Promise<Result<"calibrate">> {
    return this.call("calibrate");
  }

  // ======================================================================
  // Clock
  // ======================================================================

  /** Measure the round trip (ms) and update the clock offset. */
  async ping(): Promise<number> {
    const sent = performance.now();
    const reply = await this.call("ping");
    const received = performance.now();
    const rtt = received - sent;
    const offset = reply.t - (sent + received) / 2000;
    this.clock.push([rtt, offset]);
    if (this.clock.length > CLOCK_SAMPLES) this.clock.shift();
    return rtt;
  }

  /** Fastest recent round trip in ms. */
  get rtt(): number | null {
    return this.bestSample()?.[0] ?? null;
  }

  /** Service clock (s) minus `performance.now()` (s), from the fastest ping. */
  get clockOffset(): number | null {
    return this.bestSample()?.[1] ?? null;
  }

  /** Convert a `performance.now()` time (ms) to the service's clock (s). */
  toServiceTime(ms: number): number | null {
    const offset = this.clockOffset;
    return offset === null ? null : ms / 1000 + offset;
  }

  private bestSample(): [number, number] | undefined {
    let best: [number, number] | undefined;
    for (const sample of this.clock) if (!best || sample[0] < best[0]) best = sample;
    return best;
  }

  // ======================================================================
  // Events
  // ======================================================================

  /** Listen for an event; returns the unsubscribe function. */
  on<E extends EventName>(event: E, listener: Listener<E>): () => void {
    let set = this.listeners.get(event);
    if (!set) this.listeners.set(event, (set = new Set()));
    set.add(listener);
    return () => set!.delete(listener);
  }

  /** The last value of an event, or undefined before the first one. */
  latest<E extends EventName>(event: E): Events[E] | undefined {
    return this.latestValues.get(event) as Events[E] | undefined;
  }

  private dispatch(event: string, params: Record<string, any>): void {
    let value: unknown = params;
    if (event === "control") {
      this.isController = Boolean(params.controller);
      this.controlHolder = params.holder ?? null;
    } else if (event === "attitude") {
      value = params.attitude;
    } else if (event === "feedback") {
      value = params.feedback;
    }
    this.emit(event as EventName, value);
  }

  private emit(event: EventName, value: unknown): void {
    this.latestValues.set(event, value);
    for (const listener of [...(this.listeners.get(event) ?? [])]) {
      try {
        listener(value as never);
      } catch (error) {
        console.error(`RemoteCamera ${event} listener failed`, error);
      }
    }
  }
}

function clampSpeed(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(-100, Math.min(100, Math.round(value)));
}
