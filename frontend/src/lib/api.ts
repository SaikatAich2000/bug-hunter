/**
 * Typed fetch wrapper for the JSON API.
 *
 * Sends same-origin cookies. A 401 triggers a single redirect to /login.html
 * (deduplicated so concurrent in-flight requests don't double-redirect after a
 * session revoke). 204 resolves to null. FastAPI's {detail} field — string or
 * Pydantic validation list — is unwrapped into ApiError.message.
 */

export const API = "/api";

export class ApiError extends Error {
  status: number;
  silent: boolean;

  constructor(message: string, status: number, silent = false) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.silent = silent;
  }
}

let sessionRedirectInFlight = false;

export function bounceToLogin(): void {
  if (sessionRedirectInFlight) return;
  sessionRedirectInFlight = true;
  const next = encodeURIComponent(
    location.pathname + location.search + location.hash,
  );
  location.replace(`/login.html?next=${next}`);
}

/** Pull a readable message out of a FastAPI error body. */
function detailToMessage(body: unknown, fallback: string): string {
  if (!body || typeof body !== "object") return fallback;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // Pydantic validation errors: [{loc, msg, type}, ...]
    const msgs = detail
      .map((d) =>
        d && typeof d === "object" && "msg" in d
          ? String((d as { msg: unknown }).msg)
          : "",
      )
      .filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  }
  return fallback;
}

export interface ApiOptions extends Omit<RequestInit, "body"> {
  /** Serialised as JSON; sets Content-Type automatically. */
  json?: unknown;
  /** Raw body (e.g. FormData); takes precedence over `json`. */
  body?: BodyInit;
}

export async function api<T = unknown>(
  path: string,
  opts: ApiOptions = {},
): Promise<T> {
  const { json, headers, body, ...rest } = opts;
  const init: RequestInit = {
    credentials: "include",
    ...rest,
    headers: {
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(headers as Record<string, string> | undefined),
    },
    body: body !== undefined ? body : json !== undefined ? JSON.stringify(json) : undefined,
  };

  const res = await fetch(`${API}${path}`, init);

  if (res.status === 401) {
    bounceToLogin();
    // Page is navigating away; suppress any caller-side toast.
    throw new ApiError("Session expired", 401, true);
  }

  if (res.status === 204) return null as T;

  const text = await res.text();
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = text;
  }

  if (!res.ok) {
    throw new ApiError(
      detailToMessage(parsed, `Request failed (${res.status})`),
      res.status,
    );
  }
  return parsed as T;
}

/** Fetch a binary endpoint (e.g. XLSX export). Returns blob + filename. */
export async function apiBlob(
  path: string,
  opts: ApiOptions = {},
): Promise<{ blob: Blob; filename: string | null }> {
  const { json, headers, body, ...rest } = opts;
  const res = await fetch(`${API}${path}`, {
    credentials: "include",
    ...rest,
    headers: {
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(headers as Record<string, string> | undefined),
    },
    body: body !== undefined ? body : json !== undefined ? JSON.stringify(json) : undefined,
  });
  if (res.status === 401) {
    bounceToLogin();
    throw new ApiError("Session expired", 401, true);
  }
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      msg = detailToMessage(await res.json(), msg);
    } catch {
      /* binary body; keep fallback message */
    }
    throw new ApiError(msg, res.status);
  }
  const cd = res.headers.get("Content-Disposition") || "";
  // RFC 5987 filename* takes priority (percent-encoded, handles non-ASCII).
  // The plain regex excludes '*' as its first char to avoid a partial match
  // against the filename* form that would produce a garbled name.
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(cd);
  const plain = /filename="?([^";*][^";]*)"?/i.exec(cd);
  let raw = "";
  if (star) {
    try { raw = decodeURIComponent(star[1].trim()); } catch { raw = star[1].trim(); }
  } else if (plain) {
    raw = plain[1].trim();
  }
  // Strip path separators so a server-supplied name can't influence download location.
  const filename: string | null = raw ? (raw.replace(/[\\/]/g, "_").trim() || null) : null;
  return { blob: await res.blob(), filename };
}
