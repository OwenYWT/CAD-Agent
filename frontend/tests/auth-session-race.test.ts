import { test } from "node:test";
import assert from "node:assert/strict";
import { authFetch, getAuthToken, saveAuthSession, LOCAL_DEV_USER } from "../src/auth.ts";

test("a late 401 can only clear the bearer used by that request", async t => {
  const storage = new Map<string, string>();
  t.mock.method(globalThis, "fetch", () => new Promise<Response>(resolve => { finish = resolve; }));
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "window", {configurable: true, value: {location: {search: ""}}});
  Object.defineProperty(globalThis, "localStorage", {configurable: true, value: {
    getItem: (k: string) => storage.get(k) ?? null, setItem: (k: string,v: string) => storage.set(k,v),
    removeItem: (k: string) => storage.delete(k),
  }});
  t.after(() => {
    if (previousWindow) Object.defineProperty(globalThis, "window", previousWindow); else Reflect.deleteProperty(globalThis, "window");
    if (previousStorage) Object.defineProperty(globalThis, "localStorage", previousStorage); else Reflect.deleteProperty(globalThis, "localStorage");
  });
  let finish!: (response: Response) => void;
  saveAuthSession({token: "first", user: LOCAL_DEV_USER});
  const pending = authFetch("/api/auth/me");
  saveAuthSession({token: "refreshed", user: LOCAL_DEV_USER});
  finish(new Response(null, {status: 401}));
  await pending;
  assert.equal(getAuthToken(), "refreshed");
  const rejected = authFetch("/api/auth/me");
  finish(new Response(null, {status: 401}));
  await rejected;
  assert.equal(getAuthToken(), null);
});
