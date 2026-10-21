/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./web_ui/templates/**/*.html", "./web_ui/**/*.py"],
  theme: {
    extend: {
      colors: {
        ink: "#1D2129",
        "ink-muted": "#4E5969",
        "ink-dim": "#86909C",
        accent: {
          DEFAULT: "#1677FF",
          soft: "rgba(22,119,255,0.10)",
          glow: "rgba(22,119,255,0.18)",
          warm: "#FAAD14",
          teal: "#13C2C2",
        },
        surface: {
          0: "#FFFFFF",
          1: "#F5F7FA",
          2: "#FFFFFF",
        },
        edge: {
          DEFAULT: "#E5E6EB",
          soft: "#F0F0F0",
        },
        success: "#52C41A",
        danger: "#FF4D4F",
      },
      fontFamily: {
        sans: ["PingFang SC", "Microsoft YaHei", "Helvetica Neue", "Arial", "sans-serif"],
        mono: ["SFMono-Regular", "Consolas", "Liberation Mono", "Menlo", "monospace"],
      },
    },
  },
};
