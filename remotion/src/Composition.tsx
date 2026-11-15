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
type ClipForgeProps = { durationSeconds: number; captions: Caption[] };

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

const ClipForgeComposition: React.FC<ClipForgeProps> = ({ captions }) => (
  <AbsoluteFill style={{ backgroundColor: "#080b12" }}>
    <OffthreadVideo
      src={staticFile("clip.mp4")}
      style={{ width: "100%", height: "100%", objectFit: "cover" }}
    />
    <CaptionOverlay captions={captions} />
  </AbsoluteFill>
);

export const RemotionRoot: React.FC = () => (
  <Composition<ClipForgeProps>
    id="ClipForge"
    component={ClipForgeComposition}
    durationInFrames={150}
    fps={30}
    width={1080}
    height={1920}
    defaultProps={{ durationSeconds: 5, captions: [] }}
    calculateMetadata={({ props }) => ({
      durationInFrames: Math.max(1, Math.ceil(props.durationSeconds * 30)),
    })}
  />
);
