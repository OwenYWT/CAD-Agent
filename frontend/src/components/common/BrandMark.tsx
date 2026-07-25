import { useState } from "react";
import logoUrl from "../../assets/wordswave-logo.jpg";

interface BrandMarkProps {
  className?: string;
  description?: string;
  descriptionClassName?: string;
  nameClassName?: string;
  showName?: boolean;
  size?: "default" | "login";
  tone?: "default" | "inverse";
}

export function BrandMark({
  className = "",
  description,
  descriptionClassName = "",
  nameClassName = "",
  showName = true,
  size = "default",
  tone = "default",
}: BrandMarkProps) {
  const [failed, setFailed] = useState(false);
  const markSize = size === "login" ? "h-8 w-8 rounded-lg" : "h-6 w-6 rounded-lg";
  const fallbackTone = tone === "inverse"
    ? "bg-white text-slate-950"
    : "bg-[var(--ink)] text-white";

  return (
    <span
      aria-label={showName ? undefined : "WordsWave"}
      className={`inline-flex min-w-0 items-center gap-2.5 ${className}`}
      role={showName ? undefined : "img"}
    >
      {failed ? (
        <span aria-hidden="true" className={`grid shrink-0 place-items-center text-xs font-semibold ${markSize} ${fallbackTone}`}>W</span>
      ) : (
        <img
          alt=""
          aria-hidden="true"
          className={`shrink-0 object-contain ${markSize}`}
          onError={() => setFailed(true)}
          src={logoUrl}
        />
      )}
      {showName ? (
        <span className="min-w-0">
          <span className={`block font-semibold ${nameClassName}`}>WordsWave</span>
          {description ? <span className={`block ${descriptionClassName}`}>{description}</span> : null}
        </span>
      ) : null}
    </span>
  );
}
