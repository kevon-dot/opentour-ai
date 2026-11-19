export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    const { startBffRuntime } = await import('./lib/server/bff-runtime.js');
    await startBffRuntime();
  }
}
