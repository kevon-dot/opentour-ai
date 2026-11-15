'use strict';
// Test discipline, not a hostile-code security boundary. Catch attempts even if a test catches the error.
const net = require('node:net');
const dns = require('node:dns');
const origin = new URL(process.env.OPENTOUR_FIXTURE_ORIGIN);
const violations = [];
globalThis[Symbol.for('opentour.network.violations')] = violations;
// Keep managed Playwright browser processes in the outer command supervisor's group.
// Playwright's negative-PID termination is translated only for this process's live child handles.
const children = require('node:child_process');
const spawn = children.spawn;
const handles = new Map();
const signalProcess = process.kill.bind(process);
children.spawn = function (command, args, options) {
  const child = spawn.call(this, command, args, { ...options, detached: false });
  if (child.pid) {
    handles.set(child.pid, child);
    child.once('close', () => handles.delete(child.pid));
  }
  return child;
};
process.kill = function (pid, signal) {
  if (pid < 0) {
    const child = handles.get(-pid);
    if (!child || child.exitCode !== null || child.signalCode !== null) {
      const error = new Error('Unowned process group'); error.code = 'ESRCH'; throw error;
    }
    return child.kill(signal);
  }
  return signalProcess(pid, signal);
};
process.once('exit', () => { for (const child of handles.values()) if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
const fail = () => { violations.push('FORBIDDEN_NETWORK'); throw new Error('FORBIDDEN_NETWORK'); };
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const input = Array.isArray(args[0]) ? args[0] : args;
  const options = typeof input[0] === 'object' ? input[0] : { port: input[0], host: typeof input[1] === 'string' ? input[1] : 'localhost' };
  if (options.path || String(options.port) !== origin.port || !['127.0.0.1', 'localhost'].includes(options.host ?? 'localhost')) return fail();
  return connect.apply(this, args);
};
const lookup = dns.lookup;
dns.lookup = function (hostname, ...args) { if (!['127.0.0.1', 'localhost'].includes(hostname)) return fail(); return lookup.call(this, hostname, ...args); };
for (const name of ['resolve', 'resolve4', 'resolve6', 'resolveAny', 'reverse']) dns[name] = fail;
for (const name of ['lookup', 'resolve', 'resolve4', 'resolve6', 'resolveAny', 'reverse']) dns.promises[name] = async () => fail();
require('node:dgram').createSocket = fail;
require('node:module').syncBuiltinESMExports();
