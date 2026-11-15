// Default test-web boundary. This is test discipline, not a hostile-code sandbox.
// A child receives this exact preload through wrapper-owned NODE_OPTIONS.
"use strict";

const denied = () => {
  const error = new Error("Network access denied by unit/contract command boundary");
  error.code = "OPENTOUR_NETWORK_DENIED";
  throw error;
};

const net = require("node:net");
const tls = require("node:tls");
const dgram = require("node:dgram");
const dns = require("node:dns");
net.connect = denied;
net.createConnection = denied;
net.Socket.prototype.connect = denied;
tls.connect = denied;
dgram.Socket.prototype.connect = denied;
dgram.Socket.prototype.send = denied;
for (const api of [dns, dns.promises]) {
  for (const key of Object.keys(api)) {
    if (key === "lookup" || key === "lookupService" || key === "reverse" || key.startsWith("resolve")) {
      if (typeof api[key] === "function") api[key] = denied;
    }
  }
  if (api.Resolver) {
    for (const key of Object.getOwnPropertyNames(api.Resolver.prototype)) {
      if (key.startsWith("resolve") || key === "reverse") {
        api.Resolver.prototype[key] = denied;
      }
    }
  }
}
globalThis.fetch = async () => denied();
globalThis.WebSocket = class {
  constructor() { denied(); }
};
const childProcess = require("node:child_process");
for (const name of ["spawn", "spawnSync", "fork", "exec", "execSync", "execFile", "execFileSync"]) {
  const original = childProcess[name];
  childProcess[name] = function (...args) {
    if (args.some((arg) => arg && !Array.isArray(arg) && typeof arg === "object" && arg.detached)) {
      const error = new Error("Detached child creation denied by unit/contract command boundary");
      error.code = "OPENTOUR_DETACHED_CHILD_DENIED";
      throw error;
    }
    return Reflect.apply(original, this, args);
  };
}
require("node:module").syncBuiltinESMExports();
