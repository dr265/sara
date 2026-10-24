'use client';

import React, { useState, useEffect } from 'react';
import { LEGACY_API_URL } from '@/lib/api';

interface KbDoc {
  filename: string;
  route?: string;
  status: string;
  [key: string]: unknown;
}

export default function KnowledgePage() {
  const [docs, setDocs] = useState<KbDoc[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [token, setToken] = useState<string | null>(null);

  useEffect(() => {
    setToken(localStorage.getItem('sara_tg_token'));
  }, []);

  const fetchDocuments = async () => {
    if (!token) return;
    try {
      const res = await fetch(`${LEGACY_API_URL}/agent/ingest/documents`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json();
      if (Array.isArray(data.documents)) setDocs(data.documents);
    } catch (error) {
      console.error(error);
    }
  };

  useEffect(() => {
    fetchDocuments();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files || !e.target.files[0] || !token) return;
    setIsUploading(true);
    const file = e.target.files[0];
    const reader = new FileReader();

    reader.onload = async (event) => {
      const base64Data = (event.target?.result as string).split(',')[1];
      try {
        const res = await fetch(`${LEGACY_API_URL}/agent/ingest/upload`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: file.name, mime: file.type, content_base64: base64Data }),
        });
        if (res.ok) fetchDocuments();
      } finally {
        setIsUploading(false);
      }
    };
    reader.readAsDataURL(file);
  };

  return (
    <div className="space-y-6">
      <div className="sp-page-head">
        <div>
          <h2 className="sp-page-title">Knowledge / Files</h2>
          <p className="sp-page-sub">
            Files ingested, vectorized, and indexed for retrieval during live chats.
          </p>
        </div>
      </div>

      {/* Upload dropzone */}
      <section
        className="sp-card sp-dropzone"
        aria-label="Upload knowledge files"
      >
        <input
          type="file"
          accept=".csv,.pdf,.txt,.json"
          onChange={handleFileUpload}
          disabled={isUploading}
          className="sp-dropzone-input"
          aria-label="Choose a knowledge file"
        />
        <div className="sp-dropzone-inner">
          <div className="sp-dropzone-mark" aria-hidden="true">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 3v13M7 10l5-5 5 5" />
              <path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
            </svg>
          </div>
          <h3 className="sp-card-title">Drag &amp; Drop Knowledge Files</h3>
          <p className="sp-card-note">
            Support for PDF, CSV, TXT, and JSON. Files are automatically vectorized.
          </p>
          {isUploading && (
            <p className="sp-dropzone-busy">
              <span className="sara-dots" aria-hidden="true">
                <span /><span /><span />
              </span>
              Extracting and vectorizing…
            </p>
          )}
        </div>
      </section>

      {/* Active knowledge base */}
      <section className="sp-card">
        <h3 className="sp-card-title">Active Knowledge Base</h3>
        <p className="sp-card-note">
          {docs.length} indexed document{docs.length === 1 ? '' : 's'}.
        </p>
        <div className="mt-4 sp-table-wrap">
          <table className="sp-table">
            <thead>
              <tr>
                <th>Filename</th>
                <th>Type</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {docs.length === 0 ? (
                <tr>
                  <td colSpan={3} style={{ color: 'var(--sara-dim)' }}>
                    No indexed documents yet.
                  </td>
                </tr>
              ) : (
                docs.map((doc, idx) => (
                  <tr key={idx}>
                    <td>{doc.filename}</td>
                    <td>{doc.route || 'General'}</td>
                    <td>
                      <span className="sara-status sara-status-ok">
                        <span className="sara-status-dot" aria-hidden="true" />
                        {doc.status.toUpperCase()}
                      </span>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
