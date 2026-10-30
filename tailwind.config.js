/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/templates/**/*.html"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#14161b",
          900: "#191c23",
          850: "#1f232d",
          800: "#262b37",
          700: "#313847",
          600: "#414a5c",
        },
        volt: {
          200: "#e4fba0",
          300: "#d5f76b",
          400: "#c8f04a",
          500: "#b2dd2e",
          600: "#8fb31f",
        },
        lilac: "#a78bfa",
      },
      fontFamily: {
        sans: ["\"DM Sans\"", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
      },
      boxShadow: {
        btn: "0 3px 0 #6d8f14, 0 10px 24px rgba(200,240,74,.12)",
        card: "0 1px 0 rgba(255,255,255,.04) inset, 0 12px 32px rgba(0,0,0,.35)",
      },
    },
  },
  plugins: [],
};
