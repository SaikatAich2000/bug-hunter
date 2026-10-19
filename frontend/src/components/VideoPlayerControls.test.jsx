/** Video player: media-event driven state, seek/volume sliders, keyboard shortcuts, fullscreen and picture-in-picture. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

let VideoPlayer;
beforeEach(async () => {
  vi.resetModules();
  VideoPlayer = (await import("./VideoPlayer.jsx")).default;
});
afterEach(() => {
  delete document.pictureInPictureEnabled;
  delete document.pictureInPictureElement;
  delete document.fullscreenElement;
  delete document.exitFullscreen;
  delete document.webkitFullscreenElement;
  delete document.webkitExitFullscreen;
  vi.restoreAllMocks();
});

/** A video element whose media properties behave (jsdom does not implement playback). */
function mountPlayer(props = {}) {
  const utils = render(<VideoPlayer src="/v.mp4" type="video/mp4" label="Clip" {...props} />);
  const video = document.querySelector("video");
  const state = { duration: 100, currentTime: 10, volume: 1, muted: false, paused: true, buffered: { length: 0, end: () => 0 } };
  for (const key of Object.keys(state)) {
    Object.defineProperty(video, key, { configurable: true, get: () => state[key], set: (v) => { state[key] = v; } });
  }
  const emit = (name) => act(() => { video.dispatchEvent(new Event(name)); });
  return { ...utils, video, state, emit, wrap: document.querySelector(".vplayer") };
}

describe("media state", () => {
  it("shows elapsed and total time (with hours) and moves the seek thumb as the video plays", () => {
    const { state, emit } = mountPlayer();
    state.duration = 3725;
    state.currentTime = 65;
    emit("durationchange");
    emit("timeupdate");
    expect(screen.getByText("1:05")).toBeInTheDocument();
    expect(screen.getByText("1:02:05")).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Seek" })).toHaveValue("65");
    expect(document.querySelector(".vplayer-seek-played").style.width).toMatch(/^1\.7/);
  });

  it("an unknown duration reads 0:00", () => {
    const { state, emit } = mountPlayer();
    state.duration = Number.NaN;
    emit("loadedmetadata");
    expect(screen.getAllByText("0:00")).toHaveLength(2);
  });

  it("play, pause and buffering toggle the controls and the spinner", () => {
    const { emit } = mountPlayer();
    expect(screen.getAllByRole("button", { name: "Play" })).toHaveLength(2);
    emit("play");
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Play" })).not.toBeInTheDocument();
    emit("waiting");
    expect(document.querySelector(".vplayer-spinner")).not.toBeNull();
    emit("playing");
    expect(document.querySelector(".vplayer-spinner")).toBeNull();
    emit("pause");
    expect(screen.getAllByRole("button", { name: "Play" })).toHaveLength(2);
  });

  it("buffered progress is drawn; an error while reading it is ignored", () => {
    const { state, emit } = mountPlayer();
    emit("durationchange");
    state.buffered = { length: 1, end: () => 40 };
    emit("progress");
    expect(document.querySelector(".vplayer-seek-buffered").style.width).toBe("40%");
    state.buffered = { get length() { throw new Error("no data"); } };
    expect(() => emit("progress")).not.toThrow();
  });

  it("volume changes pick the right icon", () => {
    const { state, emit } = mountPlayer();
    const icon = () => screen.getByRole("button", { name: /Mute|Unmute/ }).textContent;
    state.volume = 0.3;
    emit("volumechange");
    expect(icon()).toBe("🔉");
    state.volume = 0.8;
    emit("volumechange");
    expect(icon()).toBe("🔊");
    state.volume = 0;
    emit("volumechange");
    expect(icon()).toBe("🔇");
    state.volume = 1;
    state.muted = true;
    emit("volumechange");
    expect(screen.getByRole("button", { name: "Unmute" })).toBeInTheDocument();
  });
});

describe("controls", () => {
  it("the seek slider jumps within the clip", () => {
    const { state, emit } = mountPlayer();
    emit("durationchange");
    fireEvent.change(screen.getByRole("slider", { name: "Seek" }), { target: { value: "30" } });
    expect(state.currentTime).toBe(30);
  });

  it("the volume slider sets volume and mutes at zero", () => {
    const { state } = mountPlayer();
    fireEvent.change(screen.getByRole("slider", { name: "Volume" }), { target: { value: "0.4" } });
    expect(state.volume).toBe(0.4);
    expect(state.muted).toBe(false);
    fireEvent.change(screen.getByRole("slider", { name: "Volume" }), { target: { value: "0" } });
    expect(state.muted).toBe(true);
  });

  it("unmuting a zero-volume video restores half volume", async () => {
    const user = userEvent.setup();
    const { state } = mountPlayer();
    state.volume = 0;
    await user.click(screen.getByRole("button", { name: "Mute" }));
    expect(state.muted).toBe(false);
    expect(state.volume).toBe(0.5);
  });

  it("clicking the picture plays or pauses it", async () => {
    const user = userEvent.setup();
    const { video, state } = mountPlayer();
    const play = vi.spyOn(HTMLMediaElement.prototype, "play");
    const pause = vi.spyOn(HTMLMediaElement.prototype, "pause");
    await user.click(video);
    expect(play).toHaveBeenCalledTimes(1);
    state.paused = false;
    await user.click(video);
    expect(pause).toHaveBeenCalledTimes(1);
  });

  it("keyboard: arrows seek and change volume, digits jump to tenths, space plays, M mutes", () => {
    const { video, state } = mountPlayer();
    const key = (k) => fireEvent.keyDown(video, { key: k });
    key("ArrowRight");
    expect(state.currentTime).toBe(15);
    key("ArrowLeft");
    expect(state.currentTime).toBe(10);
    state.volume = 0.5;
    key("ArrowUp");
    expect(state.volume).toBeCloseTo(0.6);
    key("ArrowDown");
    key("ArrowDown");
    expect(state.volume).toBeCloseTo(0.4);
    key("5");
    expect(state.currentTime).toBe(50);
    const play = vi.spyOn(HTMLMediaElement.prototype, "play");
    key(" ");
    expect(play).toHaveBeenCalled();
    key("M");
    expect(state.muted).toBe(true);
    const unrelated = new KeyboardEvent("keydown", { key: "x", bubbles: true, cancelable: true });
    video.dispatchEvent(unrelated);
    expect(unrelated.defaultPrevented).toBe(false);
  });

  it("digits do nothing while the duration is unknown", () => {
    const { video, state } = mountPlayer();
    state.duration = Number.POSITIVE_INFINITY;
    const e = new KeyboardEvent("keydown", { key: "3", bubbles: true, cancelable: true });
    video.dispatchEvent(e);
    expect(e.defaultPrevented).toBe(false);
  });
});

describe("fullscreen and picture in picture", () => {
  it("the button enters fullscreen on the player; F exits; the state follows the fullscreen event", async () => {
    const user = userEvent.setup();
    const { wrap, video } = mountPlayer();
    wrap.requestFullscreen = vi.fn(() => Promise.resolve());
    await user.click(screen.getByRole("button", { name: "Fullscreen" }));
    expect(wrap.requestFullscreen).toHaveBeenCalledTimes(1);
    Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => wrap });
    document.exitFullscreen = vi.fn(() => Promise.resolve());
    act(() => { document.dispatchEvent(new Event("fullscreenchange")); });
    expect(wrap).toHaveClass("is-fullscreen");
    expect(screen.getByRole("button", { name: "Fullscreen" })).toHaveTextContent("🗗");
    fireEvent.keyDown(video, { key: "f" });
    expect(document.exitFullscreen).toHaveBeenCalledTimes(1);
    Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => null });
    act(() => { document.dispatchEvent(new Event("webkitfullscreenchange")); });
    expect(wrap).not.toHaveClass("is-fullscreen");
  });

  it("double click toggles fullscreen using the webkit API when that is all there is", () => {
    const { wrap, video } = mountPlayer();
    wrap.webkitRequestFullscreen = vi.fn();
    fireEvent.doubleClick(video);
    expect(wrap.webkitRequestFullscreen).toHaveBeenCalled();
    Object.defineProperty(document, "webkitFullscreenElement", { configurable: true, get: () => wrap });
    document.webkitExitFullscreen = vi.fn();
    fireEvent.doubleClick(video);
    expect(document.webkitExitFullscreen).toHaveBeenCalled();
  });

  it("where the wrapper cannot go fullscreen the video element itself does", () => {
    const { wrap, video } = mountPlayer();
    wrap.requestFullscreen = undefined;
    video.webkitEnterFullscreen = vi.fn();
    fireEvent.keyDown(video, { key: "F" });
    expect(video.webkitEnterFullscreen).toHaveBeenCalledTimes(1);
  });

  it("a rejected fullscreen request is swallowed", async () => {
    const user = userEvent.setup();
    const { wrap } = mountPlayer();
    wrap.requestFullscreen = vi.fn(() => Promise.reject(new Error("denied")));
    await user.click(screen.getByRole("button", { name: "Fullscreen" }));
    expect(wrap.requestFullscreen).toHaveBeenCalled();
  });

  it("picture-in-picture is offered only where the browser supports it, and toggles", async () => {
    const user = userEvent.setup();
    mountPlayer();
    expect(screen.queryByRole("button", { name: "Picture in picture" })).not.toBeInTheDocument();
    document.body.innerHTML = "";
    vi.resetModules();
    document.pictureInPictureEnabled = true;
    VideoPlayer = (await import("./VideoPlayer.jsx")).default;
    const { video } = mountPlayer();
    video.requestPictureInPicture = vi.fn(() => Promise.resolve());
    const pip = screen.getByRole("button", { name: "Picture in picture" });
    await user.click(pip);
    expect(video.requestPictureInPicture).toHaveBeenCalledTimes(1);
    document.pictureInPictureElement = video;
    document.exitPictureInPicture = vi.fn(() => Promise.resolve());
    await user.click(pip);
    expect(document.exitPictureInPicture).toHaveBeenCalledTimes(1);
    delete document.exitPictureInPicture;
  });
});
