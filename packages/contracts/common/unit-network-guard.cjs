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
require("node:module").syncBuiltinESMExports();
