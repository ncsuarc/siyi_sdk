// Starts test/fake_service.py (the real camera service on a fake camera) for a test file.

import { spawn, type ChildProcess } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../../..");

export interface FakeService {
  url: string;
  stop: () => Promise<void>;
}

/** SIYI_PYTHON picks the interpreter (default: `uv run python`). */
export function startService(): Promise<FakeService> {
  const [command, ...args] = (process.env.SIYI_PYTHON ?? "uv run python").split(" ");
  const child: ChildProcess = spawn(command, [...args, resolve(here, "fake_service.py")], {
    cwd: root,
    stdio: ["pipe", "pipe", "inherit"],
  });
  return new Promise((resolvePromise, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error("fake service did not start")), 30_000);
    child.on("error", reject);
    child.on("exit", (code) => reject(new Error(`fake service exited with ${code}`)));
    child.stdout!.on("data", (chunk: Buffer) => {
      output += chunk.toString();
      const match = /PORT (\d+)/.exec(output);
      if (!match) return;
      clearTimeout(timer);
      resolvePromise({
        url: `ws://127.0.0.1:${match[1]}`,
        stop: () =>
          new Promise((done) => {
            child.removeAllListeners("exit");
            child.on("exit", () => done());
            child.stdin!.end();
          }),
      });
    });
  });
}

export async function waitFor(condition: () => boolean, timeout = 2000): Promise<void> {
  const deadline = Date.now() + timeout;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error("condition not met in time");
    await new Promise((r) => setTimeout(r, 10));
  }
}

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
