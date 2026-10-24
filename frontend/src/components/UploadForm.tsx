"use client";

import { useState } from "react";
import { SARA_API_URL, saraFetch } from "@/lib/api";

export default function UploadForm({ userId }: { userId: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState<"idle" | "uploading" | "success" | "error">("idle");
  const [message, setMessage] = useState("");

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setFile(e.target.files[0]);
      setStatus("idle");
      setMessage("");
    }
  };

  const handleUpload = async () => {
    if (!file) return;

    setStatus("uploading");
    setMessage("Vectorizing document...");

    const formData = new FormData();
    formData.append("file", file);
    formData.append("user_id", userId);

    try {
      const res = await saraFetch(`${SARA_API_URL}/api/documents/upload`, {
        method: "POST",
        body: formData,
      });

      if (res.ok) {
        setStatus("success");
        setMessage("Document saved to AI memory.");
        setFile(null); // Reset the file so they can upload another
      } else {
        setStatus("error");
        setMessage("Failed to process document.");
      }
    } catch (error) {
      setStatus("error");
      setMessage("Server connection failed.");
    }
  };

  return (
    <div className="w-full flex flex-col gap-4 font-sans">
      
      {/* Sleek Drag & Drop Zone */}
      <label className="flex flex-col items-center justify-center w-full h-36 border-2 border-dashed border-gray-300 rounded-2xl cursor-pointer bg-gray-50/50 hover:bg-gray-100 transition-colors">
        <div className="flex flex-col items-center justify-center pt-5 pb-6">
          {file ? (
            <div className="text-center px-4">
              <svg className="w-8 h-8 mb-2 mx-auto text-purple-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                 <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
              <p className="text-sm font-bold text-gray-800 truncate max-w-[200px]">{file.name}</p>
              <p className="text-xs text-gray-500 font-medium mt-1">{(file.size / 1024 / 1024).toFixed(2)} MB</p>
            </div>
          ) : (
            <>
              <svg className="w-8 h-8 mb-3 text-gray-400" fill="none" viewBox="0 0 20 16">
                <path stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 13h3a3 3 0 0 0 0-6h-.025A5.56 5.56 0 0 0 16 6.5 5.5 5.5 0 0 0 5.207 5.021C5.137 5.017 5.071 5 5 5a4 4 0 0 0 0 8h2.167M10 15V6m0 0L8 8m2-2 2 2"/>
              </svg>
              <p className="mb-1 text-sm text-gray-500"><span className="font-bold text-gray-900">Click to upload</span> or drag & drop</p>
              <p className="text-[11px] text-gray-400 font-medium">PDF, XLSX, MP4, MP3 or Images</p>
            </>
          )}
        </div>
        
        {/* Hidden File Input */}
        <input 
          type="file" 
          className="hidden" 
          onChange={handleFileChange}
          accept=".txt,.pdf,.doc,.docx,.png,.jpg,.jpeg,.mp3,.mp4,.wav,.csv,.xlsx,.pptx"
        />
      </label>

      {/* Dynamic Action Button */}
      {file && status !== "uploading" && status !== "success" && (
        <button
          onClick={handleUpload}
          className="w-full bg-black text-white font-bold py-3 rounded-xl hover:bg-gray-800 transition-all shadow-md"
        >
          Process Document
        </button>
      )}

      {/* Loading State */}
      {status === "uploading" && (
        <div className="flex items-center justify-center gap-3 text-sm font-bold text-gray-700 py-3 bg-gray-50 rounded-xl border border-gray-100">
          <svg className="animate-spin h-5 w-5 text-purple-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          {message}
        </div>
      )}

      {/* Success State */}
      {status === "success" && (
        <div className="flex items-center justify-center gap-2 text-sm font-bold text-green-700 bg-green-50 py-3 rounded-xl border border-green-200">
          <span>✅</span> {message}
        </div>
      )}

      {/* Error State */}
      {status === "error" && (
        <div className="flex items-center justify-center gap-2 text-sm font-bold text-red-700 bg-red-50 py-3 rounded-xl border border-red-200">
          <span>❌</span> {message}
        </div>
      )}
    </div>
  );
}