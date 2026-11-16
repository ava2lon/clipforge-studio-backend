import React from "react";
import {
  AbsoluteFill,
  Composition,
  OffthreadVideo,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

type Caption = { start: number; end: number; text: string };
type FocusKeyframe = { time: number; x: number; y: number };
type ClipForgeProps = {
  durationSeconds: number;
  captions: Caption[];
  aspectRatio: "9:16" | "1:1" | "16:9";
  focusKeyframes: FocusKeyframe[];
  sourceWidth: number;
  sourceHeight: number;
};

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value));

const focusAt = (keyframes: FocusKeyframe[], seconds: number) => {
  if (!keyframes.length) return { x: 0.5, y: 0.5 };
  if (seconds <= keyframes[0].time) return keyframes[0];
  const last = keyframes[keyframes.length - 1];
  if (seconds >= last.time) return last;
  for (let index = 1; index < keyframes.length; index += 1) {
    const right = keyframes[index];
    if (seconds <= right.time) {
      const left = keyframes[index - 1];
      const span = Math.max(0.001, right.time - left.time);
      const progress = (seconds - left.time) / span;
      return {
        x: left.x + (right.x - left.x) * progress,
        y: left.y + (right.y - left.y) * progress,
      };
    }
  }
  return { x: 0.5, y: 0.5 };
};

const cropPosition = (
  focus: { x: number; y: number },
  sourceWidth: number,
  sourceHeight: number,
  outputWidth: number,
  outputHeight: number,
) => {
  if (sourceWidth <= 0 || sourceHeight <= 0) return "50% 50%";
  const sourceAspect = sourceWidth / sourceHeight;
  const outputAspect = outputWidth / outputHeight;
  let x = 50;
  let y = 50;
  if (sourceAspect > outputAspect + 0.001) {
    x = clamp(
      ((focus.x * sourceAspect - outputAspect / 2) / (sourceAspect - outputAspect)) * 100,
      0,
      100,
    );
  } else if (sourceAspect < outputAspect - 0.001) {
    const overflowRatio = outputAspect / sourceAspect;
    y = clamp(
      ((focus.y * overflowRatio - 0.5) / (overflowRatio - 1)) * 100,
      0,
      100,
    );
  }
  return `${x}% ${y}%`;
};

const CaptionOverlay: React.FC<{ captions: Caption[] }> = ({ captions }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const seconds = frame / fps;
  const active = captions.find((caption) => seconds >= caption.start && seconds <= caption.end);

  if (!active?.text) return null;

  return (
    <div style={{
      position: "absolute",
      left: 64,
      right: 64,
      bottom: 230,
      display: "flex",
      justifyContent: "center",
      textAlign: "center",
      fontFamily: "Arial, sans-serif",
      fontSize: 58,
      fontWeight: 900,
      lineHeight: 1.12,
      color: "#ffffff",
      textShadow: "0 3px 12px rgba(0,0,0,0.95)",
      background: "rgba(0,0,0,0.35)",
      borderRadius: 18,
      padding: "18px 24px",
    }}>
      {active.text}
    </div>
  );
};

const ClipForgeComposition: React.FC<ClipForgeProps> = ({
  captions, focusKeyframes, sourceWidth, sourceHeight,
}) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const focus = focusAt(focusKeyframes, frame / fps);
  const objectPosition = cropPosition(focus, sourceWidth, sourceHeight, width, height);

  return (
    <AbsoluteFill style={{ backgroundColor: "#080b12" }}>
      <OffthreadVideo
        src={staticFile("clip.mp4")}
        style={{ width: "100%", height: "100%", objectFit: "cover", objectPosition }}
      />
      <CaptionOverlay captions={captions} />
    </AbsoluteFill>
  );
};

export const RemotionRoot: React.FC = () => (
  <Composition<ClipForgeProps>
    id="ClipForge"
    component={ClipForgeComposition}
    durationInFrames={150}
    fps={30}
    width={1080}
    height={1920}
    defaultProps={{ durationSeconds: 5, captions: [], aspectRatio: "9:16", focusKeyframes: [], sourceWidth: 0, sourceHeight: 0 }}
    calculateMetadata={({ props }) => {
      const dimensions = props.aspectRatio === "16:9" ? { width: 1920, height: 1080 }
        : props.aspectRatio === "1:1" ? { width: 1080, height: 1080 }
        : { width: 1080, height: 1920 };
      return {
        ...dimensions,
        durationInFrames: Math.max(1, Math.ceil(props.durationSeconds * 30)),
      };
    }}
  />
);
