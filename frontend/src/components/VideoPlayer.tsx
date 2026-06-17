/**
 * VideoPlayer — a polished, dependency-free player built on the native <video>
 * element (so decoding stays hardware-accelerated). The themed control bar is
 * ALWAYS visible (windowed and fullscreen) so the seek bar is always grabbable.
 *
 * Features: play/pause, a draggable seek bar with a buffered track + thumb,
 * current/total time, mute + volume slider, a playback-speed cycle, optional
 * picture-in-picture, fullscreen, a buffering spinner, and keyboard shortcuts
 * (Space/k play, ←/→ seek 5s, ↑/↓ volume, m mute, f fullscreen, 0-9 seek to %).
 *
 * Used both inline-large and inside <VideoLightbox/> (the "View" experience).
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";

interface Props {
  src: string;
  type?: string;
  /** Filename — used as the aria-label. */
  label?: string;
  /** Try to start playback on mount (the lightbox opens on a user click). */
  autoPlay?: boolean;
}

const SPEEDS = [0.5, 1, 1.25, 1.5, 2] as const;
const DEFAULT_SPEED_IDX = 1; // 1×

function fmtTime(s: number): string {
  if (!Number.isFinite(s) || s < 0) s = 0;
  const total = Math.floor(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  const ss = String(sec).padStart(2, "0");
  if (h) return `${h}:${String(m).padStart(2, "0")}:${ss}`;
  return `${m}:${ss}`;
}

function volumeIcon(muted: boolean, volume: number): string {
  if (muted || volume === 0) return "🔇";
  if (volume < 0.5) return "🔉";
  return "🔊";
}

const PIP_SUPPORTED =
  typeof document !== "undefined" && Boolean(document.pictureInPictureEnabled);

export default function VideoPlayer({ src, type, label, autoPlay = false }: Readonly<Props>) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  const [playing, setPlaying] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(0);
  const [buffered, setBuffered] = useState(0);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [speedIdx, setSpeedIdx] = useState(DEFAULT_SPEED_IDX);
  const [isFs, setIsFs] = useState(false);

  // ----- wire native <video> events -----
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const onTime = () => setCurrent(v.currentTime);
    const onDur = () => setDuration(Number.isFinite(v.duration) ? v.duration : 0);
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onWaiting = () => setWaiting(true);
    const onPlayingNow = () => setWaiting(false);
    const onVol = () => {
      setVolume(v.volume);
      setMuted(v.muted);
    };
    const onProgress = () => {
      try {
        if (v.buffered.length) setBuffered(v.buffered.end(v.buffered.length - 1));
      } catch {
        /* buffered can throw before any data */
      }
    };
    v.addEventListener("timeupdate", onTime);
    v.addEventListener("durationchange", onDur);
    v.addEventListener("loadedmetadata", onDur);
    v.addEventListener("play", onPlay);
    v.addEventListener("pause", onPause);
    v.addEventListener("waiting", onWaiting);
    v.addEventListener("playing", onPlayingNow);
    v.addEventListener("volumechange", onVol);
    v.addEventListener("progress", onProgress);
    return () => {
      v.removeEventListener("timeupdate", onTime);
      v.removeEventListener("durationchange", onDur);
      v.removeEventListener("loadedmetadata", onDur);
      v.removeEventListener("play", onPlay);
      v.removeEventListener("pause", onPause);
      v.removeEventListener("waiting", onWaiting);
      v.removeEventListener("playing", onPlayingNow);
      v.removeEventListener("volumechange", onVol);
      v.removeEventListener("progress", onProgress);
    };
  }, []);

  // Track fullscreen (incl. Esc-exit).
  useEffect(() => {
    const onFs = () => setIsFs(document.fullscreenElement === wrapRef.current);
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  // Autoplay (lightbox). If the browser blocks it, the big play button is
  // right there.
  useEffect(() => {
    if (!autoPlay) return;
    const v = videoRef.current;
    if (v) void v.play().catch(() => undefined);
  }, [autoPlay]);

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) void v.play().catch(() => undefined);
    else v.pause();
  }, []);

  const seekTo = useCallback((t: number) => {
    const v = videoRef.current;
    if (!v) return;
    const dur = Number.isFinite(v.duration) ? v.duration : 0;
    v.currentTime = Math.max(0, Math.min(t, dur));
    setCurrent(v.currentTime);
  }, []);

  const setVol = useCallback((val: number) => {
    const v = videoRef.current;
    if (!v) return;
    const nv = Math.max(0, Math.min(1, val));
    v.volume = nv;
    v.muted = nv === 0;
  }, []);

  const toggleMute = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.muted || v.volume === 0) {
      v.muted = false;
      if (v.volume === 0) v.volume = 0.5;
    } else {
      v.muted = true;
    }
  }, []);

  const cycleSpeed = useCallback(() => {
    setSpeedIdx((idx) => {
      const next = (idx + 1) % SPEEDS.length;
      if (videoRef.current) videoRef.current.playbackRate = SPEEDS[next];
      return next;
    });
  }, []);

  const togglePip = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (document.pictureInPictureElement) {
      void document.exitPictureInPicture().catch(() => undefined);
    } else {
      void v.requestPictureInPicture?.().catch(() => undefined);
    }
  }, []);

  const toggleFs = useCallback(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    if (document.fullscreenElement === wrap) {
      void document.exitFullscreen().catch(() => undefined);
    } else {
      void wrap.requestFullscreen().catch(() => undefined);
    }
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const v = videoRef.current;
    if (!v) return;
    switch (e.key) {
      case " ":
      case "k":
        e.preventDefault();
        togglePlay();
        break;
      case "ArrowLeft":
        e.preventDefault();
        seekTo(v.currentTime - 5);
        break;
      case "ArrowRight":
        e.preventDefault();
        seekTo(v.currentTime + 5);
        break;
      case "ArrowUp":
        e.preventDefault();
        setVol(v.volume + 0.1);
        break;
      case "ArrowDown":
        e.preventDefault();
        setVol(v.volume - 0.1);
        break;
      case "m":
      case "M":
        toggleMute();
        break;
      case "f":
      case "F":
        toggleFs();
        break;
      default:
        if (/^\d$/.test(e.key) && duration) {
          e.preventDefault();
          seekTo((Number(e.key) / 10) * duration);
        }
        break;
    }
  };

  const pct = duration ? (current / duration) * 100 : 0;
  const bufPct = duration ? (buffered / duration) * 100 : 0;
  const volPct = muted ? 0 : volume * 100;

  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div
      className={`vplayer${isFs ? " is-fullscreen" : ""}`}
      ref={wrapRef}
      role="group"
      aria-label={label ? `Video: ${label}` : "Video player"}
      tabIndex={0}
      onKeyDown={onKeyDown}
    >
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <video
        ref={videoRef}
        className="vplayer-video"
        preload="metadata"
        playsInline
        onClick={togglePlay}
        onDoubleClick={toggleFs}
      >
        <source src={src} type={type} />
      </video>

      {waiting && playing && <div className="vplayer-spinner" aria-hidden="true" />}

      {!playing && (
        <button type="button" className="vplayer-bigplay" aria-label="Play" onClick={togglePlay}>
          <svg viewBox="0 0 24 24" width="32" height="32" aria-hidden="true">
            <path fill="currentColor" d="M8 5v14l11-7z" />
          </svg>
        </button>
      )}

      <div className="vplayer-bar">
        <button
          type="button"
          className="vplayer-btn"
          aria-label={playing ? "Pause" : "Play"}
          onClick={togglePlay}
        >
          {playing ? "⏸" : "▶"}
        </button>

        <span className="vplayer-time">{fmtTime(current)}</span>

        <div className="vplayer-seek">
          <div className="vplayer-seek-track">
            <div className="vplayer-seek-buffered" style={{ width: `${bufPct}%` }} />
            <div className="vplayer-seek-played" style={{ width: `${pct}%` }} />
          </div>
          <input
            type="range"
            className="vplayer-range vplayer-seek-input"
            min={0}
            max={duration || 0}
            step="any"
            value={current}
            aria-label="Seek"
            onChange={(e) => seekTo(Number(e.target.value))}
          />
        </div>

        <span className="vplayer-time">{fmtTime(duration)}</span>

        <button
          type="button"
          className="vplayer-btn"
          aria-label={muted ? "Unmute" : "Mute"}
          onClick={toggleMute}
        >
          {volumeIcon(muted, volume)}
        </button>
        <div className="vplayer-volume">
          <div className="vplayer-volume-track">
            <div className="vplayer-volume-fill" style={{ width: `${volPct}%` }} />
          </div>
          <input
            type="range"
            className="vplayer-range vplayer-volume-input"
            min={0}
            max={1}
            step={0.05}
            value={muted ? 0 : volume}
            aria-label="Volume"
            onChange={(e) => setVol(Number(e.target.value))}
          />
        </div>

        <button
          type="button"
          className="vplayer-btn vplayer-speed"
          aria-label="Playback speed"
          onClick={cycleSpeed}
        >
          {SPEEDS[speedIdx]}×
        </button>

        {PIP_SUPPORTED && (
          <button
            type="button"
            className="vplayer-btn"
            aria-label="Picture in picture"
            onClick={togglePip}
          >
            ⧉
          </button>
        )}

        <button type="button" className="vplayer-btn" aria-label="Fullscreen" onClick={toggleFs}>
          {isFs ? "🗗" : "⛶"}
        </button>
      </div>
    </div>
  );
}
