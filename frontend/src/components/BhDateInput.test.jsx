/** The custom date picker: popover, month browsing, min/max, clear, Today, dismissal and placement. */
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import BhDateInput from "./BhDateInput";

function Harness({ initial = "", ...props }) {
  const [value, setValue] = useState(initial);
  return (
    <form>
      <BhDateInput name="due" ariaLabel="Due date" value={value} onChange={(v) => { props.spy?.(v); setValue(v); }} {...props} />
      <output data-testid="out">{value}</output>
    </form>
  );
}

const out = () => screen.getByTestId("out").textContent;

describe("BhDateInput", () => {
  it("shows a placeholder, then the chosen date in human form with a hidden form value", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(screen.getByText("Select date")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear date" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Due date" }));
    expect(screen.getByRole("dialog", { name: "Pick a date" })).toBeInTheDocument();
    const day = document.querySelector('.bh-date-cell:not(.muted)[data-iso$="-15"]');
    await user.click(day);
    expect(out()).toBe(day.dataset.iso);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.querySelector("input[name=due]")).toHaveValue(day.dataset.iso);
    expect(screen.queryByText("Select date")).not.toBeInTheDocument();
  });

  it("opens on the value's month, browses across year boundaries and marks selection and today", async () => {
    const user = userEvent.setup();
    render(<Harness initial="2026-01-20" />);
    expect(screen.getByText("Jan 20, 2026")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Due date" }));
    expect(screen.getByText("January 2026")).toBeInTheDocument();
    expect(document.querySelector(".bh-date-cell.is-selected")).toHaveAttribute("data-iso", "2026-01-20");
    await user.click(screen.getByRole("button", { name: "Previous month" }));
    expect(screen.getByText("December 2025")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next month" }));
    await user.click(screen.getByRole("button", { name: "Next month" }));
    expect(screen.getByText("February 2026")).toBeInTheDocument();
    for (let i = 0; i < 11; i += 1) await user.click(screen.getByRole("button", { name: "Next month" }));
    expect(screen.getByText("January 2027")).toBeInTheDocument();
    // Bleed days of neighbouring months can be picked too.
    await user.click(document.querySelector(".bh-date-cell.muted"));
    expect(out()).not.toBe("2026-01-20");
  });

  it("Today picks today's date", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Due date" }));
    await user.click(screen.getByRole("button", { name: "Today" }));
    const d = new Date();
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    expect(out()).toBe(iso);
  });

  it("refuses days outside min and max", async () => {
    const user = userEvent.setup();
    const spy = vi.fn();
    render(<Harness initial="2026-03-10" min="2026-03-05" max="2026-03-20" spy={spy} />);
    await user.click(screen.getByRole("button", { name: "Due date" }));
    await user.click(document.querySelector('[data-iso="2026-03-02"]'));
    await user.click(document.querySelector('[data-iso="2026-03-25"]'));
    expect(spy).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.click(document.querySelector('[data-iso="2026-03-12"]'));
    expect(spy).toHaveBeenCalledWith("2026-03-12");
  });

  it("the x button clears the value without opening the picker", async () => {
    const user = userEvent.setup();
    render(<Harness initial="2026-03-10" />);
    await user.click(screen.getByRole("button", { name: "Clear date" }));
    expect(out()).toBe("");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("an invalid value falls back to the current month; disabled has no clear or picker", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Harness initial="2026-02-31" />);
    expect(document.querySelector(".bh-date-label")).toHaveTextContent("");
    await user.click(screen.getByRole("button", { name: "Due date" }));
    const now = new Date();
    const month = now.toLocaleString("en-US", { month: "long" });
    expect(screen.getByText(`${month} ${now.getFullYear()}`)).toBeInTheDocument();
    unmount();
    render(<Harness initial="2026-03-10" disabled />);
    expect(screen.queryByRole("button", { name: "Clear date" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Due date" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes on a second click on the trigger, on an outside click and on Escape (keeping focus on the trigger)", async () => {
    const user = userEvent.setup();
    render(<><Harness /><button type="button">elsewhere</button></>);
    const trigger = screen.getByRole("button", { name: "Due date" });
    await user.click(trigger);
    await user.click(trigger);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(trigger);
    await user.click(screen.getByText("elsewhere"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(trigger);
    await user.click(screen.getByText(/20\d\d/)); // the title inside the popover stays open
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("flips above the trigger when there is no room below and keeps inside the viewport width", async () => {
    const user = userEvent.setup();
    const real = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = function box() {
      if (this.classList?.contains("bh-date-btn")) {
        return { top: 700, bottom: 730, left: window.innerWidth - 20, right: window.innerWidth, width: 20, height: 30, x: 0, y: 700, toJSON() {} };
      }
      return real.call(this);
    };
    try {
      render(<Harness />);
      await user.click(screen.getByRole("button", { name: "Due date" }));
      const pop = screen.getByRole("dialog");
      expect(pop.style.position).toBe("fixed");
      expect(Number.parseFloat(pop.style.top)).toBeLessThan(700);
      fireEvent(window, new Event("resize"));
      expect(Number.parseFloat(pop.style.left)).toBeGreaterThanOrEqual(8);
    } finally {
      Element.prototype.getBoundingClientRect = real;
    }
  });
});
