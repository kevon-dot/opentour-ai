import 'server-only';

/** Apply one deadline to a server operation and the complete bounded response body. */
export async function boundedServerResponse(
  invoke: (signal: AbortSignal, assertTime: () => void) => Promise<Response>,
  options: { timeoutMs: number; maxBytes: number; failure: () => Error; signal?: AbortSignal },
): Promise<Response> {
  const controller = new AbortController();
  const started = performance.now();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs);
  const signal = options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal;
  let rejectAbort: () => void = () => undefined;
  const aborted = new Promise<never>((_resolve, reject) => {
    rejectAbort = () => reject(options.failure());
    signal.addEventListener('abort', rejectAbort, { once: true });
  });
  const wait = <T>(work: Promise<T>): Promise<T> => Promise.race([work, aborted]);
  const assertTime = () => {
    const now = performance.now();
    if (signal.aborted || !Number.isFinite(now) || !Number.isFinite(started)
        || now < started || now - started >= options.timeoutMs) throw options.failure();
  };
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let source: Response | undefined;
  try {
    assertTime();
    const pending = invoke(signal, assertTime);
    void pending.then(response => {
      if (signal.aborted) void response.body?.cancel().catch(() => undefined);
    }, () => undefined);
    const response = source = await wait(pending);
    assertTime();
    if (!response.body) return response;
    reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const { value, done } = await wait(reader.read());
      assertTime();
      if (done) break;
      size += value.byteLength;
      if (size > options.maxBytes) throw options.failure();
      chunks.push(value);
    }
    return new Response(Buffer.concat(chunks), { status: response.status,
      statusText: response.statusText, headers: response.headers });
  } finally {
    clearTimeout(timer);
    signal.removeEventListener('abort', rejectAbort);
    controller.abort();
    if (reader) void reader.cancel().catch(() => undefined);
    else if (source?.body) void source.body.cancel().catch(() => undefined);
  }
}
