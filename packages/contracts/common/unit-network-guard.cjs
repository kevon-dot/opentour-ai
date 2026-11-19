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
const path = require("node:path");
const probe = path.resolve(
  __dirname, "../../../services/control-api/tests/auth/private_browser_client_postgres_probe.mjs",
);
const port = Number(process.env.OPENTOUR_NODE_TLS_PORT);
const localTls = process.argv.length === 3 && process.argv[1] === probe
  && path.isAbsolute(process.argv[2])
  && process.execArgv.length === 1 && process.execArgv[0] === "--conditions=react-server"
  && Number.isInteger(port) && port > 0 && port <= 65535;
const socketConnect = net.Socket.prototype.connect;
const tlsConnect = tls.connect;
let connectingTls = false;

const localEndpoint = options => options && options.host === "127.0.0.1"
  && Number(options.port) === port && !options.path && !options.socket;
net.connect = denied;
net.createConnection = denied;
net.Socket.prototype.connect = localTls ? function (options, ...args) {
  if (!connectingTls || !(this instanceof tls.TLSSocket) || !localEndpoint(options)) return denied();
  return Reflect.apply(socketConnect, this, [options, ...args]);
} : denied;
tls.connect = localTls ? function (options, ...args) {
  if (!localEndpoint(options) || options.rejectUnauthorized !== true) return denied();
  connectingTls = true;
  try {
    return Reflect.apply(tlsConnect, this, [options, ...args]);
  } finally {
    connectingTls = false;
  }
} : denied;
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
