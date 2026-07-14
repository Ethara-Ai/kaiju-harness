// Kaiju default ESLint flat config.
//
// Applied by commit0/harness/lint_js.py when a JS repo ships NO ESLint config of
// its own, so the SDE stage-2 (lint) ALWAYS has a real ruleset to report — the JS
// counterpart of rust `cargo clippy`, go `go vet`, python `ruff`, all of which run
// with built-in defaults regardless of repo config.
//
// Self-contained ON PURPOSE: no `@eslint/js` / plugin imports, so ESLint can load
// it from the harness path (via `--config`) against ANY repo without needing
// anything in that repo's node_modules. The rules are the plugin-free,
// globals-free subset of `eslint:recommended` (likely-bug detectors). `no-undef`
// is intentionally omitted (it needs per-repo env globals and would false-positive
// on node/browser builtins).
export default [
  {
    files: ["**/*.js", "**/*.mjs", "**/*.cjs", "**/*.jsx"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
    },
    rules: {
      "constructor-super": "error",
      "for-direction": "error",
      "getter-return": "error",
      "no-async-promise-executor": "error",
      "no-case-declarations": "error",
      "no-class-assign": "error",
      "no-compare-neg-zero": "error",
      "no-cond-assign": "error",
      "no-const-assign": "error",
      "no-constant-binary-expression": "error",
      "no-constant-condition": ["warn", { checkLoops: false }],
      "no-control-regex": "error",
      "no-debugger": "warn",
      "no-dupe-args": "error",
      "no-dupe-class-members": "error",
      "no-dupe-else-if": "error",
      "no-dupe-keys": "error",
      "no-duplicate-case": "error",
      "no-empty-character-class": "error",
      "no-empty-pattern": "error",
      "no-ex-assign": "error",
      "no-fallthrough": "error",
      "no-func-assign": "error",
      "no-import-assign": "error",
      "no-invalid-regexp": "error",
      "no-irregular-whitespace": "warn",
      "no-loss-of-precision": "error",
      "no-misleading-character-class": "error",
      "no-new-native-nonconstructor": "error",
      "no-obj-calls": "error",
      "no-self-assign": "error",
      "no-self-compare": "error",
      "no-setter-return": "error",
      "no-sparse-arrays": "warn",
      "no-this-before-super": "error",
      "no-unexpected-multiline": "error",
      "no-unreachable": "error",
      "no-unsafe-finally": "error",
      "no-unsafe-negation": "error",
      "no-unsafe-optional-chaining": "error",
      "no-unused-vars": ["warn", { args: "none" }],
      "no-useless-backreference": "error",
      "require-yield": "error",
      "use-isnan": "error",
      "valid-typeof": "error",
    },
  },
];
