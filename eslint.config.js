// The Car Thing's kiosk runs Chromium 69 (2018). ecmaVersion 2018 makes newer
// syntax — optional chaining, ??, class fields — a parse error, and the compat
// plugin flags web APIs that engine doesn't have (targets come from the
// "browserslist" field in package.json).
const js = require('@eslint/js');
const compat = require('eslint-plugin-compat');
const html = require('eslint-plugin-html');
const globals = require('globals');

module.exports = [
  js.configs.recommended,
  compat.configs['flat/recommended'],
  {
    files: ['car-thing-webapp/**/*.html'],
    plugins: { html },
    languageOptions: {
      ecmaVersion: 2018,
      sourceType: 'script',
      globals: globals.browser,
    },
    rules: {
      // `catch (e) {}` is deliberate here: optional catch binding is ES2019.
      'no-unused-vars': ['error', { caughtErrors: 'none' }],
      'no-empty': ['error', { allowEmptyCatch: true }],
    },
  },
];
