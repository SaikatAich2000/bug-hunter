/** Modal focus management: initial focus, Tab trap and focus return. */
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Modal from "./Modal";

beforeEach(() => {
  // jsdom has no layout; report every element as rendered unless it is hidden.
  vi.spyOn(Element.prototype, "getClientRects").mockImplementation(function rects() {
    return this.closest("[hidden]") ? [] : [{}];
  });
});
afterEach(() => vi.restoreAllMocks());

function Harness({ autofocus = false, bare = false }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>Open</button>
      <Modal id="m" open={open} title="Edit" onClose={() => setOpen(false)}>
        {!bare && <input aria-label="first" data-autofocus={autofocus ? "" : undefined} />}
        {!bare && <button type="button">last</button>}
      </Modal>
    </div>
  );
}

const dialog = () => screen.getByRole("dialog", { name: "Edit" });

describe("focus", () => {
  it("moves focus to the data-autofocus control, or to the card itself, and gives it back on close", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Harness autofocus />);
    const opener = screen.getByRole("button", { name: "Open" });
    await user.click(opener);
    await vi.waitFor(() => expect(screen.getByLabelText("first")).toHaveFocus());
    await user.click(screen.getByRole("button", { name: "Close" }));
    await vi.waitFor(() => expect(opener).toHaveFocus());
    unmount();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open" }));
    await vi.waitFor(() => expect(dialog()).toHaveFocus());
  });

  it("Tab and Shift+Tab wrap inside the dialog", async () => {
    const user = userEvent.setup();
    render(<Harness autofocus />);
    await user.click(screen.getByRole("button", { name: "Open" }));
    const first = screen.getByRole("button", { name: "Close" });
    const last = screen.getByRole("button", { name: "last" });
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();
    dialog().focus();
    fireEvent.keyDown(dialog(), { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();
    const mid = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    screen.getByLabelText("first").focus();
    screen.getByLabelText("first").dispatchEvent(mid);
    expect(mid.defaultPrevented).toBe(false);
    const other = new KeyboardEvent("keydown", { key: "a", bubbles: true, cancelable: true });
    dialog().dispatchEvent(other);
    expect(other.defaultPrevented).toBe(false);
  });

  it("with nothing focusable inside Tab stays put", async () => {
    const user = userEvent.setup();
    render(<Harness bare />);
    await user.click(screen.getByRole("button", { name: "Open" }));
    screen.getByRole("button", { name: "Close" }).disabled = true;
    const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    dialog().dispatchEvent(tab);
    expect(tab.defaultPrevented).toBe(true);
  });
});
