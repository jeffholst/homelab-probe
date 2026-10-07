import { Link } from "react-router-dom";

import antUrl from "../../assets/brand/ant.webp";
import logoUrl from "../../assets/brand/logo.webp";

export const APP_TITLE = "Homelab Probe";

/** The ant mascot and the name, as a home link in the header (or plain, where there is nowhere to go). */
export function BrandMark({ to }: { to?: string }) {
  const content = (
    <>
      <img className="brand__ant" src={antUrl} alt="" width={48} height={32} />
      <span className="brand__word">
        Homelab <span className="brand__accent">Probe</span>
      </span>
    </>
  );
  return to === undefined ? (
    <span className="brand">{content}</span>
  ) : (
    <Link className="brand" to={to} aria-label={`${APP_TITLE}, home`}>
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
