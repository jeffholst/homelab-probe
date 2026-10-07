import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../../test/render";
import { CodeBlock } from "./CodeBlock";

describe("a code block", () => {
  it("shows the text as it is, never as markup", () => {
    renderWithProviders(<CodeBlock title=".env" text={"A=<b>x</b>\nB=2\n"} fileName="hlp.env" />);
    expect(screen.getByRole("figure", { name: ".env" })).toHaveTextContent("A=<b>x</b>");
    expect(document.querySelector("pre b")).toBeNull();
  });

  it("copies the text and says so", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    renderWithProviders(<CodeBlock title=".env" text="A=1" fileName="hlp.env" />);
    await user.click(screen.getByRole("button", { name: "Copy .env" }));
    expect(writeText).toHaveBeenCalledWith("A=1");
    expect(await screen.findByText(".env copied.")).toBeInTheDocument();
  });

  it("says to select the text where the clipboard is not available", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    renderWithProviders(<CodeBlock title=".env" text="A=1" fileName="hlp.env" />);
    await user.click(screen.getByRole("button", { name: "Copy .env" }));
    expect(await screen.findAllByText("Copying is not available here: select the text and copy it.")).toHaveLength(2);
  });

  it("downloads the text as a file of its own name", async () => {
    const user = userEvent.setup();
    const created: Blob[] = [];
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: (blob: Blob) => (created.push(blob), "blob:test"), revokeObjectURL: vi.fn() }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.download).toBe("hlp.env");
      expect(this.href).toBe("blob:test");
    });
    renderWithProviders(<CodeBlock title=".env" text="A=1" fileName="hlp.env" />);
    await user.click(screen.getByRole("button", { name: "Download .env" }));
    expect(click).toHaveBeenCalledOnce();
    expect(await created[0]?.text()).toBe("A=1");
    vi.unstubAllGlobals();
  });
});
