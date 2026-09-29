/**
 * A non-2xx answer from the Graph API. `status` tells the send route whether
 * the message was definitely refused (4xx) or may have gone out (5xx).
 * `body` is for logs only: it can contain the recipient's phone number.
 */
export class GraphApiError extends Error {
  public readonly status: number;
  public readonly body: string;

  constructor(status: number, body: string) {
    super(`Graph API error ${status}: ${body}`);
    this.name = 'GraphApiError';
    this.status = status;
    this.body = body;
  }
}
