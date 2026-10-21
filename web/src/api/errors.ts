/**
 * Errors from /ws/live requests.
 *
 * Server codes (pyaerial.api.spec.WS_ERROR_CODES): bad_request, not_found,
 * rate_limited, history_unavailable, live_unavailable, internal_error.
 * Client-side codes: timeout, disconnected, rejected, queue_full.
 */
export type ApiErrorCode =
  | 'bad_request'
  | 'not_found'
  | 'rate_limited'
  | 'history_unavailable'
  | 'live_unavailable'
  | 'internal_error'
  | 'timeout'
  | 'disconnected'
  | 'rejected'
  | 'queue_full'
  | 'unknown';

export class ApiError extends Error {
  readonly code: ApiErrorCode;

  constructor(code: ApiErrorCode, message: string) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
  }
}

const KNOWN_CODES = new Set<ApiErrorCode>([
  'bad_request',
  'not_found',
  'rate_limited',
  'history_unavailable',
  'live_unavailable',
  'internal_error',
]);

export function apiErrorFromResponse(code: unknown, message: unknown): ApiError {
  const known = typeof code === 'string' && KNOWN_CODES.has(code as ApiErrorCode);
  return new ApiError(
    known ? (code as ApiErrorCode) : 'unknown',
    typeof message === 'string' && message ? message : 'Request failed',
  );
}

export function errorCode(err: unknown): ApiErrorCode {
  if (err instanceof ApiError) return err.code;
  // Older servers report "not found" only in the message text.
  if (err instanceof Error && /not found/i.test(err.message)) return 'not_found';
  return 'unknown';
}

/** Shown when the server refuses the socket (close 1008). */
export const ORIGIN_REJECTED_MESSAGE =
  "The server doesn't allow this portal's address. Add it to web.allowed_hosts or web.origins in the config.";

/**
 * Friendly text for a failed request. Codes with a specific cause get their
 * own message; everything else uses `fallback` ("Failed to load flights.").
 */
export function describeError(err: unknown, fallback: string): string {
  switch (errorCode(err)) {
    case 'rate_limited':
      return 'Too many requests right now. Wait a moment and try again.';
    case 'history_unavailable':
      return 'History is unavailable: the archive is not connected.';
    case 'live_unavailable':
      return 'Live data is unavailable: the live store (Redis) is unreachable.';
    case 'timeout':
      return 'The server took too long to answer. Try again.';
    case 'disconnected':
      return 'Lost the connection to the server. Retrying…';
    case 'rejected':
      return ORIGIN_REJECTED_MESSAGE;
    case 'not_found':
      return 'Not found.';
    default:
      return fallback;
  }
}
