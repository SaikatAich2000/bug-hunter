/** Mirrors server password policy for early UI feedback; server is authoritative. */

/** Must match server PASSWORD_MIN_LENGTH. */
export const PASSWORD_MIN_LENGTH = 8;

export const PASSWORD_HINT = `At least ${PASSWORD_MIN_LENGTH} characters, including a letter and a number`;

/** Error message if the password fails policy, else null. */
export function validatePassword(pw: string): string | null {
  // legacy default password; server accepts it, so we must too
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

/** Lightweight email format check; server is authoritative. */
export function isValidEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}
