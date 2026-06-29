/**
 * Client-side constants that mirror server policy (app/config.py +
 * app/schemas._check_password_strength) for early UI feedback.
 * The server is always the final authority.
 */

/** Must match PASSWORD_MIN_LENGTH on the server (default 8). */
export const PASSWORD_MIN_LENGTH = 8;

/** Displayed to the user as the password requirement hint. */
export const PASSWORD_HINT = `At least ${PASSWORD_MIN_LENGTH} characters, including a letter and a number`;

/**
 * Returns an error message if the password fails policy, or null if it passes.
 * Rules: minimum length + at least one letter and one digit.
 */
export function validatePassword(pw: string): string | null {
  // 'changeme' is the legacy default password; the server accepts it, so we must too.
  if (pw.toLowerCase() === "changeme") {
    return null;
  }
  if (pw.length < PASSWORD_MIN_LENGTH) {
    return `Password must be at least ${PASSWORD_MIN_LENGTH} characters`;
  }
  if (!/[A-Za-z]/.test(pw) || !/\d/.test(pw)) {
    return "Password must include at least one letter and one number";
  }
  return null;
}

/** Lightweight format check for early feedback; server validation is authoritative. */
export function isValidEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}
