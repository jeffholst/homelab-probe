import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useMeta } from "../app/meta";
import { useLogout, useSession } from "../auth/session";
import { safeText } from "../lib/safeText";
import { DESKTOP_QUERY, useMediaQuery } from "../lib/useMediaQuery";
import { useTerminalServices } from "../terminal/context";
import { TerminalDock, type DockMode } from "../terminal/TerminalDock";
import { BrandMark } from "./brand/Brand";
import { SiteFooter } from "./SiteFooter";
import { Text } from "./Text";
import { ThemeSwitch } from "./ThemeSwitch";
import { Icon, type IconName } from "./ui/Icon";

interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  end?: boolean;
}

/** Only pages that work are listed: a page issue adds its row here when the page ships. */
export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", label: "Dashboard", icon: "home", end: true },
  { to: "/profile", label: "Profile", icon: "user" },
];

function roleName(role: string): string {
  return role === "admin" ? "Administrator" : "Viewer";
}

/** The first letter of a user name for the avatar, cleaned like any other account string ("?" when there is none). */
function initial(username: string): string {
  return Array.from(safeText(username))[0] ?? "?";
}

/**
 * The frame of every page after the login: a skip link, the sticky header (the brand, the main navigation and the
 * account menu from 56rem up; a menu button that opens a sheet with all of them below that), the page in `<main>` and
 * the footer. Moving to another page moves focus to its heading, so a keyboard or screen reader user starts at the top
 * of the new page. While the sheet is open the rest of the page is inert, Escape closes it and focus returns to the
 * menu button; the account menu closes with Escape or a click elsewhere.
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
  const terminal = useTerminalServices();
  const [dockMode, setDockMode] = useState<DockMode>("closed");

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

  // The menu button is inert while the sheet is open, so focus goes back to it only once the page has been
  // re-rendered without the sheet.
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
  const demo = meta.data?.demo === true && <span className="pill pill--info">Demo data</span>;
  const logOut = (
    <button
      type="button"
      className="button button--secondary button--block"
      disabled={logout.isPending}
      onClick={() => {
        logout.mutate();
      }}
    >
      <Icon name="logout" />
      Log out
    </button>
  );
  const terminalToggle = terminal && (
    <button
      type="button"
      className="button button--secondary terminal-toggle"
      aria-expanded={dockMode !== "closed" && dockMode !== "collapsed"}
      onClick={() => {
        setDockMode(dockMode === "open" || dockMode === "expanded" ? "closed" : "open");
        closeDrawer(false);
      }}
    >
      <Icon name="terminal" />
      Terminal
    </button>
  );
  const who = user && (
    <p className="who">
      <span className="avatar" aria-hidden="true">
        {initial(user.username)}
      </span>
      <span className="who__text">
        <span>
          <span className="visually-hidden">Signed in as </span>
          <strong>
            <Text value={user.username} />
          </strong>
        </span>
        <span className="muted small">{roleName(user.role)}</span>
      </span>
    </p>
  );

  return (
    <div className="app">
      <a className="skip-link" href="#main" inert={drawerOpen}>
        Skip to main content
      </a>
      <header className="site-header" inert={drawerOpen}>
        <div className="site-header__inner">
          {!desktop && (
            <button
              ref={menuButton}
              type="button"
              className="icon-button"
              aria-label="Menu"
              aria-expanded={drawerOpen}
              aria-controls={navId}
              onClick={() => {
                setMenuOpen(true);
              }}
            >
              <Icon name="menu" />
            </button>
          )}
          <BrandMark to="/" />
          {desktop && (
            <nav className="topnav" aria-label="Main">
              <ul className="topnav__list">
                {NAV_ITEMS.map((item) => (
                  <li key={item.to}>
                    <NavLink to={item.to} end={item.end ?? false} className="topnav__link">
                      <Icon name={item.icon} />
                      {item.label}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </nav>
          )}
          <div className="site-header__end">
            {demo}
            {desktop && terminalToggle}
            {desktop && user && <AccountMenu who={who} initialLetter={initial(user.username)} username={user.username} logOut={logOut} />}
          </div>
        </div>
      </header>
      {!desktop && (
        <nav id={navId} className="drawer" aria-label="Main" data-open={drawerOpen}>
          <div className="drawer__head">
            <BrandMark />
            <button
              ref={closeButton}
              type="button"
              className="icon-button"
              aria-label="Close menu"
              onClick={() => {
                closeDrawer(true);
              }}
            >
              <Icon name="close" />
            </button>
          </div>
          <ul className="drawer__list">
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end ?? false}
                  className="menu-link"
                  onClick={() => {
                    closeDrawer(false);
                  }}
                >
                  <Icon name={item.icon} />
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
          <div className="drawer__account">
            {terminalToggle}
            {who}
            <ThemeSwitch compact />
            {logOut}
          </div>
        </nav>
      )}
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
      <SiteFooter inert={drawerOpen} />
      {terminal && (
        <div inert={drawerOpen}>
          <TerminalDock services={terminal} mode={dockMode} onMode={setDockMode} />
        </div>
      )}
    </div>
  );
}

interface AccountMenuProps {
  who: ReactNode;
  initialLetter: string;
  username: string;
  logOut: ReactNode;
}

/** The account button of the desktop header and its panel: who is signed in, the profile, the theme, logging out. */
function AccountMenu({ who, initialLetter, username, logOut }: AccountMenuProps) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        button.current?.focus();
      }
    };
    const onPointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  return (
    <div className="account" ref={root}>
      <button
        ref={button}
        type="button"
        className="account__button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => {
          setOpen((value) => !value);
        }}
      >
        <span className="avatar" aria-hidden="true">
          {initialLetter}
        </span>
        <span className="account__name">
          <span className="visually-hidden">Account: </span>
          <Text value={username} />
        </span>
        <Icon name="chevronDown" />
      </button>
      {open && (
        <div id={panelId} className="popover">
          {who}
          <ul className="menu-list">
            <li>
              <NavLink
                to="/profile"
                className="menu-link"
                onClick={() => {
                  setOpen(false);
                }}
              >
                <Icon name="user" />
                Profile
              </NavLink>
            </li>
          </ul>
          <div className="stack">
            <ThemeSwitch compact />
            {logOut}
          </div>
        </div>
      )}
    </div>
  );
}
