'use client';

import React, { useState } from 'react';

export default function VoiceProcessingPage() {
  const [isRecording, setIsRecording] = useState(false);

  return (
    <div className="space-y-6">
      <div className="sp-page-head">
        <div>
          <h2 className="sp-page-title">Voice Command Center</h2>
          <p className="sp-page-sub">
            Test the Whisper API Speech-to-Text (STT) pipeline directly from the dashboard.
          </p>
        </div>
      </div>

      <section className="sp-card sp-voice-card">
        <button
          onMouseDown={() => setIsRecording(true)}
          onMouseUp={() => setIsRecording(false)}
          onMouseLeave={() => setIsRecording(false)}
          className={`sp-voice-btn sara-focus-ring ${isRecording ? 'sp-voice-rec' : ''}`}
          aria-pressed={isRecording}
          aria-label={isRecording ? 'Stop recording' : 'Hold to record'}
        >
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <rect x="9" y="3" width="6" height="11" rx="3" />
            <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
          </svg>
        </button>
        <p className="sp-voice-state">
          {isRecording ? 'Listening…' : 'Hold to Record'}
        </p>
      </section>

      <section className="sp-card">
        <h3 className="sp-card-title">Recent Transcriptions</h3>
        <p className="sp-card-note">Latest results from the STT pipeline.</p>
        <div className="mt-4">
          <div className="sp-transcript">
            <div className="sp-transcript-head">
              <span className="sara-mono sp-transcript-id">ID: vn_88291a</span>
              <span className="sara-status sara-status-ok sara-pulse">
                <span className="sara-status-dot" aria-hidden="true" />
                Processed
              </span>
            </div>
            <p className="sp-transcript-text">
              &ldquo;I need to book a Deluxe room for tomorrow.&rdquo;
            </p>
            <p className="sp-transcript-meta">↳ Mapped to text payload successfully</p>
          </div>
        </div>
      </section>
    </div>
  );
}
