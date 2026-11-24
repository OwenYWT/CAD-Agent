import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { authFetch, deleteAuthAccount, getAuthToken, saveAuthSession, LOCAL_DEV_USER } from "../src/auth.ts";

beforeEach(t => {
  const storage = new Map<string, string>();
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
});

test("a late 401 can only clear the bearer used by that request", async t => {
  let finish!: (response: Response) => void;
  t.mock.method(globalThis, "fetch", () => new Promise<Response>(resolve => { finish = resolve; }));
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

test("account deletion keeps a live session and reconciles a lost receipt", async t => {
  saveAuthSession({token: "active", user: LOCAL_DEV_USER});
  const request = t.mock.method(globalThis, "fetch", async () => new Response("Account protected", {status: 400}));
  await assert.rejects(deleteAuthAccount(), /Account protected/);
  assert.equal(getAuthToken(), "active");

  let exists = true;
  request.mock.mockImplementation(async (input, init) => {
    if (init?.method === "DELETE") throw new TypeError("Connection lost");
    assert.ok(String(input).endsWith("/api/auth/me"));
    return exists ? Response.json({user: LOCAL_DEV_USER}) : new Response(null, {status: 401});
  });
  await assert.rejects(deleteAuthAccount(), /账号仍然有效/);
  assert.equal(getAuthToken(), "active");
  exists = false;
  await deleteAuthAccount();
  assert.equal(getAuthToken(), null);
});
