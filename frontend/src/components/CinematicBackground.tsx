"use client";

import React, { useEffect, useRef } from "react";

export default function CinematicBackground() {
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    // Handle reduced motion for video
    const q = window.matchMedia && window.matchMedia('(prefers-reduced-motion:reduce)');
    const v = videoRef.current;
    if (q && v) {
      const sync = () => {
        if (q.matches) {
          v.pause();
        } else {
          const p = v.play();
          if (p) p.catch(() => {});
        }
      };
      sync();
      if (q.addEventListener) {
        q.addEventListener('change', sync);
      } else {
        q.addListener(sync);
      }
    }
  }, []);

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: `
        body { background: transparent !important; }
        .art { position:fixed; inset:0; width:100%; height:100%; object-fit:cover; object-position:50% 50%; z-index:-5; user-select:none; -webkit-user-drag:none; pointer-events:none; background:#03060c; }
        .veil { position:fixed; inset:0; z-index:-4; pointer-events:none; background: radial-gradient(140% 60% at 50% 40%, rgba(6,10,18,.16) 0%, rgba(6,10,18,.057) 50%, rgba(6,10,18,0) 100%), linear-gradient(180deg, rgba(6,10,18,0) 45%, rgba(6,10,18,.10) 100%); }
      `}} />
      <video 
        ref={videoRef}
        className="art" 
        autoPlay 
        muted 
        loop 
        playsInline 
        preload="auto"
        aria-hidden="true" 
        poster="https://d2ol7oe51mr4n9.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/130837c4-0244-4f37-9c61-8d801d93fd29.jpg" 
        src="https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260912_104303_0c6d60b2-9353-408e-9449-585108a22fb5.mp4"
      />
      <div className="veil"></div>
    </>
  );
}
