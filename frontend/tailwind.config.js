/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        cream: "#FFF8F3",
        peach: "#F4DED2",
        burgundy: {
          DEFAULT: "#702B3A",
          deep: "#4A1F2B",
        },
        charcoal: "#32252A",
        mauve: "#7A6870",
        "peach-border": "#E9DAD4",
        status: {
          verified: "#3F6B54",
          "verified-bg": "#EEF5F0",
          "verified-border": "#C5DBC9",
          pending: "#9A631D",
          "pending-bg": "#FDF5E8",
          "pending-border": "#EAD2AD",
          discrepancy: "#9E3B42",
          "discrepancy-bg": "#FBEFEF",
          "discrepancy-border": "#E7C2C5",
        },
      },
      boxShadow: {
        card: "0 1px 2px rgba(74, 31, 43, 0.05), 0 8px 24px -12px rgba(74, 31, 43, 0.08)",
      },
    },
  },
  plugins: [],
};
