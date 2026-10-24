import { NextResponse } from 'next/server';

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const { agent_id, message } = body;

    if (!agent_id || !message) {
      return NextResponse.json({ error: 'Agent ID and message are required' }, { status: 400 });
    }

    // Forward the payload to your Python FastAPI chat endpoint
    // (override with SARA_LEGACY_BACKEND_URL in frontend/.env.local if needed)
    const pythonBackendUrl = `${process.env.SARA_LEGACY_BACKEND_URL ?? 'http://127.0.0.1:8000'}/api/v1/chat`;
    
    const response = await fetch(pythonBackendUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ agent_id, message }),
    });

    if (!response.ok) {
      throw new Error(`Backend responded with status: ${response.status}`);
    }

    const data = await response.json();
    return NextResponse.json(data);

  } catch (error) {
    console.error('Chat Error:', error);
    return NextResponse.json(
      { error: 'Failed to communicate with SARA chat engine' }, 
      { status: 500 }
    );
  }
}