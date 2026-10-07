/**
 * The app's icons: small inline SVGs drawn with the current text colour (no icon font, no file to fetch, nothing the
 * content-security policy has to allow). They are decoration: `aria-hidden`, so every button or link that shows one
 * also carries a written name.
 */
const PATHS = {
  moon: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z",
  sun: "M12 4V2m0 20v-2m8-8h2M2 12h2m13.66-5.66 1.41-1.41M4.93 19.07l1.41-1.41m0-11.32L4.93 4.93m14.14 14.14-1.41-1.41M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  monitor: "M3 5h18v11H3zM8 20h8m-4-4v4",
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6 6l12 12M18 6 6 18",
  home: "M4 11 12 4l8 7v9h-5v-6H9v6H4z",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8a7 7 0 0 1 14 0",
  logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10",
  chevronDown: "m6 9 6 6 6-6",
  chevronRight: "m9 6 6 6-6 6",
  arrowLeft: "M19 12H5m6-6-6 6 6 6",
  arrowRight: "M5 12h14m-6-6 6 6-6 6",
  check: "m5 12.5 4.5 4.5L19 7",
  checkCircle: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm-4-9 3 3 5-6",
  alert: "M12 9v4m0 3.5v.5M10.3 3.9 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-10v6m0-9.5V8",
  xCircle: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM9 9l6 6m0-6-6 6",
  minusCircle: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM8 12h8",
  shield: "M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z",
  shieldCheck: "M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6zm-3.5 9 2.5 2.5 4.5-5",
  server: "M4 4h16v6H4zm0 10h16v6H4zM8 7h.01M8 17h.01",
  key: "M15 7a4 4 0 1 1-3.87 5H9v2H7v2H4v-3l5.13-5.13A4 4 0 0 1 15 7Zm1 0h.01",
  lock: "M6 11h12v10H6zm2 0V8a4 4 0 0 1 8 0v3",
  upload: "M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4",
  download: "M12 4v12m-5-5 5 5 5-5M4 20h16",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  external: "M14 4h6v6m0-6-9 9M18 14v6H4V6h6",
  bell: "M6 16V11a6 6 0 1 1 12 0v5l2 2H4zm4 4h4",
  pulse: "M3 12h4l3-7 4 14 3-7h4",
  refresh: "M20 11a8 8 0 0 0-14.9-3M4 4v4h4m-4 5a8 8 0 0 0 14.9 3M20 20v-4h-4",
  book: "M4 5a2 2 0 0 1 2-2h14v16H6a2 2 0 0 0-2 2zm0 16a2 2 0 0 1 2-2h14",
  code: "m8 8-4 4 4 4m8-8 4 4-4 4M14 5l-4 14",
  file: "M6 3h8l4 4v14H6zm8 0v4h4",
  sparkle: "M12 3v4m0 10v4M3 12h4m10 0h4M6 6l2.5 2.5m7 7L18 18M6 18l2.5-2.5m7-7L18 6",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Zm10 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z",
  eyeOff: "M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6C3.8 8.4 2 12 2 12s3.5 7 10 7a9.6 9.6 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2",
  history: "M3 12a9 9 0 1 0 3-6.7M3 4v5h5m4-1v5l3 2",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, className }: { name: IconName; className?: string }) {
  return (
    <svg
      className={className === undefined ? "icon" : `icon ${className}`}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
