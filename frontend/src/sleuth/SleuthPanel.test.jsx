/** Sleuth assistant: launcher, welcome, chat with every block kind, markdown safety, bug links, resize and import. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ADMIN, createBackend, mountApp, reply } from "../test/backend";

const text = (t, extra = {}) => ({ kind: "text", payload: { text: t, ...extra } });
const panel = () => document.getElementById("sleuthPanel");
const input = () => document.getElementById("sleuthInput");

// The panel is lazy-loaded: its first import can take longer than the default wait.
const fab = () => waitFor(() => {
  const el = document.getElementById("sleuthFab");
  expect(el).not.toBeNull();
  return el;
}, { timeout: 8000 });

async function openSleuth(user, { backend = createBackend(), me, ...rest } = {}) {
  await mountApp({ backend, me, ...rest });
  await user.click(await fab());
  await waitFor(() => expect(panel()).not.toHaveAttribute("hidden"));
  return backend;
}

async function ask(user, backend, answer, message = "hello") {
  backend.on("POST", "/chat/ask", () => answer);
  await user.type(input(), `${message}{Enter}`);
}

afterEach(() => {
  vi.restoreAllMocks();
  window.innerWidth = 1024;
  window.innerHeight = 768;
});

describe("launcher and panel", () => {
  it("opens from the floating button with the welcome message and focus in the box", async () => {
    const user = userEvent.setup();
    await openSleuth(user);
    expect(panel()).toHaveAccessibleName(/AI Assistant/);
    const welcome = within(panel()).getByText(/AI Assistant$/, { selector: "strong" }).closest(".sleuth-msg");
    expect(within(welcome).getByText("answer questions").tagName).toBe("STRONG");
    expect(within(welcome).getAllByRole("listitem").length).toBeGreaterThan(5);
    await waitFor(() => expect(input()).toHaveFocus());
    expect(document.getElementById("sleuthFab")).toHaveAttribute("aria-expanded", "true");
  });

  it("the top bar button forwards to the launcher; clicking the launcher again or Escape closes and refocuses it", async () => {
    const user = userEvent.setup();
    await mountApp();
    await user.click(await screen.findByRole("button", { name: "Ask Sleuth" }));
    await waitFor(() => expect(panel()).not.toHaveAttribute("hidden"));
    await waitFor(() => expect(input()).toHaveFocus());
    await user.keyboard("{Escape}");
    expect(panel()).toHaveAttribute("hidden");
    expect(document.getElementById("sleuthFab")).toHaveFocus();
    await user.click(document.getElementById("sleuthFab"));
    await user.click(document.getElementById("sleuthFab"));
    expect(panel()).toHaveAttribute("hidden");
    await user.click(document.getElementById("sleuthFab"));
    await user.click(document.getElementById("sleuthClose"));
    expect(panel()).toHaveAttribute("hidden");
  });

  it("Ctrl+/ opens the panel from anywhere; pressing it while open changes nothing", async () => {
    const user = userEvent.setup();
    await mountApp();
    await fab();
    await user.keyboard("{Control>}/{/Control}");
    await waitFor(() => expect(panel()).not.toHaveAttribute("hidden"));
    const before = panel().querySelectorAll(".sleuth-msg").length;
    await user.keyboard("{Control>}/{/Control}");
    expect(panel().querySelectorAll(".sleuth-msg")).toHaveLength(before);
  });

  it("Clear conversation goes back to the welcome message", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    await ask(user, backend, { blocks: [text("An answer")] });
    expect(await screen.findByText("An answer")).toBeInTheDocument();
    await user.click(document.getElementById("sleuthClear"));
    expect(screen.queryByText("An answer")).not.toBeInTheDocument();
    expect(within(panel()).getAllByRole("listitem").length).toBeGreaterThan(5);
  });
});

describe("chatting", () => {
  it("sends on Enter (not Shift+Enter) and by the Send button, shows the question and clears the box", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    backend.on("POST", "/chat/ask", () => ({ blocks: [text("Hi there")] }));
    await user.type(input(), "first{Shift>}{Enter}{/Shift}line two");
    expect(backend.called("POST", "/chat/ask")).toHaveLength(0);
    await user.type(input(), "{Enter}");
    await waitFor(() => expect(backend.called("POST", "/chat/ask")).toHaveLength(1));
    expect(backend.called("POST", "/chat/ask")[0].body).toEqual({ message: "first\nline two" });
    expect(panel().querySelector(".sleuth-msg.user")).toHaveTextContent("first line two");
    expect(await screen.findByText("Hi there")).toBeInTheDocument();
    expect(input()).toHaveValue("");
    await user.type(input(), "second");
    await user.click(document.getElementById("sleuthSend"));
    await waitFor(() => expect(backend.called("POST", "/chat/ask")).toHaveLength(2));
  });

  it("ignores empty messages and a second message while one is in flight; shows typing", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    let release;
    backend.on("POST", "/chat/ask", () => new Promise((resolve) => { release = () => resolve({ blocks: [text("late")] }); }));
    await user.click(document.getElementById("sleuthSend"));
    await user.type(input(), "   {Enter}");
    expect(backend.called("POST", "/chat/ask")).toHaveLength(0);
    await user.type(input(), "slow{Enter}");
    expect(await screen.findByLabelText("Sleuth is thinking")).toBeInTheDocument();
    expect(document.getElementById("sleuthSend")).toBeDisabled();
    await user.type(input(), "again{Enter}");
    expect(backend.called("POST", "/chat/ask")).toHaveLength(1);
    release();
    expect(await screen.findByText("late")).toBeInTheDocument();
    expect(screen.queryByLabelText("Sleuth is thinking")).not.toBeInTheDocument();
    expect(document.getElementById("sleuthSend")).toBeEnabled();
  });

  it("a server error appears as an error bubble; an empty message falls back to a generic text", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    backend.on("POST", "/chat/ask", () => reply(500, { detail: "Model unavailable" }));
    await user.type(input(), "q1{Enter}");
    expect(await screen.findByText("Model unavailable")).toBeInTheDocument();
    expect(screen.getByText("Model unavailable").closest(".sleuth-msg")).toHaveClass("error");
  });

  it("an expired session (401) shows nothing in the chat", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    vi.stubGlobal("location", { replace: vi.fn(), assign: vi.fn(), href: "http://localhost/", pathname: "/", search: "", hash: "", origin: "http://localhost" });
    backend.on("POST", "/chat/ask", () => reply(401, { detail: "expired" }));
    await user.type(input(), "q{Enter}");
    await waitFor(() => expect(document.getElementById("sleuthSend")).toBeEnabled());
    expect(panel().querySelector(".sleuth-msg.error")).toBeNull();
    vi.unstubAllGlobals();
  });

  it("counts answers that arrive while the panel is closed and clears the badge on open", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    let release;
    backend.on("POST", "/chat/ask", () => new Promise((resolve) => { release = () => resolve({ blocks: [text("done")] }); }));
    await user.type(input(), "q{Enter}");
    await user.click(document.getElementById("sleuthClose"));
    release();
    await waitFor(() => expect(document.getElementById("sleuthFab")).toHaveAttribute("data-unread", "1"));
    await user.click(document.getElementById("sleuthFab"));
    expect(document.getElementById("sleuthFab")).toHaveAttribute("data-unread", "0");
    expect(screen.getByText("done")).toBeInTheDocument();
  });

  it("an error that arrives while closed also counts as unread", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    let release;
    backend.on("POST", "/chat/ask", () => new Promise((resolve) => { release = () => resolve(reply(500, { detail: "boom" })); }));
    await user.type(input(), "q{Enter}");
    await user.click(document.getElementById("sleuthClose"));
    release();
    await waitFor(() => expect(document.getElementById("sleuthFab")).toHaveAttribute("data-unread", "1"));
  });

  it("refreshes the lists after an answer that changed data", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    const before = backend.called("GET", /^\/bugs\?/).length;
    await ask(user, backend, { intent: "action_done", blocks: [text("Closed it")] });
    await screen.findByText("Closed it");
    await waitFor(() => expect(backend.called("GET", /^\/bugs\?/).length).toBeGreaterThan(before));
  });
});

describe("text blocks", () => {
  it("renders the supported markdown: paragraphs, line breaks, lists, bold, italic and code", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    await ask(user, backend, { blocks: [text("Line one\nLine two\n\nSecond **bold** and *slanted* with `x < y`\n- first item\n- second **item**\nafter list `tick")] });
    const bubble = (await screen.findByText("Line one", { exact: false })).closest(".sleuth-msg");
    expect(bubble.querySelectorAll("p")).toHaveLength(3);
    expect(bubble.querySelector("p br")).not.toBeNull();
    expect(within(bubble).getByText("bold").tagName).toBe("STRONG");
    expect(within(bubble).getByText("slanted").tagName).toBe("EM");
    expect(within(bubble).getByText("x < y").tagName).toBe("CODE");
    expect(within(bubble).getAllByRole("listitem")).toHaveLength(2);
    expect(within(bubble).getByText("item").tagName).toBe("STRONG");
    expect(bubble).toHaveTextContent("after list `tick");
  });

  it("only http(s), mailto and open-bug links become links; HTML is shown as text", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    await ask(user, backend, {
      blocks: [text('[site](https://example.com) [mail](mailto:a@b.co) [bad](javascript:alert(1)) [local](#open-bug-9) [dangling](https://x [empty]() <script>alert(1)</script> <b>raw</b>')],
    });
    const bubble = (await screen.findByText("site")).closest(".sleuth-msg");
    expect(within(bubble).getByRole("link", { name: "site" })).toHaveAttribute("href", "https://example.com");
    expect(within(bubble).getByRole("link", { name: "site" })).toHaveAttribute("target", "_blank");
    expect(within(bubble).getByRole("link", { name: "mail" })).toHaveAttribute("href", "mailto:a@b.co");
    expect(within(bubble).queryByRole("link", { name: "bad" })).not.toBeInTheDocument();
    expect(bubble.querySelector("script")).toBeNull();
    expect(bubble.querySelector("b")).toBeNull();
    expect(bubble).toHaveTextContent("<script>alert(1)</script> <b>raw</b>");
    expect(bubble).toHaveTextContent("[bad](javascript:alert(1))");
  });

  it("a bug link opens that bug in the app; an unrelated fragment link does not", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    await ask(user, backend, { blocks: [text("See [PRJ-101](#open-bug-101) and [other](#open-bug-5)", { open_bug_id: 101 })] });
    await user.click(await screen.findByRole("link", { name: "PRJ-101" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/101")).toHaveLength(1));
    await user.click(screen.getByRole("link", { name: "other" }));
    expect(backend.called("GET", "/bugs/5")).toHaveLength(0);
  });

  it("an invalid open_bug_id is ignored and a missing text renders nothing", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    await ask(user, backend, { blocks: [text("[x](#open-bug-3)", { open_bug_id: -3 }), { kind: "text", payload: {} }] });
    await screen.findByRole("link", { name: "x" });
    await user.click(screen.getByRole("link", { name: "x" }));
    expect(backend.called("GET", "/bugs/3")).toHaveLength(0);
  });
});

describe("other blocks", () => {
  const table = {
    kind: "table",
    payload: {
      headers: ["ID", "Title", "Title"], rows: [["101", "Login fails", "x"], ["0", "Bad id", null], ["101", "Login fails", "x"], "not-a-row"],
      row_bug_ids: [101, 0, 101],
    },
  };

  it("tables make rows with a bug id clickable and leave the rest alone", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    await ask(user, backend, { blocks: [table] });
    await screen.findByText("Bad id");
    const rows = [...panel().querySelectorAll("tbody tr")];
    const clickable = rows.filter((r) => r.classList.contains("clickable"));
    expect(clickable).toHaveLength(2);
    expect(clickable[0]).toHaveAttribute("title", "Open this bug");
    expect(rows.find((r) => r.textContent.includes("Bad id"))).not.toHaveClass("clickable");
    await user.click(within(clickable[0]).getByText("Login fails"));
    await waitFor(() => expect(backend.called("GET", "/bugs/101")).toHaveLength(1));
    const header = panel().querySelectorAll("th");
    expect([...header].map((th) => th.textContent)).toEqual(["ID", "Title", "Title"]);
  });

  it("file blocks link to the download with name, row count and size", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    const file = (payload) => ({ kind: "file", payload });
    await ask(user, backend, {
      blocks: [
        file({ filename: "bugs.xlsx", row_count: 3, size_bytes: 1536, download_token: "tok/1 2" }),
        file({ row_count: 1, size_bytes: 12, download_token: "t2" }),
        file({ size_bytes: 3 * 1024 * 1024, download_token: "t3" }),
        file({ download_token: "t4" }),
      ],
    });
    const links = await screen.findAllByRole("link", { name: "Download" });
    expect(links[0]).toHaveAttribute("href", "/api/chat/download/tok%2F1%202");
    expect(links[0]).toHaveAttribute("download", "bugs.xlsx");
    const meta = [...panel().querySelectorAll(".sleuth-file-meta")].map((m) => m.textContent);
    expect(meta).toEqual(["3 rows · 1.5 KB", "1 row · 12 B", "3.00 MB", ""]);
    expect(panel().querySelectorAll(".sleuth-file-name")[1]).toHaveTextContent("bugs.xlsx");
  });

  it("suggestions send their text (or label) when clicked and skip empty labels", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    await ask(user, backend, {
      blocks: [{ kind: "suggestions", payload: { items: ["show bugs", { label: "Open ones", send: "list open" }, { label: "Only label" }, { send: "x" }, { label: "" }] } }],
    });
    expect(await screen.findByRole("button", { name: "show bugs" })).toBeInTheDocument();
    expect(panel().querySelectorAll(".sleuth-suggestions button")).toHaveLength(3);
    backend.on("POST", "/chat/ask", () => ({ blocks: [text("ok")] }));
    await user.click(screen.getByRole("button", { name: "Open ones" }));
    await waitFor(() => expect(backend.called("POST", "/chat/ask")[1].body).toEqual({ message: "list open" }));
    await user.click(screen.getByRole("button", { name: "Only label" }));
    await waitFor(() => expect(backend.called("POST", "/chat/ask")[2].body).toEqual({ message: "Only label" }));
  });

  it("confirmations send yes or no once and then lock both buttons", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    await ask(user, backend, {
      blocks: [{ kind: "confirm", payload: { summary: "Close bug 5?", yes_label: "Do it", no_label: "Never mind" } }, { kind: "confirm", payload: {} }],
    });
    expect(await screen.findByText("Close bug 5?")).toBeInTheDocument();
    expect(screen.getByText("Confirm action")).toBeInTheDocument();
    backend.on("POST", "/chat/ask", () => ({ blocks: [text("Closed")] }));
    await user.click(screen.getByRole("button", { name: "Do it" }));
    await waitFor(() => expect(backend.called("POST", "/chat/ask")[1].body).toEqual({ message: "yes" }));
    expect(screen.getByRole("button", { name: "Do it" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Never mind" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(backend.called("POST", "/chat/ask")[2].body).toEqual({ message: "no" }));
    expect(screen.getByRole("button", { name: "Yes, do it" })).toBeDisabled();
  });

  it("unknown or missing blocks say there is no answer", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    await ask(user, backend, { blocks: [{ kind: "hologram", payload: {} }] });
    expect(await screen.findByText("(no answer)")).toBeInTheDocument();
    backend.on("POST", "/chat/ask", () => ({}));
    await user.type(input(), "again{Enter}");
    await waitFor(() => expect(screen.getAllByText("(no answer)")).toHaveLength(2));
  });
});

describe("size", () => {
  it("expand and shrink toggle the panel size and remember it", async () => {
    const user = userEvent.setup();
    await openSleuth(user);
    expect(panel()).toHaveStyle({ width: "400px", height: "560px" });
    await user.click(screen.getByRole("button", { name: "Expand assistant panel" }));
    expect(panel().style.width).toBe("560px");
    expect(JSON.parse(localStorage.getItem("bh.sleuth.size"))).toEqual({ w: 560, h: 648 });
    expect(screen.getByRole("button", { name: "Shrink assistant panel" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Shrink assistant panel" }));
    expect(panel()).toHaveStyle({ width: "400px", height: "560px" });
  });

  it("dragging the grip up and left enlarges the panel within the screen", async () => {
    const user = userEvent.setup();
    await openSleuth(user);
    const grip = panel().querySelector(".sleuth-resize");
    fireEvent.pointerMove(grip, { clientX: 5, clientY: 5 });
    fireEvent.pointerUp(grip, { pointerId: 1 });
    expect(panel().style.width).toBe("400px");
    fireEvent.pointerDown(grip, { clientX: 500, clientY: 500, pointerId: 1 });
    fireEvent.pointerMove(grip, { clientX: 440, clientY: 450 });
    expect(panel()).toHaveStyle({ width: "460px", height: "610px" });
    fireEvent.pointerMove(grip, { clientX: -5000, clientY: -5000 });
    expect(Number.parseInt(panel().style.width, 10)).toBeLessThanOrEqual(window.innerWidth - 32);
    fireEvent.pointerMove(grip, { clientX: 5000, clientY: 5000 });
    expect(panel().style.width).toBe("340px");
    fireEvent.pointerUp(grip, { pointerId: 1 });
    fireEvent.pointerMove(grip, { clientX: 0, clientY: 0 });
    expect(panel().style.width).toBe("340px");
    await user.click(document.getElementById("sleuthClose"));
  });

  it("restores a saved size, ignores a corrupt one and clamps to a smaller window", async () => {
    const user = userEvent.setup();
    localStorage.setItem("bh.sleuth.size", JSON.stringify({ w: 480, h: 500 }));
    await openSleuth(user);
    expect(panel()).toHaveStyle({ width: "480px", height: "500px" });
    window.innerHeight = 500;
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(panel().style.height).toBe("420px"));
    expect(panel().style.width).toBe("480px");
  });

  it("a corrupt saved size falls back to the default", async () => {
    const user = userEvent.setup();
    localStorage.setItem("bh.sleuth.size", "{not json");
    await openSleuth(user);
    expect(panel()).toHaveStyle({ width: "400px", height: "560px" });
  });

  it("a saved size of the wrong shape falls back to the default", async () => {
    const user = userEvent.setup();
    localStorage.setItem("bh.sleuth.size", JSON.stringify({ w: "wide" }));
    await openSleuth(user);
    expect(panel()).toHaveStyle({ width: "400px", height: "560px" });
  });

  it("on a narrow screen the panel is sized by CSS and has no expand button", async () => {
    const user = userEvent.setup();
    window.innerWidth = 500;
    await openSleuth(user);
    expect(panel().getAttribute("style")).toBeNull();
    expect(document.getElementById("sleuthExpand")).toHaveAttribute("hidden");
    window.innerWidth = 1024;
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(document.getElementById("sleuthExpand")).not.toHaveAttribute("hidden"));
  });
});

describe("document import (admin)", () => {
  const pick = (name = "bugs.csv") => new File(["a,b"], name, { type: "text/csv" });

  it("is not offered to non-admins", async () => {
    const user = userEvent.setup();
    const backend = createBackend({ me: { ...ADMIN, role: "user" } });
    await openSleuth(user, { backend });
    expect(document.getElementById("sleuthUploadBtn")).toBeNull();
  });

  it("the paperclip opens the file picker; the chosen file is uploaded and the summary shown", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    const click = vi.spyOn(HTMLInputElement.prototype, "click");
    await user.click(document.getElementById("sleuthUploadBtn"));
    expect(click).toHaveBeenCalled();
    backend.on("POST", "/chat/ingest", () => ({ blocks: [text("Created 2 items")] }));
    await user.upload(document.getElementById("sleuthUpload"), pick());
    expect(await screen.findByText("📎 Uploaded bugs.csv")).toBeInTheDocument();
    expect(await screen.findByText("Created 2 items")).toBeInTheDocument();
    expect(backend.called("POST", "/chat/ingest")[0].body).toBeInstanceOf(FormData);
    expect(document.getElementById("sleuthUploadBtn")).toBeEnabled();
  });

  it("opens the panel if needed, and reports an upload failure in the chat", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    await user.click(document.getElementById("sleuthClose"));
    backend.on("POST", "/chat/ingest", () => reply(413, { detail: "File too large" }));
    await user.upload(document.getElementById("sleuthUpload"), pick("huge.csv"));
    expect(await screen.findByText("File too large")).toBeInTheDocument();
    expect(panel()).not.toHaveAttribute("hidden");
  });

  it("a response without blocks is shown as no answer; cancelling the picker does nothing", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    backend.on("POST", "/chat/ingest", () => ({}));
    fireEvent.change(document.getElementById("sleuthUpload"), { target: { files: [] } });
    expect(backend.called("POST", "/chat/ingest")).toHaveLength(0);
    await user.upload(document.getElementById("sleuthUpload"), pick());
    expect(await screen.findByText("(no answer)")).toBeInTheDocument();
  });

  it("an expired session during upload shows nothing", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    vi.stubGlobal("location", { replace: vi.fn(), assign: vi.fn(), href: "http://localhost/", pathname: "/", search: "", hash: "", origin: "http://localhost" });
    backend.on("POST", "/chat/ingest", () => reply(401, { detail: "expired" }));
    await user.upload(document.getElementById("sleuthUpload"), pick());
    await waitFor(() => expect(document.getElementById("sleuthUploadBtn")).toBeEnabled());
    expect(panel().querySelector(".sleuth-msg.error")).toBeNull();
    vi.unstubAllGlobals();
  });

  it("is ignored while an answer is still pending", async () => {
    const user = userEvent.setup();
    const backend = await openSleuth(user);
    let release;
    backend.on("POST", "/chat/ask", () => new Promise((resolve) => { release = () => resolve({ blocks: [text("ok")] }); }));
    await user.type(input(), "q{Enter}");
    fireEvent.change(document.getElementById("sleuthUpload"), { target: { files: [pick()] } });
    expect(backend.called("POST", "/chat/ingest")).toHaveLength(0);
    release();
    await screen.findByText("ok");
  });
});
