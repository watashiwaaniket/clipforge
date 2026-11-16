import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        raise: "var(--bg-raise)",
        ink: "var(--ink)",
        muted: "var(--muted)",
        acid: "var(--acid)",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      keyframes: {
        rise: {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        rise: "rise 700ms cubic-bezier(0.2, 0, 0, 1) both",
      },
      boxShadow: {
        card: "0 1px 0 rgba(255,255,255,0.06), 0 24px 48px rgba(0,0,0,0.35)",
      },
    },
  },
  plugins: [],
};

export default config;
