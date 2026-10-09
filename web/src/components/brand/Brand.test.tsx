import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { BrandHero, BrandMark } from "./Brand";

it("keeps the original mascot and mixed-case text outside the text-only locations", () => {
  const { container } = render(<BrandMark />);
  expect(container.querySelector(".brand")?.textContent).toBe("Homelab Probe");
  expect(container.querySelector(".brand__ant")).toHaveAttribute("src", expect.stringContaining("ant.webp"));
});

it("keeps the login and setup hero artwork", () => {
  render(<BrandHero />);
  expect(screen.getByRole("img", { name: "Homelab Probe" })).toHaveAttribute("src", expect.stringContaining("logo.webp"));
});
