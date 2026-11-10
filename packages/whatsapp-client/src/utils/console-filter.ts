/**
 * Drop libsignal's session dumps from the console.
 *
 * libsignal@6.0.0 (pulled in by Baileys) logs whole `SessionEntry` objects via
 * `console.info` / `console.warn` in `session_record.js` — including `privKey`,
 * `rootKey` and `chainKey` — straight to stdout, i.e. into the Docker logs.
 * Baileys' pino logger can't silence it because it bypasses pino. The filter
 * wraps whatever `console.info`/`console.warn` are at install time (Sentry's
 * console integration, when enabled), so dropped lines never become Sentry
 * breadcrumbs either. Every other console line passes through unchanged.
 */

const LIBSIGNAL_SESSION_PREFIXES = [
  'Closing session:',
  'Opening session:',
  'Removing old closed session:',
  'Session already closed',
] as const;

const INSTALLED = Symbol.for('ai-boilerplate.libsignalConsoleFilter');

/** True for a console call that is one of libsignal's session-key dumps. */
export function isLibsignalSessionDump(args: unknown[]): boolean {
  const first = args[0];
  return typeof first === 'string' && LIBSIGNAL_SESSION_PREFIXES.some((p) => first.startsWith(p));
}

type ConsoleMethod = (...args: unknown[]) => void;

function wrap(original: ConsoleMethod): ConsoleMethod {
  const filtered: ConsoleMethod = (...args) => {
    if (isLibsignalSessionDump(args)) return;
    original(...args);
  };
  (filtered as unknown as Record<symbol, boolean>)[INSTALLED] = true;
  return filtered;
}

/** Install the filter on `console.info` and `console.warn`. Idempotent. */
export function installLibsignalConsoleFilter(): void {
  for (const method of ['info', 'warn'] as const) {
    const current = console[method] as ConsoleMethod;
    if ((current as unknown as Record<symbol, boolean>)[INSTALLED]) continue;
    console[method] = wrap(current.bind(console));
  }
}
