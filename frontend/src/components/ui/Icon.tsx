import type { SVGProps } from "react";

export type IconName =
  | "box"
  | "chevron-down"
  | "chevron-right"
  | "clock"
  | "code"
  | "download"
  | "eye"
  | "eye-off"
  | "file"
  | "history"
  | "layers"
  | "message"
  | "minus"
  | "more"
  | "plus"
  | "redo"
  | "rotate"
  | "send"
  | "settings"
  | "shield-check"
  | "sliders"
  | "trash"
  | "undo"
  | "user"
  | "x";

const paths: Record<IconName, React.ReactNode> = {
  box: <><path d="M21 8 12 3 3 8l9 5 9-5Z"/><path d="m3 8 9 5 9-5"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/></>,
  "chevron-down": <path d="m6 9 6 6 6-6" />,
  "chevron-right": <path d="m9 18 6-6-6-6" />,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  code: <><path d="m8 9-3 3 3 3"/><path d="m16 9 3 3-3 3"/><path d="m14 5-4 14"/></>,
  download: <><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></>,
  eye: <><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/></>,
  "eye-off": <><path d="m3 3 18 18"/><path d="M10.6 10.7a2 2 0 0 0 2.7 2.7"/><path d="M9.9 5.2A10.7 10.7 0 0 1 12 5c6 0 9.5 7 9.5 7a14 14 0 0 1-2.1 2.9"/><path d="M6.2 6.2C3.8 7.8 2.5 12 2.5 12s3.5 7 9.5 7a9.6 9.6 0 0 0 4-.9"/></>,
  file: <><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5"/></>,
  history: <><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></>,
  layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 16 9 5 9-5"/></>,
  message: <><path d="M4 5h16v11H8l-4 4z"/></>,
  minus: <path d="M5 12h14" />,
  more: <><circle cx="5" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none"/></>,
  plus: <><path d="M12 5v14"/><path d="M5 12h14"/></>,
  redo: <><path d="m15 7 4 4-4 4"/><path d="M19 11H9a5 5 0 0 0-5 5v1"/></>,
  rotate: <><path d="M20 7v5h-5"/><path d="M19 12a7 7 0 1 0-2 5"/></>,
  send: <><path d="m21 3-7 18-4-8-8-4 19-6Z"/><path d="M10 13 21 3"/></>,
  settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></>,
  "shield-check": <><path d="M12 3 20 6v5c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-3Z"/><path d="m8.5 12 2.2 2.2 4.8-5"/></>,
  sliders: <><path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/><circle cx="9" cy="6" r="2" fill="white"/><circle cx="15" cy="12" r="2" fill="white"/><circle cx="7" cy="18" r="2" fill="white"/></>,
  trash: <><path d="M4 7h16"/><path d="M9 3h6l1 4H8l1-4Z"/><path d="m6 7 1 14h10l1-14"/><path d="M10 11v6M14 11v6"/></>,
  undo: <><path d="m9 7-4 4 4 4"/><path d="M5 11h10a5 5 0 0 1 5 5v1"/></>,
  user: <><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></>,
  x: <><path d="m6 6 12 12"/><path d="m18 6-12 12"/></>,
};

interface IconProps extends SVGProps<SVGSVGElement> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 18, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={1.8}
      viewBox="0 0 24 24"
      width={size}
      {...props}
    >
      {paths[name]}
    </svg>
  );
}
