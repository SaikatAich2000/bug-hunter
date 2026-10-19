/** RichEditor behaviour in a real DOM: formatting, lists, blocks, history, file routing. */
import React, { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import RichEditor from "./RichEditor";

function setup(props = {}) {
  const ref = createRef();
  const utils = render(
    <>
      <div id="toast" hidden />
      <RichEditor ref={ref} ariaLabel="Body" placeholder="Write" name="body" textareaId="body" {...props} />
    </>,
  );
  const editor = screen.getByRole("textbox", { name: "Body" });
  const toolbar = screen.getByRole("toolbar", { hidden: true });
  return { ref, editor, toolbar, native: document.getElementById("body"), ...utils };
}

/** Select [start,end) of the first text node under `el` (collapsed when start === end). */
function select(el, start, end = start) {
  const text = el.firstChild;
  const range = document.createRange();
  range.setStart(text, start);
  range.setEnd(text, end);
  const s = window.getSelection();
  s.removeAllRanges();
  s.addRange(range);
}

const tool = (name) => screen.getByRole("button", { name, hidden: true });
const toastText = () => document.getElementById("toast").textContent;

describe("RichEditor content API", () => {
  it("seeds from initialHtml through the sanitizer and exposes it with getHtml", () => {
    const { ref } = setup({ initialHtml: "<p>Hello</p><script>alert(1)</script>" });
    expect(ref.current.getHtml()).toBe("<p>Hello</p>");
  });

  it("setHtml replaces content, mirrors it into the native textarea and reports onChange", () => {
    const onChange = vi.fn();
    const { ref, native, editor } = setup({ onChange });
    act(() => ref.current.setHtml("<b>Hi</b><img src=x onerror=alert(1)>"));
    expect(editor.innerHTML).toContain("<b>Hi</b>");
    expect(editor.innerHTML).not.toContain("onerror");
    expect(native.value).toBe(editor.innerHTML.replaceAll("​", ""));
    expect(onChange).toHaveBeenLastCalledWith(editor.innerHTML);
  });

  it("treats scaffolding-only content as empty and focus() focuses the surface", () => {
    const { ref, editor } = setup({ initialHtml: "<div><br></div>" });
    expect(ref.current.getHtml()).toBe("");
    act(() => ref.current.focus());
    expect(editor).toHaveFocus();
  });

  it("typing updates onChange and the native textarea", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { editor, native } = setup({ onChange });
    await user.click(editor);
    await user.type(editor, "abc");
    expect(editor).toHaveTextContent("abc");
    expect(onChange).toHaveBeenLastCalledWith(expect.stringContaining("abc"));
    expect(native.value).toContain("abc");
  });

  it("writes made to the native textarea (automation) are mirrored into the surface", () => {
    const { editor, native, ref } = setup();
    fireEvent.input(native, { target: { value: "<i>from outside</i><script>x</script>" } });
    expect(editor.innerHTML).toBe("<i>from outside</i>");
    expect(ref.current.getHtml()).toBe("<i>from outside</i>");
  });

  it("disabled editors are not editable and their toolbar buttons are disabled", () => {
    const { editor } = setup({ disabled: true });
    expect(editor).toHaveAttribute("contenteditable", "false");
    expect(tool("Bold")).toBeDisabled();
    expect(editor).toHaveAttribute("aria-disabled", "true");
  });

  it("formatting=false hides the toolbar and ignores Ctrl+B", () => {
    const { editor, toolbar } = setup({ formatting: false, initialHtml: "plain" });
    expect(toolbar).toHaveAttribute("hidden");
    select(editor, 0, 5);
    fireEvent.keyDown(editor, { key: "b", ctrlKey: true });
    expect(editor.innerHTML).toBe("plain");
  });
});

describe("inline formatting", () => {
  it("bold wraps the selection and a second click unwraps it", async () => {
    const user = userEvent.setup();
    const { editor, ref } = setup({ initialHtml: "hello world" });
    select(editor, 0, 5);
    await user.click(tool("Bold"));
    expect(editor.innerHTML).toBe("<b>hello</b> world");
    expect(tool("Bold")).toHaveClass("is-active");
    await user.click(tool("Bold"));
    expect(editor.innerHTML).toBe("hello world");
    expect(ref.current.getHtml()).toBe("hello world");
  });

  it("with a bare caret inside a word, bold formats that whole word", async () => {
    const user = userEvent.setup();
    const { editor } = setup({ initialHtml: "hello world" });
    select(editor, 8);
    await user.click(tool("Italic"));
    expect(editor.innerHTML).toBe("hello <i>world</i>");
  });

  it("with a bare caret between non-word characters it arms a styled placeholder and toggles back out", async () => {
    const user = userEvent.setup();
    const { editor } = setup({ initialHtml: "a  b" });
    select(editor, 2);
    await user.click(tool("Underline"));
    expect(editor.querySelector("u")).not.toBeNull();
    // caret is now inside <u>; toggling again exits the wrapper
    await user.click(tool("Underline"));
    expect(editor.querySelectorAll("u")).toHaveLength(1);
    expect(editor.textContent.replaceAll("​", "")).toBe("a  b");
  });

  it("Ctrl+B / Ctrl+I / Ctrl+U apply the matching formats", () => {
    const { editor } = setup({ initialHtml: "word" });
    select(editor, 0, 4);
    fireEvent.keyDown(editor, { key: "b", ctrlKey: true });
    expect(editor.innerHTML).toBe("<b>word</b>");
    fireEvent.keyDown(editor, { key: "i", ctrlKey: true });
    expect(editor.innerHTML).toContain("<i>");
    fireEvent.keyDown(editor, { key: "u", ctrlKey: true });
    expect(editor.innerHTML).toContain("<u>");
  });

  it("strikethrough works from the keyboard path of the toolbar (Enter on the button)", async () => {
    const user = userEvent.setup();
    const { editor } = setup({ initialHtml: "gone" });
    select(editor, 0, 4);
    tool("Strikethrough").focus();
    await user.keyboard("{Enter}");
    expect(editor.innerHTML).toBe("<s>gone</s>");
  });
});

describe("lists and blocks", () => {
  it("bullet list wraps the caret's block, then lifts the item back out", async () => {
    const user = userEvent.setup();
    const { editor } = setup({ initialHtml: "<p>one</p>" });
    select(editor.firstChild, 1);
    await user.click(tool("Bulleted list"));
    expect(editor.innerHTML).toBe("<ul><li>one</li></ul>");
    select(editor.querySelector("li"), 1);
    await user.click(tool("Bulleted list"));
    expect(editor.innerHTML).toBe("<p>one</p>");
  });

  it("numbered list over loose text wraps everything; lifting a middle item splits the list", async () => {
    const user = userEvent.setup();
    const { editor } = setup({ initialHtml: "loose" });
    select(editor, 2);
    await user.click(tool("Numbered list"));
    expect(editor.innerHTML).toBe("<ol><li>loose</li></ol>");
    // three items; lift the middle one
    editor.innerHTML = "<ol><li>a</li><li>b</li><li>c</li></ol>";
    select(editor.querySelectorAll("li")[1], 0);
    await user.click(tool("Numbered list"));
    expect(editor.innerHTML).toBe("<ol><li>a</li></ol><p>b</p><ol><li>c</li></ol>");
  });

  it("a non-collapsed selection without a block is wrapped into a list item", async () => {
    const user = userEvent.setup();
    const { editor } = setup({ initialHtml: "alpha beta" });
    select(editor, 0, 5);
    await user.click(tool("Bulleted list"));
    expect(editor.querySelector("ul > li")).toHaveTextContent("alpha");
  });

  it("quote wraps then unwraps its block; code block replaces a quote", async () => {
    const user = userEvent.setup();
    const { editor } = setup({ initialHtml: "<p>quoted</p>" });
    select(editor.firstChild, 2);
    await user.click(tool("Quote"));
    expect(editor.innerHTML).toBe("<blockquote>quoted</blockquote>");
    select(editor.firstChild, 2);
    await user.click(tool("Quote"));
    expect(editor.innerHTML).toBe("<p>quoted</p>");
    select(editor.firstChild, 2);
    await user.click(tool("Code block"));
    expect(editor.innerHTML).toBe("<pre>quoted</pre>");
  });

  it("quote with no surrounding block wraps all editor content", async () => {
    const user = userEvent.setup();
    const { editor } = setup({ initialHtml: "bare text" });
    select(editor, 1);
    await user.click(tool("Quote"));
    expect(editor.innerHTML).toBe("<blockquote>bare text</blockquote>");
  });
});

describe("history and shortcuts", () => {
  it("Ctrl+Z undoes a toolbar change and Ctrl+Shift+Z / Ctrl+Y redo it", async () => {
    const user = userEvent.setup();
    const { editor } = setup({ initialHtml: "hello world" });
    select(editor, 0, 5);
    await user.click(tool("Bold"));
    expect(editor.innerHTML).toBe("<b>hello</b> world");
    fireEvent.keyDown(editor, { key: "z", ctrlKey: true });
    expect(editor.innerHTML).toBe("hello world");
    fireEvent.keyDown(editor, { key: "z", ctrlKey: true, shiftKey: true });
    expect(editor.innerHTML).toBe("<b>hello</b> world");
    fireEvent.keyDown(editor, { key: "z", ctrlKey: true });
    fireEvent.keyDown(editor, { key: "y", ctrlKey: true });
    expect(editor.innerHTML).toBe("<b>hello</b> world");
    // nothing further to redo / undo past the start is a no-op
    fireEvent.keyDown(editor, { key: "y", ctrlKey: true });
    fireEvent.keyDown(editor, { key: "z", ctrlKey: true });
    fireEvent.keyDown(editor, { key: "z", ctrlKey: true });
    fireEvent.keyDown(editor, { key: "z", ctrlKey: true });
    expect(editor.innerHTML).toBe("hello world");
  });

  it("Ctrl+Enter is forwarded to onCtrlEnter", () => {
    const onCtrlEnter = vi.fn();
    const { editor } = setup({ onCtrlEnter });
    fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true });
    expect(onCtrlEnter).toHaveBeenCalledTimes(1);
  });

  it("plain keys and Alt combinations are left alone", () => {
    const { editor } = setup({ initialHtml: "x" });
    select(editor, 0, 1);
    fireEvent.keyDown(editor, { key: "b" });
    fireEvent.keyDown(editor, { key: "b", ctrlKey: true, altKey: true });
    fireEvent.keyDown(editor, { key: "b", ctrlKey: true, shiftKey: true });
    expect(editor.innerHTML).toBe("x");
  });

  it("typing is snapshotted after a pause so undo steps back through edits", () => {
    vi.useFakeTimers();
    try {
      const { editor } = setup({ initialHtml: "a" });
      editor.innerHTML = "ab";
      fireEvent.input(editor);
      act(() => { vi.advanceTimersByTime(700); });
      editor.innerHTML = "abc";
      fireEvent.input(editor);
      fireEvent.keyDown(editor, { key: "z", ctrlKey: true });
      expect(editor.innerHTML).toBe("ab");
    } finally {
      vi.useRealTimers();
    }
  });

  it("toolbar keyboard: Enter on the toolbar itself moves focus to its first button", async () => {
    const user = userEvent.setup();
    const { toolbar } = setup();
    toolbar.focus();
    await user.keyboard("{Enter}");
    expect(tool("Bold")).toHaveFocus();
  });

  it("mouse clicks on the toolbar do not double-run the command", async () => {
    const { editor } = setup({ initialHtml: "twice" });
    select(editor, 0, 5);
    fireEvent.click(tool("Bold"), { detail: 1 });
    expect(editor.innerHTML).toBe("twice");
  });
});

describe("files", () => {
  const fileItem = (file) => ({ kind: "file", getAsFile: () => file });

  it("pasted files go to onPasteFile instead of the document", () => {
    const onPasteFile = vi.fn();
    const { editor } = setup({ onPasteFile });
    const file = new File(["x"], "shot.png", { type: "image/png" });
    fireEvent.paste(editor, { clipboardData: { items: [fileItem(file)] } });
    expect(onPasteFile).toHaveBeenCalledWith(file);
  });

  it("text-only pastes are left to the browser", () => {
    const onPasteFile = vi.fn();
    const { editor } = setup({ onPasteFile });
    fireEvent.paste(editor, { clipboardData: { items: [{ kind: "string" }] } });
    fireEvent.paste(editor, {});
    expect(onPasteFile).not.toHaveBeenCalled();
  });

  it("blocks executables, oversized files and reports handler failures", async () => {
    const onPasteFile = vi.fn()
      .mockImplementationOnce(() => { throw new Error("sync boom"); })
      .mockImplementationOnce(() => Promise.reject(new Error("async boom")));
    const { editor } = setup({ onPasteFile });
    const paste = (file) => fireEvent.paste(editor, { clipboardData: { items: [fileItem(file)] } });

    paste(new File(["x"], "virus.exe", { type: "" }));
    expect(toastText()).toMatch(/can't be attached/);
    paste(new File(["x"], "weird", { type: "application/x-msdownload; charset=x" }));
    expect(toastText()).toMatch(/can't be attached/);

    const big = new File(["x"], "huge.bin", { type: "application/octet-stream" });
    Object.defineProperty(big, "size", { value: 60 * 1024 * 1024 });
    paste(big);
    expect(toastText()).toMatch(/too large/);
    expect(onPasteFile).not.toHaveBeenCalled();

    paste(new File(["x"], "a.png", { type: "image/png" }));
    expect(toastText()).toBe(`Couldn't attach "a.png": sync boom`);
    paste(new File(["x"], "b.png", { type: "image/png" }));
    await vi.waitFor(() => expect(toastText()).toBe(`Couldn't attach "b.png": async boom`));
  });

  it("with no handler a dropped file explains there is nowhere to attach it", () => {
    const { editor } = setup();
    fireEvent.drop(editor, { dataTransfer: { files: [new File(["x"], "n.png", { type: "image/png" })], types: ["Files"] } });
    expect(toastText()).toBe(`There's nowhere to attach "n.png" here.`);
  });

  it("drag-over with files shows the copy cursor; non-file drags and drops are ignored; disabled ignores all", () => {
    const onPasteFile = vi.fn();
    const { editor, rerender, ref } = setup({ onPasteFile });
    const dt = { types: ["Files"], dropEffect: "none" };
    fireEvent.dragOver(editor, { dataTransfer: dt });
    expect(dt.dropEffect).toBe("copy");
    const text = { types: ["text/plain"], dropEffect: "none" };
    fireEvent.dragOver(editor, { dataTransfer: text });
    expect(text.dropEffect).toBe("none");
    fireEvent.drop(editor, { dataTransfer: { files: [], types: [] } });
    expect(onPasteFile).not.toHaveBeenCalled();
    rerender(
      <>
        <div id="toast" hidden />
        <RichEditor ref={ref} ariaLabel="Body" disabled onPasteFile={onPasteFile} />
      </>,
    );
    const ed = screen.getByRole("textbox", { name: "Body" });
    fireEvent.dragOver(ed, { dataTransfer: dt });
    fireEvent.drop(ed, { dataTransfer: { files: [new File(["x"], "z.png")], types: ["Files"] } });
    expect(onPasteFile).not.toHaveBeenCalled();
  });

  it("the image button opens a file picker and routes the chosen file to the handler", () => {
    const onPasteFile = vi.fn();
    setup({ onPasteFile });
    fireEvent.mouseDown(tool("Insert image"));
    const input = document.body.querySelector('input[type="file"]');
    expect(input.accept).toBe("image/*,application/pdf");
    const file = new File(["x"], "pic.png", { type: "image/png" });
    Object.defineProperty(input, "files", { value: [file] });
    fireEvent.change(input);
    expect(onPasteFile).toHaveBeenCalledWith(file);
    expect(document.body.querySelector('input[type="file"]')).toBeNull();
  });

  it("the image picker rejects unsafe files and surfaces handler errors", async () => {
    const onPasteFile = vi.fn(() => Promise.reject(new Error("nope")));
    setup({ onPasteFile });
    const choose = (file) => {
      fireEvent.mouseDown(tool("Insert image"));
      const input = document.body.querySelector('input[type="file"]');
      Object.defineProperty(input, "files", { value: file ? [file] : [] });
      fireEvent.change(input);
    };
    choose(new File(["x"], "run.bat", { type: "" }));
    expect(toastText()).toBe("Blocked unsafe file: run.bat");
    choose(new File(["x"], "ok.png", { type: "image/png" }));
    await vi.waitFor(() => expect(toastText()).toBe("Failed to attach: nope"));
    choose(null);
  });

  it("the image picker reports sync failures and explains when no handler exists", () => {
    const onPasteFile = vi.fn(() => { throw new Error("sync"); });
    const first = setup({ onPasteFile });
    fireEvent.mouseDown(tool("Insert image"));
    const input = document.body.querySelector('input[type="file"]');
    Object.defineProperty(input, "files", { value: [new File(["x"], "ok.png", { type: "image/png" })] });
    fireEvent.change(input);
    expect(toastText()).toBe("Failed to attach: sync");
    first.unmount();

    setup();
    fireEvent.mouseDown(tool("Insert image"));
    expect(toastText()).toBe("Attach files via the attachment area for this form.");
  });
});
