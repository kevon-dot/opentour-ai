'use strict';

const https = require('node:https');
const REF = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const OPS = new Set(['discover_sources', 'submit_intake', 'get_run']);
const HISTORICAL = ['pair_ref', 'source_revision', 'correction_revision', 'policy_revision', 'idempotency_key'];
const STARTER = ['policy_resource_ref', 'source_revision', 'setup_policy_revision', 'idempotency_key'];
const LIVE = ['feed_binding_ref', 'source_revision', 'idempotency_key'];

class BoundaryError extends Error {
  constructor(code) { super(code); this.code = code; }
}
const hold = (code = 'invalid_reference_metadata') => { throw new BoundaryError(code); };
const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const keys = (v, wanted) => object(v) && Object.keys(v).sort().join('\0') === [...wanted].sort().join('\0');
const ref = (v) => typeof v === 'string' && REF.test(v);
const refs = (v, names) => keys(v, names) && names.every((k) => ref(v[k]));

function connection(auth) {
  if (!object(auth) || !ref(auth.connection_alias) || typeof auth.audience !== 'string' ||
      !/^[A-Za-z0-9_-]{1,64}$/.test(auth.audience || '') ||
      typeof auth.access_token !== 'string' || !/^[A-Za-z0-9_-]{43,128}$/.test(auth.access_token)) {
    hold('connection_configuration_required');
  }
  let url;
  try { url = new URL(auth.origin); } catch { hold('connection_configuration_required'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash ||
      auth.origin !== `https://${url.host}` || !/^[a-z0-9.-]+(?::[0-9]+)?$/.test(url.host) ||
      url.port === '0' || auth.origin.length > 300) hold('connection_configuration_required');
  return { origin: auth.origin, audience: auth.audience, token: auth.access_token };
}

function argumentsFor(operation, input) {
  if (!OPS.has(operation) || !object(input)) hold('invalid_intake_arguments');
  if (operation === 'discover_sources') {
    if (Object.keys(input).some((k) => !['limit', 'cursor'].includes(k)) ||
        ('limit' in input && (!Number.isInteger(input.limit) || input.limit < 1 || input.limit > 50)) ||
        ('cursor' in input && !ref(input.cursor))) hold('invalid_intake_arguments');
  } else if (operation === 'get_run') {
    if (!refs(input, ['run_ref'])) hold('invalid_intake_arguments');
  } else {
    const mode = input.mode || 'historical';
    const wanted = mode === 'historical' ? HISTORICAL : mode === 'live' ? LIVE : STARTER;
    let shapes = [wanted];
    if (mode === 'starter') shapes = [[...STARTER, 'creation_ref'], [...STARTER, 'workflow_ref']];
    if (!['historical', 'starter', 'live'].includes(mode) ||
        !shapes.some((shape) => refs(input, 'mode' in input ? ['mode', ...shape] : shape)) ||
        (mode !== 'historical' && !('mode' in input))) hold('invalid_intake_arguments');
  }
  return { ...input };
}

function metadata(operation, value, input) {
  if (!object(value)) hold();
  if (operation === 'discover_sources') {
    if (!keys(value, ['sources', 'next_cursor']) || !Array.isArray(value.sources) ||
        value.sources.length > (input.limit || 50) ||
        !(value.next_cursor === null || ref(value.next_cursor))) hold();
    for (const source of value.sources) {
      const ordinary = refs(source, ['source_kind', 'source_ref', 'source_revision']) &&
        ['policy', 'historical'].includes(source.source_kind);
      const live = refs(source, ['feed_binding_ref', 'binding_version', 'feed_kind', 'source_revision']) &&
        ['decision', 'independent_outcome'].includes(source.feed_kind);
      if (!ordinary && !live) hold();
    }
  } else if (operation === 'submit_intake') {
    if (refs(value, ['run_ref', 'state', 'correlation_ref'])) {
      // A previously admitted run can itself now be held; retain its identity.
    } else if (value.state === 'held') {
      const wanted = ['state', 'reason_code'];
      if ('successor_feed_binding_ref' in value) wanted.push('successor_feed_binding_ref');
      if (!refs(value, wanted)) hold();
    } else hold();
  } else {
    const wanted = ['run_ref', 'state', 'next_action', 'reason_code', 'workflow_ref'];
    const benchmark = ['benchmark_ref', 'version', 'benchmark_state'];
    const withBenchmark = benchmark.some((k) => k in value);
    if (withBenchmark) wanted.push(...benchmark);
    if (!keys(value, wanted) || value.run_ref !== input.run_ref ||
        !['run_ref', 'state', 'next_action', 'workflow_ref'].every((k) => ref(value[k])) ||
        !(value.reason_code === null || ref(value.reason_code)) ||
        (withBenchmark && (!ref(value.benchmark_ref) || !ref(value.benchmark_state) ||
          !(ref(value.version) || Number.isSafeInteger(value.version))))) hold();
  }
  // Re-encode the validated object, never the raw response or its headers.
  return JSON.parse(JSON.stringify(value));
}

function request(bound, operation, input) {
  const body = Buffer.from(JSON.stringify(input));
  if (body.length > 4096) hold('invalid_intake_arguments');
  return new Promise((resolve, reject) => {
    const fail = (code) => reject(new BoundaryError(code));
    let complete = false;
    const req = https.request(`${bound.origin}/v1/${operation}`, {
      method: 'POST', agent: false, minVersion: 'TLSv1.2', rejectUnauthorized: true,
      headers: {
        Origin: bound.origin, 'X-OpenTour-Audience': bound.audience,
        Authorization: `Bearer ${bound.token}`, 'Content-Type': 'application/json',
        'Content-Length': String(body.length), Accept: 'application/json', Connection: 'close',
      },
    }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        const code = res.statusCode === 409 ? 'idempotency_conflict' :
          res.statusCode === 429 ? 'intake_rate_limited' :
          [401, 403].includes(res.statusCode) ? 'intake_unavailable' : 'intake_outcome_unknown';
        fail(code); return;
      }
      if (!/^application\/json(?:;|$)/i.test(res.headers['content-type'] || '')) {
        res.destroy(); fail('invalid_reference_metadata'); return;
      }
      let size = 0;
      const chunks = [];
      res.on('data', (chunk) => {
        size += chunk.length;
        if (size > 32768) { res.destroy(); fail('invalid_reference_metadata'); }
        else chunks.push(chunk);
      });
      res.on('error', () => fail('intake_outcome_unknown'));
      res.on('end', () => {
        try {
          const result = metadata(operation, JSON.parse(Buffer.concat(chunks).toString('utf8')), input);
          complete = true; resolve(result);
        } catch { fail('invalid_reference_metadata'); }
      });
    });
    const deadline = setTimeout(() => req.destroy(), 4000);
    req.on('close', () => clearTimeout(deadline));
    req.on('error', () => { if (!complete) fail('intake_outcome_unknown'); });
    req.end(body);
  });
}

async function perform(z, bundle, operation) {
  try {
    const bound = connection(bundle.authData);
    const input = argumentsFor(operation, bundle.inputData || {});
    return await request(bound, operation, input);
  } catch (error) {
    // Never attach transport errors, request config, credentials or response bodies.
    const code = error instanceof BoundaryError ? error.code : 'intake_outcome_unknown';
    const status = { invalid_intake_arguments: 400, connection_configuration_required: 400,
      intake_unavailable: 403, idempotency_conflict: 409, intake_rate_limited: 429 }[code] || 503;
    throw new z.errors.Error(code, code, status);
  }
}

module.exports = { perform, connection, argumentsFor, metadata };
