'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { PLATFORM_CSS } from './platform.css';
import "@/styles/sara.css"; // shared SARA design tokens + utilities

const NAVIGATION_ITEMS = [
  { name: 'Dashboard', href: '/sara-platform', icon: 'grid' },
  { name: 'AI Agent', href: '/sara-platform/agent', icon: 'bot' },
  { name: 'Conversations', href: '/sara-platform/conversations', icon: 'chat' },
  { name: 'Leads & Bookings', href: '/sara-platform/bookings', icon: 'cal' },
  { name: 'Knowledge / Files', href: '/sara-platform/knowledge', icon: 'folder' },
  { name: 'Prompts & Rules', href: '/sara-platform/prompts', icon: 'sliders' },
  { name: 'Google Sheets', href: '/sara-platform/sheets', icon: 'sheet' },
  { name: 'Telegram Connection', href: '/sara-platform/telegram', icon: 'send' },
  { name: 'Generated Documents', href: '/sara-platform/documents', icon: 'doc' },
  { name: 'Voice', href: '/sara-platform/voice', icon: 'mic' },
  { name: 'Activity Logs', href: '/sara-platform/logs', icon: 'shield' },
  { name: 'Settings', href: '/sara-platform/settings', icon: 'wrench' },
  { name: 'Help', href: '/sara-platform/help', icon: 'help' },
] as const;

function NavIcon({ name, size = 17 }: { name: string; size?: number }) {
  const p = {
    fill: 'none', stroke: 'currentColor', strokeWidth: 1.7,
    strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
  };
  switch (name) {
    case 'grid': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /></svg>;
    case 'bot': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><rect x="4" y="8" width="16" height="11" rx="3" /><path d="M12 8V4M8 4h8" /><circle cx="9" cy="13" r="1" /><circle cx="15" cy="13" r="1" /><path d="M2 13v3M22 13v3" /></svg>;
    case 'chat': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><path d="M21 12a8 8 0 0 1-11.4 7.2L4 21l1.8-5.6A8 8 0 1 1 21 12Z" /></svg>;
    case 'cal': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><rect x="3" y="5" width="18" height="16" rx="2.5" /><path d="M8 3v4M16 3v4M3 10h18" /></svg>;
    case 'folder': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><path d="M4 7a2 2 0 0 1 2-2h4l2 2.5h6a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z" /></svg>;
    case 'sliders': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><path d="M4 7h10M18 7h2M4 17h4M12 17h8" /><circle cx="16" cy="7" r="2" /><circle cx="10" cy="17" r="2" /></svg>;
    case 'sheet': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M4 9h16M4 15h16M10 3v18" /></svg>;
    case 'send': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><path d="M21 3 3 10.5l7 3 3 7Z" /><path d="m10 13.5 4-4" /></svg>;
    case 'doc': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" /><path d="M14 3v5h5M9 13h6M9 17h4" /></svg>;
    case 'mic': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>;
    case 'shield': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><path d="M12 3l8 3v6c0 4.5-3.2 7.7-8 9-4.8-1.3-8-4.5-8-9V6Z" /><path d="m9.5 12 1.8 1.8 3.4-3.6" /></svg>;
    case 'wrench': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><path d="M15.5 3.5a5 5 0 0 0-4 8l-8 8 2 2 8-8a5 5 0 0 0 6.5-6.5l-3 3-2.5-.5L13 6.5Z" /></svg>;
    case 'help': return <svg width={size} height={size} viewBox="0 0 24 24" {...p}><circle cx="12" cy="12" r="9" /><path d="M9.5 9.5a2.5 2.5 0 1 1 3.4 2.3c-.6.3-.9.8-.9 1.4v.3" /><path d="M12 17.5h.01" /></svg>;
    default: return null;
  }
}

export default function SaraPlatformLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [sidebarOpen, setSidebarOpen] = useState(true);

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: PLATFORM_CSS }} />
      <div className="sp-root sara-grain">
        {/* AMBIENT FIELD */}
        <div className="sp-field" aria-hidden="true" />

        {/* PERSISTENT LEFT SIDEBAR */}
        <aside className={`sp-sidebar ${sidebarOpen ? 'sp-sidebar-open' : 'sp-sidebar-rail'}`}>
          <div className="sp-sidebar-head">
            <Link href="/" className="sp-brand sara-focus-ring">
              <span className="sp-brand-mark">
                <span className="sp-brand-dot" />
              </span>
              {sidebarOpen && <span className="sp-brand-text">SARA AI</span>}
            </Link>
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="sp-collapse sara-focus-ring"
              aria-label={sidebarOpen ? 'Collapse sidebar' : 'Expand sidebar'}
              aria-expanded={sidebarOpen}
            >
              {sidebarOpen ? '◀' : '▶'}
            </button>
          </div>

          <nav className="sara-scroll sp-nav" aria-label="Platform navigation">
            {NAVIGATION_ITEMS.map((item) => {
              const isActive = pathname === item.href;
              return (
                <Link
                  key={item.name}
                  href={item.href}
                  className={`sp-nav-item sara-focus-ring ${isActive ? 'sp-nav-active' : ''}`}
                  title={item.name}
                >
                  <span className="sp-nav-icon" aria-hidden="true">
                    <NavIcon name={item.icon} />
                  </span>
                  {sidebarOpen && <span className="sp-nav-label">{item.name}</span>}
                </Link>
              );
            })}
          </nav>

          <div className="sp-sidebar-foot">
            <span className="sp-status-dot sara-pulse" aria-hidden="true" />
            {sidebarOpen && (
              <span className="sp-status-text">Gateway Online</span>
            )}
          </div>
        </aside>

        {/* MAIN CONTENT AREA */}
        <div className="sp-main">
          <header className="sp-header">
            <div className="sp-header-brand">
              <span className="sp-header-tag">Enterprise Operations Platform</span>
            </div>
            <div className="sp-header-meta">
              <span>Workspace:</span>
              <strong>Active Tenant</strong>
            </div>
          </header>

          <main className="sara-scroll sp-content">
            {children}
          </main>
        </div>
      </div>
    </>
  );
}
