'use client';

import React, { useState, useEffect } from 'react';
import { LEGACY_API_URL } from '@/lib/api';

interface Booking {
  id: string;
  data: {
    full_name?: string;
    room_type?: string;
    check_in?: string;
    check_out?: string;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

export default function BookingsPage() {
  const [bookings, setBookings] = useState<Booking[]>([]);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const token = localStorage.getItem('sara_tg_token');
    if (!token) {
      setLoading(false);
      return;
    }
    fetch(`${LEGACY_API_URL}/agent/bookings`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => res.json())
      .then((data) => {
        if (cancelled) return;
        if (Array.isArray(data.bookings)) setBookings(data.bookings);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const filteredBookings = bookings.filter(
    (b) =>
      b.data?.full_name?.toLowerCase().includes(search.toLowerCase()) ||
      b.data?.room_type?.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <div className="space-y-6">
      <div className="sp-page-head">
        <div>
          <h2 className="sp-page-title">Leads &amp; Bookings</h2>
          <p className="sp-page-sub">
            Every lead captured by the conversational agents, with room
            preference and stay window.
          </p>
        </div>
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search guest or room…"
          className="sara-input"
          style={{ maxWidth: '16rem' }}
          aria-label="Search bookings"
        />
      </div>

      <section className="sp-card">
        {loading ? (
          <p className="sp-card-note">Loading bookings…</p>
        ) : filteredBookings.length === 0 ? (
          <p className="sp-card-note">
            {bookings.length === 0
              ? 'No bookings captured yet. Leads arrive through the Telegram and WhatsApp agents.'
              : 'No bookings match your search.'}
          </p>
        ) : (
          <div className="sp-table-wrap">
            <table className="sp-table">
              <thead>
                <tr>
                  <th>Guest</th>
                  <th>Room</th>
                  <th>Check-in</th>
                  <th>Check-out</th>
                </tr>
              </thead>
              <tbody>
                {filteredBookings.map((b) => (
                  <tr key={b.id}>
                    <td>{b.data?.full_name ?? '—'}</td>
                    <td>{b.data?.room_type ?? '—'}</td>
                    <td>{b.data?.check_in ?? '—'}</td>
                    <td>{b.data?.check_out ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
