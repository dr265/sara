import { NextResponse } from 'next/server';

export async function POST(request: Request) {
  try {
    // 1. Extract the prompt from the frontend UI
    const body = await request.json();
    const { prompt } = body;

    if (!prompt) {
      return NextResponse.json({ error: 'Prompt is required' }, { status: 400 });
    }

    // 2. Forward the prompt to your Python FastAPI backend
    // Ensure this matches the port uvicorn is running on (default 8000)
    // (override with SARA_LEGACY_BACKEND_URL in frontend/.env.local if needed)
    const pythonBackendUrl = `${process.env.SARA_LEGACY_BACKEND_URL ?? 'http://127.0.0.1:8000'}/api/v1/orchestrate`;
    
    const response = await fetch(pythonBackendUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ prompt }),
    });

    if (!response.ok) {
      throw new Error(`Backend responded with status: ${response.status}`);
    }

    // 3. Send SARA's decision back to the frontend
    const data = await response.json();
    return NextResponse.json(data);

  } catch (error) {
    console.error('Orchestration Error:', error);
    return NextResponse.json(
      { error: 'Failed to communicate with SARA engine' }, 
      { status: 500 }
    );
  }
}