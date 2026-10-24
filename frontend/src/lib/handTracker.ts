import { FilesetResolver, HandLandmarker } from "@mediapipe/tasks-vision";

export interface TrackerStatus {
  hands: number;
  mode: "idle" | "spin" | "zoom";
}

interface TrackerCallbacks {
  onRotate: (deltaTheta: number, deltaPhi: number) => void;
  onZoom: (factor: number) => void;
  onStatus: (status: TrackerStatus) => void;
}

export class HandTracker {
  private video: HTMLVideoElement;
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D | null;
  private callbacks: TrackerCallbacks;
  private stream: MediaStream | null = null;
  private landmarker: HandLandmarker | null = null;
  private animationId: number = 0;
  private isRunning: boolean = false;

  private prevPinchX: number | null = null;
  private prevPinchY: number | null = null;
  private prevHandsDistance: number | null = null;

  constructor(
    video: HTMLVideoElement,
    canvas: HTMLCanvasElement,
    callbacks: TrackerCallbacks
  ) {
    this.video = video;
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.callbacks = callbacks;
  }

  async start(): Promise<void> {
    const vision = await FilesetResolver.forVisionTasks(
      "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@latest/wasm"
    );

    this.landmarker = await HandLandmarker.createFromOptions(vision, {
      baseOptions: {
        modelAssetPath:
          "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
        delegate: "GPU",
      },
      runningMode: "VIDEO",
      numHands: 2,
    });

    this.stream = await navigator.mediaDevices.getUserMedia({
      video: { width: 320, height: 240, facingMode: "user" },
    });

    this.video.srcObject = this.stream;
    await this.video.play();
    this.isRunning = true;
    this.detectLoop();
  }

  private detectLoop = () => {
    if (!this.isRunning || !this.landmarker || !this.ctx) return;

    if (this.video.readyState >= 2) {
      const results = this.landmarker.detectForVideo(
        this.video,
        performance.now()
      );

      this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
      const hands = results.landmarks.length;

      if (hands === 0) {
        this.prevPinchX = null;
        this.prevPinchY = null;
        this.prevHandsDistance = null;
        this.callbacks.onStatus({ hands: 0, mode: "idle" });
      } else if (hands === 1) {
        const landmarks = results.landmarks[0];
        this.drawLandmarks(landmarks);

        // Distance between Thumb Tip (4) and Index Tip (8)
        const pinchDistance = Math.hypot(
          landmarks[4].x - landmarks[8].x,
          landmarks[4].y - landmarks[8].y
        );

        if (pinchDistance < 0.08) {
          const currentX = (landmarks[4].x + landmarks[8].x) / 2;
          const currentY = (landmarks[4].y + landmarks[8].y) / 2;

          if (this.prevPinchX !== null && this.prevPinchY !== null) {
            const dx = currentX - this.prevPinchX;
            const dy = currentY - this.prevPinchY;
            this.callbacks.onRotate(dx * 4, dy * 4);
          }

          this.prevPinchX = currentX;
          this.prevPinchY = currentY;
          this.callbacks.onStatus({ hands: 1, mode: "spin" });
        } else {
          this.prevPinchX = null;
          this.prevPinchY = null;
          this.callbacks.onStatus({ hands: 1, mode: "idle" });
        }
      } else if (hands >= 2) {
        const h1 = results.landmarks[0];
        const h2 = results.landmarks[1];
        this.drawLandmarks(h1);
        this.drawLandmarks(h2);

        const dist = Math.hypot(h1[0].x - h2[0].x, h1[0].y - h2[0].y);

        if (this.prevHandsDistance !== null) {
          const deltaDist = dist - this.prevHandsDistance;
          if (Math.abs(deltaDist) > 0.005) {
            const zoomFactor = deltaDist > 0 ? 0.96 : 1.04;
            this.callbacks.onZoom(zoomFactor);
          }
        }

        this.prevHandsDistance = dist;
        this.callbacks.onStatus({ hands: 2, mode: "zoom" });
      }
    }

    this.animationId = requestAnimationFrame(this.detectLoop);
  };

  private drawLandmarks(landmarks: { x: number; y: number }[]) {
    if (!this.ctx) return;
    this.ctx.fillStyle = "#e879f9";
    for (const point of landmarks) {
      this.ctx.beginPath();
      this.ctx.arc(
        (1 - point.x) * this.canvas.width,
        point.y * this.canvas.height,
        3,
        0,
        2 * Math.PI
      );
      this.ctx.fill();
    }
  }

  stop(): void {
    this.isRunning = false;
    cancelAnimationFrame(this.animationId);
    if (this.stream) {
      this.stream.getTracks().forEach((track) => track.stop());
      this.stream = null;
    }
    if (this.landmarker) {
      this.landmarker.close();
      this.landmarker = null;
    }
  }
}