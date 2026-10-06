/**
 * The one error type of the API client.
 *
 * The server answers every failure with `{"error": CODE, "message": SENTENCE}` and a fixed sentence (never the text of
 * an exception), plus a few extra fields on some (`retry_after` on a 429, `candidates` on a 409). `status` 0 means the
 * request never got an answer (the network, a refused connection, an aborted fetch).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /** Seconds to wait before trying again, from `Retry-After` or the body's `retry_after` (a 429). */
  readonly retryAfter: number | undefined;
  /** The rest of the error body, as the server sent it. Untrusted: render it as text only. */
  readonly details: Readonly<Record<string, unknown>>;

  constructor(
    status: number,
    code: string,
    message: string,
    options: { retryAfter?: number | undefined; details?: Record<string, unknown> } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = options.retryAfter;
    this.details = options.details ?? {};
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

const STATUS_SENTENCES: Readonly<Record<number, string>> = {
  401: "Log in first.",
  403: "That is not allowed.",
  404: "That does not exist.",
  429: "Too many attempts. Try again in a moment.",
  502: "The server could not read the controller.",
  503: "The server is busy or not ready. Try again in a moment.",
  504: "The server timed out.",
};

/** What to say when the body is not the API's `{error, message}` (a proxy's error page, an empty answer). */
export function fallbackMessage(status: number): string {
  return STATUS_SENTENCES[status] ?? "The server answered with something unexpected.";
}

/** A positive whole number of seconds, or undefined. */
export function parseSeconds(value: unknown): number | undefined {
  const seconds = typeof value === "string" ? Number(value.trim()) : value;
  return typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : undefined;
}

/** Builds the error of a failed response from its status, `Retry-After` header and (already parsed) body. */
export function errorFromResponse(status: number, retryAfterHeader: string | null, body: unknown): ApiError {
  const fields = typeof body === "object" && body !== null && !Array.isArray(body) ? (body as Record<string, unknown>) : {};
  const { error, message, ...details } = fields;
  const retryAfter = parseSeconds(retryAfterHeader) ?? parseSeconds(details["retry_after"]);
  if (typeof error === "string" && typeof message === "string") {
    return new ApiError(status, error, message, { retryAfter, details });
  }
  return new ApiError(status, "bad_response", fallbackMessage(status), { retryAfter });
}
