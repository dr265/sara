"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowUpRight, Command, Search, X } from "lucide-react";
import { DESTINATIONS, type Destination } from "@/lib/navigation";

const OPEN_EVENT = "sara:command";
const RECENTS_KEY = "sara:recent-destinations";

export function CommandTrigger({ compact = false }: { compact?: boolean }) {
  return <button type="button" className="sara-command-trigger" onClick={() => window.dispatchEvent(new Event(OPEN_EVENT))} aria-label="Search SARA, Control or Command K" aria-haspopup="dialog">
    <Search size={16} aria-hidden="true" /><span>{compact ? "Search" : "Search SARA"}</span><kbd>⌘ K</kbd>
  </button>;
}

export default function CommandCenter() {
  const router = useRouter();
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [recent, setRecent] = useState<string[]>([]);
  const search = query.trim().toLowerCase();
  const results = search
    ? DESTINATIONS.filter(item => `${item.label} ${item.group} ${item.description}`.toLowerCase().includes(search))
    : [...recent.map(href => DESTINATIONS.find(item => item.href === href)).filter((item): item is Destination => !!item), ...DESTINATIONS.filter(item => !recent.includes(item.href))].slice(0, 9);

  useEffect(() => {
    function open() {
      if (dialog.current?.open) return;
      previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setQuery("");
      setActive(0);
      try {
        const value: unknown = JSON.parse(localStorage.getItem(RECENTS_KEY) || "[]");
        setRecent(Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").slice(0, 5) : []);
      } catch { setRecent([]); }
      dialog.current?.showModal();
      input.current?.focus();
    }
    function shortcut(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (dialog.current?.open) dialog.current.close(); else open();
      }
    }
    window.addEventListener(OPEN_EVENT, open);
    window.addEventListener("keydown", shortcut);
    return () => { window.removeEventListener(OPEN_EVENT, open); window.removeEventListener("keydown", shortcut); };
  }, []);

  useEffect(() => {
    if (dialog.current?.open) document.getElementById(`sara-result-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  function navigate(item: Destination) {
    try { localStorage.setItem(RECENTS_KEY, JSON.stringify([item.href, ...recent.filter(href => href !== item.href)].slice(0, 5))); } catch { /* Storage is optional. */ }
    dialog.current?.close();
    router.push(item.href);
  }

  return <dialog ref={dialog} className="sara-command" aria-labelledby="sara-command-title" onClose={() => previousFocus.current?.focus()} onClick={event => { if (event.target === dialog.current) dialog.current.close(); }}>
    <div className="sara-command-inner">
      <div className="sara-command-heading"><span id="sara-command-title"><Command size={15} aria-hidden="true" /> SARA command center</span><button type="button" onClick={() => dialog.current?.close()} aria-label="Close command center"><X size={18} /></button></div>
      <div className="sara-command-search"><Search size={21} aria-hidden="true" /><input ref={input} value={query} onChange={event => { setQuery(event.target.value); setActive(0); }} placeholder="Where would you like to go?" role="combobox" aria-label="Search agents and workspaces" aria-autocomplete="list" aria-expanded="true" aria-controls="sara-command-results" aria-activedescendant={results[active] ? `sara-result-${active}` : undefined} onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          setActive(index => results.length ? (index + (event.key === "ArrowDown" ? 1 : -1) + results.length) % results.length : 0);
        } else if (event.key === "Enter" && results[active]) { event.preventDefault(); navigate(results[active]); }
      }} /></div>
      <p className="sara-command-caption">{search ? `${results.length} destinations` : recent.length ? "Recent destinations & suggestions" : "Explore your workspace"}</p>
      <div id="sara-command-results" role="listbox" aria-label="Destinations" className="sara-command-results sara-scroll">
        {results.map((item, index) => <div id={`sara-result-${index}`} key={item.href} role="option" aria-selected={index === active} className="sara-command-result" onMouseMove={() => setActive(index)} onClick={() => navigate(item)}>
          <span className="sara-command-result-icon"><ArrowUpRight size={18} aria-hidden="true" /></span><span><strong>{item.label}</strong><small>{item.description}</small></span><span className="sara-command-group">{item.group}</span>
        </div>)}
      </div>
      {!results.length && <div className="sara-command-empty" role="status">No matching destinations.<br /><span>Try an agent name, knowledge, or settings.</span></div>}
      <footer><span><kbd>↑</kbd> <kbd>↓</kbd> to navigate</span><span><kbd>Enter</kbd> to open · <kbd>Esc</kbd> to close</span></footer>
    </div>
  </dialog>;
}
