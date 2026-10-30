/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/templates/**/*.html"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#0b0d14",
          900: "#10131d",
          850: "#161a27",
          800: "#1c2130",
          750: "#232941",
          700: "#2b3352",
          600: "#61708f",
        },
        volt: {
          200: "#e9ffa1",
          300: "#d9f96e",
          400: "#c8f04a",
          500: "#a8e02a",
          600: "#86b81c",
        },
        lilac: {
          300: "#c4b5fd",
          400: "#a78bfa",
        },
        aqua: {
          300: "#7ceef5",
          400: "#4de1ea",
        },
        flame: {
          300: "#ffc46b",
          400: "#ff9f43",
        },
        mint: {
          200: "#b8f8d8",
          300: "#52eaa8",
          400: "#22d392",
        },
        mist: {
          100: "#eef1fa",
          200: "#ccd5ea",
          300: "#a9b6d3",
          400: "#8291b1",
          500: "#5c6a8a",
        },
      },
      fontFamily: {
        sans: ["\"DM Sans\"", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
      },
      boxShadow: {
        btn: "0 3px 0 #6f9217, 0 10px 28px rgba(200,240,74,.16)",
        card: "0 1px 0 rgba(255,255,255,.05) inset, 0 12px 32px rgba(0,0,0,.35)",
        glow: "0 0 60px rgba(200,240,74,.08)",
      },
    },
  },
  plugins: [],
};
