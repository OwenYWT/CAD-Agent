import { useState } from "react";
import logoUrl from "../../assets/wordswave-logo.jpg";

interface BrandMarkProps {
  className?: string;
  description?: string;
  descriptionClassName?: string;
  nameAs?: "span" | "h1";
  nameClassName?: string;
  showName?: boolean;
  size?: "default" | "login";
  tone?: "default" | "inverse";
}

export function BrandMark({
  className = "",
  description,
  descriptionClassName = "",
  nameAs = "span",
  nameClassName = "",
  showName = true,
  size = "default",
  tone = "default",
}: BrandMarkProps) {
  const [failed, setFailed] = useState(false);
  const Name = nameAs;
  const markSize = size === "login" ? "h-9 w-9 rounded-lg" : "h-7 w-7 rounded-lg";
  const fallbackTone = tone === "inverse"
    ? "bg-white text-[var(--ink)]"
    : "bg-[var(--ink)] text-white";

  return (
    <span
      aria-label={showName ? undefined : "WordsWave"}
      className={`inline-flex min-w-0 items-center gap-2.5 ${className}`}
      role={showName ? undefined : "img"}
    >
      {failed ? (
        <span aria-hidden="true" className={`grid shrink-0 place-items-center type-body  ${markSize} ${fallbackTone}`}>W</span>
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
          <Name className={`block  ${nameClassName}`}>WordsWave</Name>
          {description ? <span className={`block ${descriptionClassName}`}>{description}</span> : null}
        </span>
      ) : null}
    </span>
  );
}
