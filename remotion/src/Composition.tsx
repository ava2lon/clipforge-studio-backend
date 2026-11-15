import React from "react";
import {Composition} from "remotion";

const ClipForgeComposition: React.FC = () => (
  <div style={{flex: 1, backgroundColor: "#080b12", color: "white", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "Arial, sans-serif"}}>
    <div style={{fontSize: 64, fontWeight: 800}}>ClipForge</div>
  </div>
);

export const RemotionRoot: React.FC = () => (
  <Composition
    id="ClipForge"
    component={ClipForgeComposition}
    durationInFrames={150}
    fps={30}
    width={1080}
    height={1920}
  />
);
