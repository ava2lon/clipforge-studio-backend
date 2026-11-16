import React from "react";
import {
  AbsoluteFill,
  Composition,
  OffthreadVideo,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  Sequence,
} from "remotion";

type Caption = { start: number; end: number; text: string };
type CaptionStyle = "karaoke" | "bold" | "minimal";
type CaptionPosition = "top" | "center" | "bottom";
type FocusKeyframe = { time: number; x: number; y: number };
type BrollOverlay = { src: string; startSeconds: number; durationSeconds: number };
type ClipForgeProps = {
  durationSeconds: number;
  captions: Caption[];
  aspectRatio: "9:16" | "1:1" | "16:9";
  focusKeyframes: FocusKeyframe[];
  sourceWidth: number;
  sourceHeight: number;
  captionStyle: CaptionStyle;
  captionPosition: CaptionPosition;
  transition: "none" | "fade";
  transitionDurationSeconds: number;
  brollOverlays: BrollOverlay[];
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

const CaptionOverlay: React.FC<{
  captions: Caption[];
  style: CaptionStyle;
  position: CaptionPosition;
}> = ({ captions, style, position }) => {
  const frame = useCurrentFrame();
  const { fps, height } = useVideoConfig();
  const seconds = frame / fps;
  const activeIndex = captions.findIndex((caption) => seconds >= caption.start && seconds < caption.end);
  if (activeIndex < 0) return null;

  const groups: Caption[][] = [];
  for (const caption of captions) {
    const current = groups[groups.length - 1];
    const previous = current?.[current.length - 1];
    const gap = previous ? caption.start - previous.end : 0;
    const punctuationBreak = previous && /[.!?]$/.test(previous.text.trim());
    if (!current || current.length >= 4 || gap > 0.48 || punctuationBreak) groups.push([caption]);
    else current.push(caption);
  }
  const activeWord = captions[activeIndex];
  const activeGroup = groups.find((group) => group.includes(activeWord)) || [activeWord];
  const progress = Math.max(0, Math.min(1, (seconds - activeWord.start) / Math.max(0.04, activeWord.end - activeWord.start)));
  const pop = 1 + Math.sin(Math.min(1, progress * 3) * Math.PI) * 0.09;
  const isTop = position === "top";
  const isBottom = position === "bottom";
  const y = isTop ? Math.round(height * 0.12) : Math.round(height * 0.5);
  const palette = style === "karaoke"
    ? { active: "#FDE047", inactive: "#FFFFFF", background: "rgba(0,0,0,0.58)" }
    : style === "bold"
      ? { active: "#FFFFFF", inactive: "#FFFFFF", background: "rgba(0,0,0,0.68)" }
      : { active: "#FDE047", inactive: "#FFFFFF", background: "transparent" };

  return (
    <div style={{
      position: "absolute", left: "6%", right: "6%",
      top: isTop || position === "center" ? y : undefined,
      bottom: isBottom ? Math.round(height * 0.12) : undefined,
      transform: position === "center" ? "translateY(-50%)" : undefined,
      display: "flex", justifyContent: "center", flexWrap: "wrap",
      gap: "0.22em", textAlign: "center",
      fontFamily: "Arial, sans-serif", fontSize: "clamp(30px, 5.4vw, 68px)",
      fontWeight: 900, lineHeight: 1.16,
      letterSpacing: style === "minimal" ? "0.01em" : "-0.025em",
      color: "#fff", textShadow: "0 3px 14px rgba(0,0,0,0.95)",
      background: palette.background, borderRadius: style === "minimal" ? 0 : 16,
      padding: style === "minimal" ? "8px 4px" : "14px 20px",
    }}>
      {activeGroup.map((word, index) => {
        const isActive = word === activeWord;
        return <span key={word.start + "-" + index} style={{
          color: isActive ? palette.active : palette.inactive,
          display: "inline-block",
          transform: isActive ? `scale(${pop})` : "scale(1)",
          WebkitTextStroke: style === "minimal" ? "0.5px rgba(0,0,0,0.8)" : undefined,
        }}>{word.text}</span>;
      })}
    </div>
  );
};


const BrollOverlayLayer: React.FC<{ overlays: BrollOverlay[] }> = ({ overlays }) => {
  const { fps, durationInFrames } = useVideoConfig();
  return (
    <>
      {overlays.map((overlay, index) => {
        const from = Math.max(0, Math.round(overlay.startSeconds * fps));
        const durationInFramesForOverlay = Math.max(1, Math.min(
          Math.round(overlay.durationSeconds * fps),
          durationInFrames - from,
        ));
        if (durationInFramesForOverlay <= 0) return null;
        return (
          <Sequence key={overlay.src + "-" + index} from={from} durationInFrames={durationInFramesForOverlay}>
            <OffthreadVideo
              src={staticFile(overlay.src)}
              volume={0}
              style={{ width: "100%", height: "100%", objectFit: "cover", objectPosition: "50% 50%" }}
            />
          </Sequence>
        );
      })}
    </>
  );
};

const ClipForgeComposition: React.FC<ClipForgeProps> = ({
  captions, focusKeyframes, sourceWidth, sourceHeight, captionStyle, captionPosition,
  transition, transitionDurationSeconds, brollOverlays,
}) => {
  const frame = useCurrentFrame();
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const focus = focusAt(focusKeyframes, frame / fps);
  const objectPosition = cropPosition(focus, sourceWidth, sourceHeight, width, height);
  const fadeFrames = transition === "fade" ? Math.max(1, Math.round(transitionDurationSeconds * fps)) : 0;
  const fadeOutStart = Math.max(fadeFrames + 1, durationInFrames - fadeFrames);
  const fadeOpacity = (currentFrame: number) => fadeFrames > 0
    ? interpolate(currentFrame, [0, fadeFrames, fadeOutStart, durationInFrames - 1], [0, 1, 1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 1;

  return (
    <AbsoluteFill style={{ backgroundColor: "#080b12", opacity: fadeOpacity(frame) }}>
      <OffthreadVideo
        src={staticFile("clip.mp4")}
        volume={fadeOpacity}
        style={{ width: "100%", height: "100%", objectFit: "cover", objectPosition }}
      />
      <BrollOverlayLayer overlays={brollOverlays || []} />
      <CaptionOverlay captions={captions} style={captionStyle} position={captionPosition} />
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
    defaultProps={{ durationSeconds: 5, captions: [], aspectRatio: "9:16", focusKeyframes: [], sourceWidth: 0, sourceHeight: 0, captionStyle: "karaoke", captionPosition: "bottom", transition: "none", transitionDurationSeconds: 0.5, brollOverlays: [] }}
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
