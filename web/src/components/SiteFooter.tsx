import { useMeta } from "../app/meta";
import { BrandMark } from "./brand/Brand";
import { Icon, type IconName } from "./ui/Icon";

const REPOSITORY = "https://github.com/jeffholst/homelab-probe";

/** The project's public pages; nothing here is read from the server or the controller. */
export const FOOTER_LINKS: readonly { href: string; label: string; icon: IconName }[] = [
  { href: `${REPOSITORY}/tree/main/docs`, label: "Documentation", icon: "book" },
  { href: REPOSITORY, label: "Source code", icon: "code" },
  { href: `${REPOSITORY}/blob/main/LICENSE`, label: "Apache-2.0 license", icon: "file" },
];

/** The footer of every page: what the app is, its links, its version and the promise that it changes nothing. */
export function SiteFooter({ inert = false }: { inert?: boolean }) {
  const meta = useMeta();
  return (
    <footer className="site-footer" inert={inert}>
      <div className="site-footer__inner">
        <div className="site-footer__about">
          <BrandMark textOnly />
          <p>Read-only insight into your UniFi network: health checks, inventory and history, from your own machine.</p>
        </div>
        <nav aria-label="Project">
          <ul className="site-footer__links">
            {FOOTER_LINKS.map((link) => (
              <li key={link.href}>
                <a href={link.href} target="_blank" rel="noopener noreferrer">
                  <Icon name={link.icon} />
                  {link.label}
                  <span className="visually-hidden"> (opens in a new tab)</span>
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <div className="site-footer__bottom">
          <p>
            Homelab Probe{meta.data ? ` ${meta.data.version}` : ""}. Read-only: nothing here changes your controller.
          </p>
        </div>
      </div>
    </footer>
  );
}
