/** Video lightbox: focus handling, scroll lock, Escape/backdrop close and the Tab trap. */
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import VideoLightbox from "./VideoLightbox";

function Host({ onClose = () => {} }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>View</button>
      {open && <VideoLightbox src="/v.mp4" type="video/mp4" label="Demo" onClose={() => { onClose(); setOpen(false); }} />}
    </div>
  );
}

const withLayout = (els) => els.forEach((el) => Object.defineProperty(el, "offsetParent", { configurable: true, get: () => document.body }));
const controls = () => [...document.querySelectorAll(".video-lightbox button, .video-lightbox input")];
const tab = (shiftKey = false) => {
  const e = new KeyboardEvent("keydown", { key: "Tab", shiftKey, bubbles: true, cancelable: true });
  document.dispatchEvent(e);
  return e;
};

describe("VideoLightbox", () => {
  it("opens as a labelled modal, focuses Close, locks page scroll and restores both when closed", async () => {
    const user = userEvent.setup();
    document.body.style.overflow = "auto";
    render(<Host />);
    await user.click(screen.getByRole("button", { name: "View" }));
    expect(screen.getByRole("dialog", { name: "Video: Demo" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Close video" })).toHaveFocus();
    expect(document.body.style.overflow).toBe("hidden");
    expect(document.querySelector(".video-lightbox-title")).toHaveTextContent("Demo");
    await user.click(screen.getByRole("button", { name: "Close video" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe("auto");
    expect(screen.getByRole("button", { name: "View" })).toHaveFocus();
  });

  it("the opening click does not close it; the player does not; the backdrop and Escape do", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Host onClose={onClose} />);
    await user.click(screen.getByRole("button", { name: "View" }));
    expect(onClose).not.toHaveBeenCalled();
    await new Promise((r) => setTimeout(r, 5));
    await user.click(document.querySelector(".video-lightbox-stage"));
    expect(onClose).not.toHaveBeenCalled();
    await user.click(document.querySelector(".video-lightbox"));
    expect(onClose).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "View" }));
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("without a label it is just Video and has no title line", () => {
    render(<VideoLightbox src="/v.mp4" onClose={() => {}} />);
    expect(screen.getByRole("dialog", { name: "Video" })).toBeInTheDocument();
    expect(document.querySelector(".video-lightbox-title")).toBeNull();
  });

  it("Tab wraps from the last control to the first and Shift+Tab the other way", () => {
    render(<VideoLightbox src="/v.mp4" label="Demo" onClose={() => {}} />);
    const all = controls();
    withLayout(all);
    const first = screen.getByRole("button", { name: "Close video" });
    const last = all.at(-1);
    last.focus();
    expect(tab().defaultPrevented).toBe(true);
    expect(first).toHaveFocus();
    expect(tab(true).defaultPrevented).toBe(true);
    expect(last).toHaveFocus();
  });

  it("Tab from outside the dialog is pulled back in; Tab in the middle is left alone", () => {
    render(<><button type="button">page</button><VideoLightbox src="/v.mp4" label="Demo" onClose={() => {}} /></>);
    const all = controls();
    withLayout(all);
    screen.getByRole("button", { name: "page" }).focus();
    expect(tab().defaultPrevented).toBe(true);
    expect(all[0]).toHaveFocus();
    all[1].focus();
    expect(tab().defaultPrevented).toBe(false);
  });

  it("with nothing focusable Tab is swallowed", () => {
    render(<VideoLightbox src="/v.mp4" onClose={() => {}} />);
    controls().forEach((el) => { el.disabled = true; });
    document.querySelectorAll(".video-lightbox [tabindex]").forEach((el) => el.setAttribute("tabindex", "-1"));
    document.activeElement?.blur?.();
    expect(tab().defaultPrevented).toBe(true);
  });
});
