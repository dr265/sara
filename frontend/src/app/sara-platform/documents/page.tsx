'use client';

import React from 'react';

interface DocRow {
  id: string;
  type: string;
  guest: string;
  date: string;
  status: string;
}

const DOCUMENTS: DocRow[] = [
  { id: 'DOC-9921', type: 'Staff Briefing', guest: 'John Doe', date: '2026-09-01', status: 'Ready' },
  { id: 'DOC-9920', type: 'Booking Confirmation', guest: 'Jane Smith', date: '2026-08-30', status: 'Ready' },
];

export default function GeneratedDocumentsPage() {
  return (
    <div className="space-y-6">
      <div className="sp-page-head">
        <div>
          <h2 className="sp-page-title">Generated Documents</h2>
          <p className="sp-page-sub">
            View and download automated staff briefings and guest PDFs.
          </p>
        </div>
      </div>

      <section className="sp-card">
        <div className="sp-table-wrap">
          <table className="sp-table">
            <thead>
              <tr>
                <th>Document ID</th>
                <th>Type</th>
                <th>Associated Guest</th>
                <th>Generated On</th>
                <th style={{ textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {DOCUMENTS.map((d) => (
                <tr key={d.id}>
                  <td className="sara-mono">{d.id}</td>
                  <td>{d.type}</td>
                  <td>{d.guest}</td>
                  <td>{d.date}</td>
                  <td style={{ textAlign: 'right' }}>
                    <span className="sara-status sara-status-ok">
                      <span className="sara-status-dot" aria-hidden="true" />
                      {d.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
