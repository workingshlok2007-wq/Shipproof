// The browser talks only to this Next.js origin; /api/* is proxied server-side to the FastAPI engine.
// This avoids CORS and works behind Codespaces port forwarding, where the browser cannot reach 127.0.0.1:8000.
const apiUrl = process.env.SHIPPROOF_API_URL ?? "http://127.0.0.1:8000";
if (!/^https?:\/\/[^\s/]+$/.test(apiUrl)) {
  throw new Error(`SHIPPROOF_API_URL must be an origin like http://127.0.0.1:8000, got: ${apiUrl}`);
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/:path*` }];
  },
  async headers() {
    return [{
      source: "/:path*",
      headers: [
        { key: "X-Content-Type-Options", value: "nosniff" },
        { key: "Referrer-Policy", value: "no-referrer" },
        { key: "X-Frame-Options", value: "DENY" },
      ],
    }];
  },
};

export default nextConfig;
