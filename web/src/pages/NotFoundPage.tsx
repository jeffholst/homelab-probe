import { Link } from "react-router-dom";

import { usePageTitle } from "../lib/usePageTitle";

export function NotFoundPage() {
  usePageTitle("Page not found");
  return (
    <>
      <h1>Page not found</h1>
      <p>
        There is no page at this address. <Link to="/">Go to the home page</Link>.
      </p>
    </>
  );
}
