/** Work-item modal: oversized pastes, unsent attachments blocking Save, file-only comments. */
import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createBackend, makeBug, mountApp, recordToasts } from "../test/backend";

const modal = () => within(document.getElementById("modalBug"));
const toastText = () => document.getElementById("toast").textContent;
const file = (name, size = 5) => new File(["x".repeat(size)], name, { type: "image/png" });
const huge = (name) => {
  const f = file(name);
  Object.defineProperty(f, "size", { value: 60 * 1024 * 1024 });
  return f;
};
const pasteOf = (f) => ({ clipboardData: { items: [{ kind: "file", getAsFile: () => f }] } });

async function openBug(user, backend = createBackend(), id = 101) {
  await mountApp({ backend });
  await user.click(document.querySelector(`tr[data-bug-id="${id}"]`));
  await waitFor(() => expect(document.getElementById("modalBugTitle")).toHaveTextContent(`#${id}`));
  return backend;
}

const withComment = () => {
  const backend = createBackend();
  backend.state.bugs[0] = makeBug({
    id: 101,
    comments: [{ id: 11, bug_id: 101, author_user_id: 1, author_name: "Ada Admin", body: "<p>First</p>", created_at: "2026-01-05T10:00:00Z", attachments: [] }],
  });
  return backend;
};

describe("pasting files", () => {
  it("an oversized image pasted into the description of an existing item is refused with the size limit", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user);
    fireEvent.paste(modal().getByRole("textbox", { name: "Description" }), pasteOf(huge("big.png")));
    await waitFor(() => expect(toastText()).toMatch(/"big.png" is too large .* 50 MB/));
    expect(backend.called("POST", /attachments$/)).toHaveLength(0);
  });

  it("an image pasted into a comment being edited attaches to the item; an oversized one is refused", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, withComment());
    await user.click(document.querySelector('[data-act="edit-comment"][data-id="11"]'));
    const editor = await screen.findByRole("textbox", { name: "Edit comment" });
    const toasts = recordToasts();
    fireEvent.paste(editor, pasteOf(huge("big.png")));
    await waitFor(() => expect(toasts.some((t) => /too large/.test(t))).toBe(true));
    expect(backend.called("POST", /attachments$/)).toHaveLength(0);
    fireEvent.paste(editor, pasteOf(file("ok.png")));
    await waitFor(() => expect(backend.called("POST", "/bugs/101/attachments")).toHaveLength(1));
    await waitFor(() => expect(toasts).toContain("Attached: ok.png"));
  });
});

describe("saving with unsent files", () => {
  it("staged attachments that were never uploaded block Save and say how many", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user);
    await user.upload(document.getElementById("bugAttachAddInput"), [file("one.png"), file("two.png")]);
    await user.click(document.getElementById("bugSubmitBtn"));
    expect(toastText()).toMatch(/2 attachments that haven't been uploaded yet/);
    await user.click(document.querySelector(".attach-staged-cancel"));
    await user.upload(document.getElementById("bugAttachAddInput"), file("one.png"));
    await user.click(document.getElementById("bugSubmitBtn"));
    expect(toastText()).toMatch(/1 attachment that hasn't been uploaded yet/);
    expect(backend.called("PUT", "/bugs/101")).toHaveLength(0);
  });

  it("files staged on an unposted comment block Save too", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user);
    await user.upload(document.getElementById("commentFiles"), [file("a.png"), file("b.png")]);
    await user.click(document.getElementById("bugSubmitBtn"));
    expect(toastText()).toMatch(/2 files attached to a comment you haven't posted yet/);
    expect(backend.called("PUT", "/bugs/101")).toHaveLength(0);
  });
});

describe("comments", () => {
  it("files without any comment text are refused with advice, and nothing is posted", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user);
    await user.upload(document.getElementById("commentFiles"), [file("a.png")]);
    await user.click(document.getElementById("commentPostBtn"));
    expect(toastText()).toMatch(/Add some comment text/);
    expect(backend.called("POST", /comments$|attachments$/)).toHaveLength(0);
  });
});
