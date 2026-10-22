// Inline brand marks: "hunter" (H-Trap, the Bug Hunter trademark) and "sleuth" (Sleuth Lens, the assistant).
// Colours come from the --bm-* tokens in styles.css, so the mark follows the light/dark theme.
// tone="dark" pins the dark palette for use on coloured surfaces (e.g. the Sleuth button gradient).
const SHAPES = {
  hunter: (
    <>
      <path d="M26 44h48M26 56h48M26 68h48" className="bm-s-acc" strokeWidth="4" strokeLinecap="round" fill="none" />
      <rect x="10" y="8" width="16" height="84" rx="8" className="bm-f-ink" />
      <rect x="74" y="8" width="16" height="84" rx="8" className="bm-f-ink" />
      <ellipse cx="50" cy="57" rx="16" ry="19" className="bm-f-hot" />
      <circle cx="50" cy="36" r="7" className="bm-f-ink" />
      <path d="M50 41v34" className="bm-s-ko" strokeWidth="2.5" fill="none" />
    </>
  ),
  sleuth: (
    <>
      <line x1="64" y1="64" x2="90" y2="90" className="bm-s-gold" strokeWidth="13" strokeLinecap="round" />
      <circle cx="42" cy="42" r="30" className="bm-s-ink" strokeWidth="8" fill="none" />
      <path d="M24 36a20 20 0 0 1 12-13" className="bm-s-acc" strokeWidth="4" strokeLinecap="round" fill="none" />
      <circle cx="42" cy="47" r="15" className="bm-f-hot" />
      <ellipse cx="42" cy="32" rx="8" ry="5.5" className="bm-f-ink" />
      <path d="M42 35v27" className="bm-s-ko" strokeWidth="2.5" fill="none" />
      <g className="bm-f-ko">
        <circle cx="36" cy="45" r="2.6" />
        <circle cx="48" cy="45" r="2.6" />
        <circle cx="37" cy="54" r="2.2" />
        <circle cx="47" cy="54" r="2.2" />
      </g>
    </>
  ),
};

export default function BrandMark({ kind = "hunter", size, tone, className = "", label }) {
  const cls = `bm${tone === "dark" ? " bm-dark" : ""}${className ? ` ${className}` : ""}`;
  return (
    <svg
      className={cls}
      viewBox="0 0 100 100"
      width={size}
      height={size}
      role={label ? "img" : undefined}
      aria-label={label || undefined}
      aria-hidden={label ? undefined : "true"}
      focusable="false"
    >
      {SHAPES[kind] || SHAPES.hunter}
    </svg>
  );
}
