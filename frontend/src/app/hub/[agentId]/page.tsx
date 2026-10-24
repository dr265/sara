'use client';

import React, { useState, useEffect, useRef } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { AGENT_REGISTRY } from '@/lib/agents.config';
import ReactMarkdown from 'react-markdown';
import { AGENT_WS_CSS } from './agent-ws.css';

export default function AgentWorkspacePage() {
  const params = useParams();
  const router = useRouter();

  const [input, setInput] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const agentId = params.agentId as string;
  const agent = AGENT_REGISTRY.find(a => a.route === `/hub/${agentId}`);

  const [messages, setMessages] = useState([
    {
      role: 'agent',
      content: agent ? `${agent.name} initialized. Secure connection established. How can I assist you with ${agent.category} today?` : ''
    }
  ]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isTyping]);

  if (!agent) {
    return (
      <>
        <style dangerouslySetInnerHTML={{ __html: AGENT_WS_CSS }} />
        <div className="aws-root aws-notfound">
          <div className="aws-notfound-mark" aria-hidden="true">◈</div>
          <h1 className="aws-notfound-title">404 · NODE NOT FOUND</h1>
          <p className="aws-notfound-copy">
            The requested intelligence node does not exist in the registry.
          </p>
          <button
            onClick={() => router.push('/hub')}
            className="sara-btn sara-btn-primary sara-focus-ring"
          >
            ← Return to Core Gateway
          </button>
        </div>
      </>
    );
  }

  const handleSendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim()) return;

    const userMessage = input.trim();
    setMessages(prev => [...prev, { role: 'user', content: userMessage }]);
    setInput('');
    setIsTyping(true);

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agent_id: agent.name,
          message: userMessage
        }),
      });

      if (!response.ok) throw new Error('Network error during chat');

      const data = await response.json();

      if (data.status === 'success' && data.reply) {
        setMessages(prev => [
          ...prev,
          { role: 'agent', content: data.reply }
        ]);
      } else {
        throw new Error('Invalid response from AI');
      }

    } catch (error) {
      console.error('Failed to send message:', error);
      setMessages(prev => [
        ...prev,
        { role: 'agent', content: '[SYSTEM ERROR]: Connection to intelligence node severed. Please try again.' }
      ]);
    } finally {
      setIsTyping(false);
    }
  };

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: AGENT_WS_CSS }} />
      <div className="aws-root">
        {/* HEADER BAR */}
        <header className="aws-header">
          <div className="aws-header-left">
            <button
              onClick={() => router.push('/hub')}
              className="aws-back sara-focus-ring"
              title="Back to Hub"
              aria-label="Back to Hub"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
            </button>
            <div className="aws-header-divide" aria-hidden="true" />
            <div className="aws-header-id">
              <div className="aws-header-name">
                <h1>{agent.name}</h1>
                <span
                  className={`sara-status ${agent.status === 'online' ? 'sara-status-ok' : 'sara-status-warn'} ${agent.status === 'online' ? 'sara-pulse' : ''}`}
                >
                  <span className="sara-status-dot" aria-hidden="true" />
                  {agent.status}
                </span>
              </div>
              <p className="aws-header-cat">{agent.category} NODE</p>
            </div>
          </div>

          <div className="aws-caps">
            {agent.capabilities.map((cap, i) => (
              <span key={i} className="sara-chip">{cap}</span>
            ))}
          </div>
        </header>

        {/* MAIN WORKSPACE GRID */}
        <div className="aws-body">
          {/* LEFT PANEL: Chat Terminal */}
          <section className="aws-chat">
            <div className="aws-log sara-scroll">
              {messages.map((msg, idx) => (
                <div key={idx} className={`aws-msg ${msg.role === 'user' ? 'aws-msg-user' : 'aws-msg-agent'}`}>
                  <span className="aws-msg-author">
                    {msg.role === 'user' ? 'User' : agent.name}
                  </span>
                  <div className={`aws-bubble ${msg.role === 'user' ? 'aws-bubble-user' : 'aws-bubble-agent'}`}>
                    <ReactMarkdown
                      components={{
                        p: ({ node, ...props }) => <p className="aws-md-p" {...props} />,
                        strong: ({ node, ...props }) => <strong className="aws-md-strong" {...props} />,
                        ul: ({ node, ...props }) => <ul className="aws-md-ul" {...props} />,
                        ol: ({ node, ...props }) => <ol className="aws-md-ol" {...props} />,
                        li: ({ node, ...props }) => <li className="aws-md-li" {...props} />,
                        h1: ({ node, ...props }) => <h1 className="aws-md-h1" {...props} />,
                        h2: ({ node, ...props }) => <h2 className="aws-md-h2" {...props} />,
                        h3: ({ node, ...props }) => <h3 className="aws-md-h3" {...props} />,
                        code: ({ node, ...props }) => <code className="aws-md-code" {...props} />
                      }}
                    >
                      {msg.content}
                    </ReactMarkdown>
                  </div>
                </div>
              ))}

              {isTyping && (
                <div className="aws-msg aws-msg-agent">
                  <span className="aws-msg-author">{agent.name}</span>
                  <div className="aws-bubble aws-bubble-agent">
                    <span className="sara-dots" aria-label="Agent is typing">
                      <span /><span /><span />
                    </span>
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>

            {/* Input Area */}
            <div className="aws-input-bar">
              <form onSubmit={handleSendMessage} className="aws-input-form">
                <input
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder={`Command ${agent.name}…`}
                  className="sara-input aws-input-field"
                  aria-label={`Message ${agent.name}`}
                />
                <button
                  type="submit"
                  disabled={!input.trim() || isTyping}
                  className="aws-send sara-focus-ring"
                  aria-label="Send message"
                >
                  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" /></svg>
                </button>
              </form>
            </div>
          </section>

          {/* RIGHT PANEL: Execution Canvas */}
          <section className="aws-canvas" aria-label="Execution canvas">
            <div className="aws-canvas-grid" aria-hidden="true" />
            <div className="aws-canvas-fade" aria-hidden="true" />

            <div className="aws-canvas-inner">
              <div className="aws-canvas-mark" aria-hidden="true">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <line x1="3" y1="9" x2="21" y2="9" />
                  <line x1="9" y1="21" x2="9" y2="9" />
                </svg>
              </div>
              <h3 className="aws-canvas-title">Execution Canvas</h3>
              <p className="aws-canvas-copy">
                Artifacts, charts, code segments, and rendered graphics produced by{' '}
                <strong>{agent.name}</strong> will appear here during processing.
              </p>
            </div>
          </section>
        </div>
      </div>
    </>
  );
}
