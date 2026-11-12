// Correctness lint, not style police — the same posture as the repo's
// ruff config (pyflakes/bugbear classes, no formatting rules). The bug
// this exists to catch is the one that shipped 2026-07-28: a reference
// to state that had been deleted (`view`) crashed the whole app at
// mount, vite built it without a word, and the first signal was a user
// staring at a blank page. `no-undef` makes that a build failure.
import js from "@eslint/js";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  { ignores: ["dist/**", "node_modules/**"] },
  {
    files: ["src/**/*.{js,jsx}", "*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: {
        // browser — the only runtime this code has
        window: "readonly", document: "readonly", navigator: "readonly",
        localStorage: "readonly", fetch: "readonly", console: "readonly",
        EventSource: "readonly", URLSearchParams: "readonly",
        requestAnimationFrame: "readonly", cancelAnimationFrame: "readonly",
        setTimeout: "readonly", clearTimeout: "readonly",
        setInterval: "readonly", clearInterval: "readonly",
        AbortController: "readonly", CustomEvent: "readonly", Event: "readonly",
        getComputedStyle: "readonly", location: "readonly", history: "readonly",
      },
    },
    plugins: { react, "react-hooks": reactHooks },
    settings: { react: { version: "detect" } },
    rules: {
      // the correctness core: everything here is a real bug, never taste
      ...js.configs.recommended.rules,
      "no-unused-vars": ["error", {
        argsIgnorePattern: "^_", varsIgnorePattern: "^_",
        caughtErrors: "none",
      }],
      // JSX participation, so no-unused-vars/no-undef see through JSX
      "react/jsx-uses-vars": "error",
      "react/jsx-uses-react": "error",
      "react/jsx-no-undef": "error",
      "react/jsx-key": "error",
      // hooks discipline: both are bug classes, not opinions — a
      // conditional hook corrupts state order; a stale dep array is
      // exactly how the `view` leftover survived my grep
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
    },
  },
];
