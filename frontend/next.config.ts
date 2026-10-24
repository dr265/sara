import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactCompiler: true,
  allowedDevOrigins: ["localhost", "127.0.0.1"],
  output: process.env.SARA_NEXT_STANDALONE === "1" ? "standalone" : undefined,
};

export default nextConfig;
