"use client";

import { useState, useRef } from "react";
import { SARA_API_URL, saraFetch } from "@/lib/api";
import "../sara-orb/sara-orb.css"; 

export default function AgentCreator() {
  const [isDeploying, setIsDeploying] = useState(false);
  const [log, setLog] = useState("SYSTEM READY. AWAITING BUSINESS DATA.");
  const fileRef = useRef<HTMLInputElement>(null);
  const [fileName, setFileName] = useState("");

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setFileName(e.target.files[0].name);
    }
  };

  const handleDeploy = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setIsDeploying(true);
    setLog("EXTRACTING KNOWLEDGE BASE AND DEPLOYING AGENT...");

    const formData = new FormData(e.currentTarget);
    formData.append("owner_id", "user_123"); 

    try {
      const res = await saraFetch(`${SARA_API_URL}/api/business/setup`, {
        method: "POST",
        body: formData, 
      });

      const data = await res.json();
      if (data.status === "success") {
        setLog(`SUCCESS: Agent deployed. Absorbed ${data.knowledge_bytes} bytes of data into memory.`);
      } else {
        setLog(`ERROR: ${data.detail || data.message}`);
      }
    } catch (err) {
      setLog("CRITICAL ERROR: Failed to connect to FastAPI backend.");
    } finally {
      setIsDeploying(false);
    }
  };

  return (
    <div className="sara-orb-viewport" style={{ padding: "40px", overflowY: "auto", display: "flex", justifyContent: "center", alignItems: "center" }}>
      <div className="overlay-vignette" />
      <div className="overlay-scanlines" />

      <div style={{ position: "relative", zIndex: 10, width: "100%", maxWidth: "600px", background: "rgba(4, 5, 12, 0.9)", padding: "32px", borderRadius: "12px", border: "1px solid rgba(232, 121, 249, 0.4)", boxShadow: "0 0 20px rgba(232, 121, 249, 0.1)" }}>
        <h1 style={{ color: "#e879f9", marginBottom: "24px", fontFamily: "monospace", fontSize: "1.5rem", textAlign: "center" }}>
          UNIVERSAL AGENT DEPLOYMENT
        </h1>

        <form onSubmit={handleDeploy} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
          <input name="business_name" required placeholder="Business Name (e.g. Royal Clinic)" className="command-input" style={{ border: "1px solid rgba(56, 189, 248, 0.5)", padding: "12px", borderRadius: "6px" }} />

          <select name="business_type" className="command-input" style={{ border: "1px solid rgba(56, 189, 248, 0.5)", padding: "12px", borderRadius: "6px", background: "transparent" }}>
            <option value="clinic" style={{color:"black"}}>Clinic / Hospital</option>
            <option value="hotel" style={{color:"black"}}>Hotel / Hospitality</option>
            <option value="salon" style={{color:"black"}}>Salon / Spa</option>
            <option value="gym" style={{color:"black"}}>Gym / Fitness</option>
          </select>

          <textarea name="business_rules" required placeholder="Business Rules & Policies (e.g. 50% advance required, open Mon-Sat 9-5, cancel within 24h)" className="command-input" style={{ border: "1px solid rgba(56, 189, 248, 0.5)", padding: "12px", borderRadius: "6px", minHeight: "100px", resize: "vertical" }} />

          <div style={{ border: "1px dashed rgba(232, 121, 249, 0.6)", padding: "24px", textAlign: "center", cursor: "pointer", borderRadius: "6px", transition: "all 0.2s" }} onClick={() => fileRef.current?.click()}>
            <span style={{ color: fileName ? "#e879f9" : "rgba(255, 255, 255, 0.5)" }}>
              {fileName ? `📄 ${fileName} ATTACHED` : "+ CLICK TO UPLOAD KNOWLEDGE BASE (PDF, CSV, XLSX)"}
            </span>
            <input type="file" name="file" ref={fileRef} onChange={handleFileChange} style={{ display: "none" }} accept=".pdf,.csv,.xlsx,.xls" />
          </div>

          <button type="submit" disabled={isDeploying} className="command-btn" style={{ padding: "16px", marginTop: "12px", fontSize: "1.1rem" }}>
            {isDeploying ? "INITIALIZING NEURAL NET..." : "DEPLOY AI AGENT"}
          </button>
        </form>

        <div className="terminal-log info" style={{ marginTop: "24px", borderLeft: "2px solid #38bdf8", padding: "12px", background: "rgba(0,0,0,0.6)" }}>
          &gt; {log}
        </div>
      </div>
    </div>
  );
}