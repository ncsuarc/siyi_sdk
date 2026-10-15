// RemoteCamera -> websocket -> CameraServer -> fake camera, end to end.

import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import {
  CenteringAction,
  GimbalMotionMode,
  RemoteCamera,
  RemoteError,
  decodeValue,
  encodeValue,
  type GimbalAttitude,
} from "../src";
import { sleep, startService, waitFor, type FakeService } from "./service";

let service: FakeService;
const cameras: RemoteCamera[] = [];

async function connect(name: string, options = {}): Promise<RemoteCamera> {
  const cam = new RemoteCamera(service.url, { name, ...options });
  cameras.push(cam);
  await cam.connect();
  return cam;
}

beforeAll(async () => {
  service = await startService();
}, 60_000);

afterAll(async () => {
  await service?.stop();
});

afterEach(async () => {
  await Promise.all(cameras.splice(0).map((cam) => cam.close()));
});

describe("calls", () => {
  it("returns decoded SDK values", async () => {
    const cam = await connect("a");
    const version = await cam.call("get_firmware_version");
    expect(typeof version.camera).toBe("number");
    expect(version).not.toHaveProperty("$type");

    const ack = await cam.call("set_attitude", { yaw_deg: 15, pitch_deg: -30 });
    expect(ack.yaw_deg).toBeCloseTo(15, 0);

    const range = await cam.call("get_zoom_range");
    expect(range.max_int).toBeGreaterThan(0);
  });

  it("accepts enum constants as arguments", async () => {
    const cam = await connect("a");
    await expect(
      cam.call("one_key_centering", { action: CenteringAction.CENTER }),
    ).resolves.toBeNull();
  });

  it("rejects with the service's error kind", async () => {
    const cam = await connect("a");
    await expect(cam.call("set_attitude", { yaw_deg: "x" as never, pitch_deg: 0 })).rejects.toMatchObject({
      kind: "bad_args",
    });
    const call = cam.call.bind(cam) as (method: string) => Promise<unknown>;
    await expect(call("no_such_method")).rejects.toMatchObject({
      kind: "unknown_method",
    });
  });

  it("returns service state", async () => {
    const cam = await connect("a");
    const state = await cam.getState();
    expect(state.camera.alive).toBe(true);
    expect(state.control).toMatchObject({ controller: true, holder: "a" });
    expect(state.lock.state).toBe("idle");
  });
});

describe("values", () => {
  it("unwraps tagged models, enums and bytes", () => {
    expect(
      decodeValue({
        $type: "CameraSystemInfo",
        gimbal_motion_mode: { $enum: "GimbalMotionMode", name: "FOLLOW", value: 1 },
        raw: { $bytes: "AQL/" },
      }),
    ).toEqual({ gimbal_motion_mode: GimbalMotionMode.FOLLOW, raw: new Uint8Array([1, 2, 255]) });
  });

  it("tags bytes and drops undefined arguments", () => {
    expect(encodeValue({ raw: new Uint8Array([1, 2, 255]), size: undefined })).toEqual({
      raw: { $bytes: "AQL/" },
    });
  });
});

describe("control", () => {
  it("lets one client control and the others observe", async () => {
    const first = await connect("first");
    const second = await connect("second");
    expect(first.isController).toBe(true);
    expect(second.isController).toBe(false);
    expect(second.controlHolder).toBe("first");

    await expect(second.call("get_gimbal_attitude")).resolves.toBeTruthy();
    const error = await second.call("set_attitude", { yaw_deg: 0, pitch_deg: 0 }).catch((e) => e);
    expect(error).toBeInstanceOf(RemoteError);
    expect(error.kind).toBe("not_controller");

    await second.takeControl(true);
    await waitFor(() => !first.isController);
    expect(first.controlHolder).toBe("second");
  });

  it("hands control on when the controller leaves", async () => {
    const first = await connect("first");
    const second = await connect("second");
    await first.close();
    await waitFor(() => second.controlHolder === null);
    await second.takeControl();
    expect(second.isController).toBe(true);
  });
});

describe("setpoints", () => {
  it("holds a rate until it is set back to zero", async () => {
    const cam = await connect("a");
    cam.rate(40, -20);
    // Longer than the service's 0.3 s setpoint timeout: the client keeps resending.
    await sleep(700);
    expect((await cam.getState()).motion).toMatchObject({ rate: [40, -20] });

    cam.rate(0, 0);
    await sleep(100);
    expect((await cam.getState()).motion).not.toHaveProperty("rate");
  });

  it("stops when the client goes away", async () => {
    const cam = await connect("a");
    const observer = await connect("observer");
    cam.zoomRate(1);
    await sleep(200);
    expect((await observer.getState()).motion).toHaveProperty("zoom");
    await cam.close();
    await sleep(100);
    expect((await observer.getState()).motion).not.toHaveProperty("zoom");
  });
});

describe("events", () => {
  it("delivers attitude and keeps the latest value", async () => {
    const cam = await connect("a");
    const received: GimbalAttitude[] = [];
    cam.on("attitude", (att) => received.push(att));
    await waitFor(() => received.length >= 3);
    expect(typeof received[0].yaw_deg).toBe("number");
    expect(cam.latest("attitude")).toBe(received.at(-1));
  });

  it("reports connection changes", async () => {
    const cam = new RemoteCamera(service.url, { name: "a" });
    cameras.push(cam);
    const seen: boolean[] = [];
    cam.on("connection", ({ connected }) => seen.push(connected));
    await cam.connect();
    await cam.close();
    expect(seen).toEqual([true, false]);
  });
});

describe("clock", () => {
  it("estimates the service clock and locks with a frame time", async () => {
    const cam = await connect("a");
    await waitFor(() => cam.clockOffset !== null);
    expect(cam.rtt).toBeGreaterThanOrEqual(0);
    const { t } = await cam.call("ping");
    expect(Math.abs(cam.toServiceTime(performance.now())! - t)).toBeLessThan(0.5);

    // No video on the fake service: the request reaches it and is refused for that reason.
    const error = await cam.lock(0.1, -0.05, { frameTime: performance.now() }).catch((e) => e);
    expect(error.kind).toBe("unavailable");
  });
});

/** A socket that opens, answers `hello`, and then never replies. */
class SilentSocket {
  static last: SilentSocket;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor() {
    SilentSocket.last = this;
    setTimeout(() => {
      this.readyState = 1;
      this.onopen?.();
    });
  }

  send(text: string) {
    const msg = JSON.parse(text);
    if (msg.method === "hello") {
      const result = { protocol: 1, sdk: "test", t: 0, controller: true };
      setTimeout(() => this.onmessage?.({ data: JSON.stringify({ jsonrpc: "2.0", id: msg.id, result }) }));
    }
  }

  close() {
    this.readyState = 3;
    setTimeout(() => this.onclose?.());
  }
}

describe("failures", () => {
  const silent = () => {
    const cam = new RemoteCamera("ws://silent", {
      WebSocket: SilentSocket as unknown as typeof WebSocket,
      reconnect: false,
    });
    cameras.push(cam);
    return cam;
  };

  it("times out a call with no reply", async () => {
    const cam = silent();
    await cam.connect();
    await expect(cam.call("get_state", {}, { timeout: 50 })).rejects.toMatchObject({
      kind: "timeout",
    });
  });

  it("fails calls in flight when the connection drops", async () => {
    const cam = silent();
    await cam.connect();
    const call = cam.call("get_state");
    SilentSocket.last.close();
    await expect(call).rejects.toMatchObject({ kind: "disconnected" });
  });
});

describe("connection", () => {
  it("fails calls while disconnected", async () => {
    const cam = new RemoteCamera(service.url);
    await expect(cam.call("ping")).rejects.toMatchObject({ kind: "disconnected" });
  });

  it("gives up on an unreachable service without reconnect", async () => {
    const cam = new RemoteCamera("ws://127.0.0.1:1", { reconnect: false });
    cameras.push(cam);
    await expect(cam.connect()).rejects.toMatchObject({ kind: "disconnected" });
  });
});
