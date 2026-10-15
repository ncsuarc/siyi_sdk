// React bindings: one RemoteCamera per provider, events as hook values.
//
//   <CameraProvider url="ws://192.168.144.30:8765" name="shimmer">
//     <App />
//   </CameraProvider>
//
//   const cam = useCamera();
//   const attitude = useCameraEvent("attitude");

import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { RemoteCamera, type RemoteCameraOptions } from "./client";
import type { EventName, Events } from "./protocol";

const CameraContext = createContext<RemoteCamera | null>(null);

export interface CameraProviderProps extends RemoteCameraOptions {
  url: string;
  children?: ReactNode;
}

/** Connects while mounted; reconnects by itself. Children render straight away. */
export function CameraProvider({ url, children, ...options }: CameraProviderProps) {
  // Options are read once per url; changing them later does not reconnect.
  const [camera, setCamera] = useState<RemoteCamera | null>(null);
  useEffect(() => {
    const cam = new RemoteCamera(url, options);
    setCamera(cam);
    cam.connect().catch(() => undefined);
    return () => {
      void cam.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);
  return camera ? createElement(CameraContext.Provider, { value: camera }, children) : null;
}

/** The provider's RemoteCamera. */
export function useCamera(): RemoteCamera {
  const camera = useContext(CameraContext);
  if (!camera) throw new Error("useCamera must be used inside <CameraProvider>");
  return camera;
}

/** The latest value of an event, re-rendering on each new one (undefined until the first). */
export function useCameraEvent<E extends EventName>(event: E): Events[E] | undefined {
  const camera = useCamera();
  const subscribe = useCallback((onChange: () => void) => camera.on(event, onChange), [camera, event]);
  return useSyncExternalStore(subscribe, () => camera.latest(event));
}

/** Connected to the service. */
export function useConnected(): boolean {
  return useCameraEvent("connection")?.connected ?? false;
}

/** Who has control: `{ controller, holder }`. */
export function useControl(): Events["control"] {
  return useCameraEvent("control") ?? { controller: false, holder: null };
}
