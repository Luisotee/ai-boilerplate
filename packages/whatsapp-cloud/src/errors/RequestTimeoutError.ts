/**
 * Error thrown when a fetch request times out via AbortController.
 * Provides structured metadata for better error handling and logging.
 */
export class RequestTimeoutError extends Error {
  public readonly url: string;
  public readonly timeoutMs: number;
  public readonly timestamp: Date;

  constructor(url: string, timeoutMs: number) {
    super(`Request timeout after ${timeoutMs}ms: ${url}`);
    this.name = 'RequestTimeoutError';
    this.url = url;
    this.timeoutMs = timeoutMs;
    this.timestamp = new Date();

    // Maintain proper stack trace (V8 engines)
    if (Error.captureStackTrace) {
      Error.captureStackTrace(this, RequestTimeoutError);
    }
  }

  /**
   * Returns structured error data for logging.
   */
  toJSON() {
    return {
      name: this.name,
      message: this.message,
      url: this.url,
      timeoutMs: this.timeoutMs,
      timestamp: this.timestamp.toISOString(),
    };
  }
}
