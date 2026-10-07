import { type ReactNode } from "react";

import { BrandHero, BrandMark } from "../components/brand/Brand";
import { SiteFooter } from "../components/SiteFooter";
import { ThemeSwitch } from "../components/ThemeSwitch";

/**
 * The frame of the screens before anyone is logged in (the login, the setup): the theme switch, the whole logo, the
 * page in `<main>` and the same footer as every other page. `wide` gives a page with more to show (the setup) room.
 */
export function AuthLayout({ children, wide = false, hero = true }: { children: ReactNode; wide?: boolean; hero?: boolean }) {
  return (
    <div className="auth">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <header className="auth__top">
        {hero ? <span className="visually-hidden">Homelab Probe</span> : <BrandMark />}
        <ThemeSwitch compact />
      </header>
      <main id="main" className={wide ? "auth__main auth__main--wide" : "auth__main"} tabIndex={-1}>
        {hero && <BrandHero />}
        {children}
      </main>
      <SiteFooter />
    </div>
  );
}
