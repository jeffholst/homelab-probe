import { useEffect, useId, useRef, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useLogout, useSession } from "../auth/session";
import { DESKTOP_QUERY, useMediaQuery } from "../lib/useMediaQuery";
import { useMeta } from "../app/meta";
import { Text } from "./Text";
import { ThemeSwitch } from "./ThemeSwitch";

interface NavItem {
  to: string;
  label: string;
  end?: boolean;
}

/** Only pages that work are listed: a page issue adds its row here when the page ships. */
export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", label: "Home", end: true },
  { to: "/profile", label: "Profile" },
];

/**
 * The frame of every page after the login: a skip link, a banner, the main navigation (a drawer on a phone or tablet,
 * a sidebar from 56rem up), the page itself in `<main>` and a footer. Moving to another page moves focus to its
 * heading, so a keyboard or screen reader user starts at the top of the new page. While the drawer is open the rest of
 * the page is inert, Escape closes it and focus returns to the menu button.
 */
export function Shell() {
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const [menuOpen, setMenuOpen] = useState(false);
  const drawerOpen = menuOpen && !desktop;
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const firstRender = useRef(true);
  const navId = useId();
  const session = useSession();
  const logout = useLogout();
  const meta = useMeta();

  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    const heading = mainRef.current?.querySelector<HTMLElement>("h1");
    if (heading) {
      heading.tabIndex = -1;
      heading.focus();
    } else {
      mainRef.current?.focus();
    }
  }, [location.pathname]);

  // The menu button is inert while the drawer is open, so focus goes back to it only once the page has been
  // re-rendered without the drawer.
  const restoreFocus = useRef(false);
  function closeDrawer(returnFocus: boolean) {
    restoreFocus.current = returnFocus;
    setMenuOpen(false);
  }

  useEffect(() => {
    if (!drawerOpen && restoreFocus.current) {
      restoreFocus.current = false;
      menuButton.current?.focus();
    }
  }, [drawerOpen]);

  useEffect(() => {
    if (!drawerOpen) return;
    closeButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        restoreFocus.current = true;
        setMenuOpen(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [drawerOpen]);

  const user = session.data;
  return (
    <div className="app">
      <a className="skip-link" href="#main" inert={drawerOpen}>
        Skip to main content
      </a>
      <header className="topbar" inert={drawerOpen}>
        <button
          ref={menuButton}
          type="button"
          className="button button--secondary menu-button"
          aria-expanded={drawerOpen}
          aria-controls={navId}
          onClick={() => {
            setMenuOpen(true);
          }}
        >
          Menu
        </button>
        <p className="brand">Homelab Probe</p>
        {meta.data?.demo === true && <span className="pill pill--info">Demo data</span>}
      </header>
      <nav
        id={navId}
        className="nav"
        aria-label="Main"
        data-open={drawerOpen}
      >
        <div className="nav__head">
          <button
            ref={closeButton}
            type="button"
            className="button button--secondary nav__close"
            onClick={() => {
              closeDrawer(true);
            }}
          >
            Close menu
          </button>
        </div>
        <ul className="nav__list">
          {NAV_ITEMS.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.to}
                end={item.end ?? false}
                className="nav__link"
                onClick={() => {
                  closeDrawer(false);
                }}
              >
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
        <div className="nav__account">
          {user && (
            <p className="nav__who">
              Signed in as <strong><Text value={user.username} /></strong>
              <br />
              <span className="muted">{user.role === "admin" ? "Administrator" : "Viewer"}</span>
            </p>
          )}
          <ThemeSwitch />
          <button
            type="button"
            className="button button--secondary"
            disabled={logout.isPending}
            onClick={() => {
              logout.mutate();
            }}
          >
            Log out
          </button>
        </div>
      </nav>
      {drawerOpen && (
        <div
          className="scrim"
          aria-hidden="true"
          onClick={() => {
            closeDrawer(true);
          }}
        />
      )}
      <main id="main" className="main" ref={mainRef} tabIndex={-1} inert={drawerOpen}>
        <Outlet />
      </main>
      <footer className="footer" inert={drawerOpen}>
        <p>
          Homelab Probe{meta.data ? ` ${meta.data.version}` : ""}. Read-only: nothing here changes your controller.
        </p>
      </footer>
    </div>
  );
}
