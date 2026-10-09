import { Link } from "react-router-dom";

import antUrl from "../../assets/brand/ant.webp";
import logoUrl from "../../assets/brand/logo.webp";

export const APP_TITLE = "Homelab Probe";

/** The name, optionally with its mascot, as a home link or a plain mark. */
export function BrandMark({ to, textOnly = false }: { to?: string; textOnly?: boolean }) {
  const className = textOnly ? "brand brand--text" : "brand";
  const content = textOnly ? (
    <span className="brand__word">HOMELAB <span className="brand__accent">PROBE</span></span>
  ) : (
    <>
      <img className="brand__ant" src={antUrl} alt="" width={48} height={32} />
      <span className="brand__word">
        Homelab <span className="brand__accent">Probe</span>
      </span>
    </>
  );
  return to === undefined ? (
    <span className={className}>{content}</span>
  ) : (
    <Link className={className} to={to} aria-label={`${APP_TITLE}, home`}>
      {content}
    </Link>
  );
}

/** The whole logo, large, for the screens before the login (the login itself, the setup). */
export function BrandHero() {
  return (
    <div className="brand-hero">
      <img className="brand-hero__logo" src={logoUrl} alt={APP_TITLE} width={900} height={450} />
    </div>
  );
}
