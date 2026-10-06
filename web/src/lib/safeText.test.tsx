import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Text } from "../components/Text";
import vectors from "./printable-vectors.json";
import { safeText } from "./safeText";

describe("safeText", () => {
  it.each(vectors)("gives what the Python printable gives: $name", ({ input, limit, expected }) => {
    expect(safeText(input, limit)).toBe(expected);
  });

  it("removes a right-to-left override that would reverse the rest of the name", () => {
    expect(safeText("gpj.\u202eexe.txt")).toBe("gpj.exe.txt");
  });

  it("removes every embedding, override and isolate, and the zero-width characters that hide a difference", () => {
    for (const hidden of ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069", "\u200b", "\u2060", "\ufeff"]) {
      expect(safeText(`a${hidden}b`)).toBe("ab");
    }
  });

  it("keeps what real names need: joiners, direction marks and right-to-left letters", () => {
    expect(safeText("a\u200db")).toBe("a\u200db");
    expect(safeText("a\u200cb")).toBe("a\u200cb");
    expect(safeText("a\u200eb\u200fc")).toBe("a\u200eb\u200fc");
    expect(safeText("\u05e9\u05dc\u05d5\u05dd")).toBe("\u05e9\u05dc\u05d5\u05dd");
  });

  it("removes control characters, including the escape that starts a terminal sequence", () => {
    expect(safeText("a\u0000b\u0007c\u001bd\u007fe\u0080f\u009fg")).toBe("abcdefg");
  });

  it("makes one line out of several", () => {
    expect(safeText("first\nsecond\r\nthird\tfourth\u2028fifth")).toBe("first second third fourth fifth");
  });

  it("is not fooled by two names that differ only by an invisible character", () => {
    expect(safeText("admin")).toBe(safeText("ad\u200bmin"));
  });

  it("shows only what is text: numbers and booleans yes, null, undefined and objects as nothing", () => {
    expect(safeText(42)).toBe("42");
    expect(safeText(false)).toBe("false");
    expect(safeText(null)).toBe("");
    expect(safeText(undefined)).toBe("");
    expect(safeText({ toString: () => "<b>x</b>" })).toBe("");
    expect(safeText(["a"])).toBe("");
  });

  it("cuts by characters, not by UTF-16 units, so an emoji is never split", () => {
    expect(safeText("\u{1F600}\u{1F601}\u{1F602}\u{1F603}", 3)).toBe("\u{1F600}\u{1F601}\u2026");
  });
});

describe("<Text>", () => {
  it("shows markup as text, never as elements", () => {
    const { container } = render(<Text value={'<img src=x onerror="alert(1)"><script>alert(2)</script>'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container).toHaveTextContent('<img src=x onerror="alert(1)"><script>alert(2)</script>');
  });

  it("cleans the value, isolates its direction and offers a fallback for nothing", () => {
    render(
      <>
        <Text value={"gpj.\u202eexe"} />
        <Text value={"\u200b"} fallback="(no name)" />
      </>,
    );
    expect(screen.getByText("gpj.exe")).toHaveAttribute("dir", "auto");
    expect(screen.getByText("(no name)")).toBeInTheDocument();
  });
});
