import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/errors";
import { Banner, Empty, ErrorState, Loading, Refreshing, StatusBanner } from "./DataStates";
import { DataView, type QueryLike } from "./DataView";

function query(overrides: Partial<QueryLike<string[]>> = {}): QueryLike<string[]> {
  return {
    data: ["alpha"],
    error: null,
    isPending: false,
    isFetching: false,
    dataUpdatedAt: Date.UTC(2026, 0, 2, 3, 4, 5),
    refetch: vi.fn(),
    ...overrides,
  };
}

const list = (items: string[]) => <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul>;

function view(q: QueryLike<string[]>, warnings: string[] = []) {
  return render(
    <DataView
      query={q}
      label="things"
      isEmpty={(items) => items.length === 0}
      empty={{ title: "No things", text: "Nothing to show yet." }}
      warnings={warnings}
    >
      {list}
    </DataView>,
  );
}

describe("DataView", () => {
  it("shows Loading while there is no data and the first read is on its way", () => {
    view(query({ data: undefined, isPending: true, isFetching: true }));
    expect(screen.getByRole("status")).toHaveTextContent("Loading things");
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("shows an Error with the server's sentence and its code, and retries on request", async () => {
    const refetch = vi.fn();
    view(query({ data: undefined, error: new ApiError(502, "controller_unreachable", "The controller could not be reached."), refetch }));
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not load things");
    expect(alert).toHaveTextContent("The controller could not be reached.");
    expect(alert).toHaveTextContent("controller_unreachable");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("shows Empty for an empty list, with its explanation", () => {
    view(query({ data: [] }));
    expect(screen.getByText("No things")).toBeInTheDocument();
    expect(screen.getByText("Nothing to show yet.")).toBeInTheDocument();
  });

  it("keeps the old data on screen, with a Refreshing line, while it is read again", () => {
    view(query({ isFetching: true }));
    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Refreshing things");
  });

  it("keeps the old data under a stale banner when the refresh failed, and says what failed and from when the data is", async () => {
    const refetch = vi.fn();
    view(query({ error: new ApiError(504, "controller_timeout", "The controller did not answer in time."), refetch }));
    expect(screen.getByText("alpha")).toBeInTheDocument();
    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("Showing older things");
    expect(banner).toHaveTextContent("The controller did not answer in time.");
    expect(banner).toHaveTextContent("2026");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(refetch).toHaveBeenCalled();
  });

  it("keeps the data under a partial banner that lists what could not be read, as text", () => {
    view(query(), ["Wi-Fi networks could not be read", "<b>bold</b>"]);
    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Some things could not be read");
    expect(screen.getByText("Wi-Fi networks could not be read")).toBeInTheDocument();
    expect(screen.getByText("<b>bold</b>")).toBeInTheDocument(); // text, not markup
    expect(document.querySelector("b")).toBeNull();
  });

  it("still shows a stale list that is empty as empty, under the stale banner", () => {
    view(query({ data: [], error: new ApiError(0, "network_error", "The server could not be reached.") }));
    expect(screen.getByText("No things")).toBeInTheDocument();
    expect(screen.getByText("Showing older things")).toBeInTheDocument();
  });

  it("uses a generic title for Empty when the page gives none", () => {
    render(
      <DataView query={query({ data: [] })} label="things" isEmpty={(items) => items.length === 0}>
        {list}
      </DataView>,
    );
    expect(screen.getByText("No things")).toBeInTheDocument();
  });
});

describe("the pieces", () => {
  it("says more than colour: every banner has a title", () => {
    render(
      <>
        <Banner tone="danger" title="Danger title" role="alert" />
        <Banner tone="info" title="Info title" />
      </>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Danger title");
    expect(screen.getByText("Info title")).toBeInTheDocument();
  });

  it("renders Loading, Refreshing and Empty with accessible roles", () => {
    render(
      <>
        <Loading label="x" />
        <Refreshing label="y" />
        <Empty title="Nothing" action={<button type="button">Do</button>} />
      </>,
    );
    expect(screen.getAllByRole("status")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Do" })).toBeInTheDocument();
  });

  it("falls back to a general sentence for an error that is not an ApiError, and never shows its text", () => {
    render(<ErrorState error={new Error("secret path /etc/passwd")} label="x" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong.");
    expect(screen.getByRole("alert")).not.toHaveTextContent("passwd");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders a stale banner with no time and a non-API error", () => {
    render(<StatusBanner kind="stale" label="x" error={new Error("boom")} />);
    expect(screen.getByRole("status")).toHaveTextContent("The latest refresh failed.");
  });
});
