/**
 * Shared client-side constants. These mirror the server policy
 * (app/config.py + app/schemas._check_password_strength) so the UI can give
 * early feedback; the server stays authoritative.
 */

/** Minimum password length — mirrors PASSWORD_MIN_LENGTH (default 8). */
export const PASSWORD_MIN_LENGTH = 8;

/** Human-readable statement of the real password policy. */
export const PASSWORD_HINT = `At least ${PASSWORD_MIN_LENGTH} characters, including a letter and a number`;

/**
 * Client-side password strength check mirroring the backend
 * (min length + at least one letter AND one digit). Returns an error
 * message string, or null when the password satisfies the policy.
 */
export function validatePassword(pw: string): string | null {
  if (pw.length < PASSWORD_MIN_LENGTH) {
    return `Password must be at least ${PASSWORD_MIN_LENGTH} characters`;
  }
  if (!/[A-Za-z]/.test(pw) || !/\d/.test(pw)) {
    return "Password must include at least one letter and one number";
  }
  return null;
}

/** Basic email-format check for early client feedback (server authoritative). */
export function isValidEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}
