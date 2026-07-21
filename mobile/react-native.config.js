const path = require('path');

// Monorepo wiring for the React Native CLI / autolinking. react-native and the native
// dependencies are hoisted to the repo-root node_modules, so point the CLI at the hoisted
// react-native explicitly. The Android project is auto-detected at ./android relative to
// this package — do NOT set project.android.sourceDir to an absolute path: the CLI joins
// it onto the project root, so an absolute value yields a bad path and a null project
// (which breaks RNGP autolinking: "Could not find project.android.packageName").
module.exports = {
  reactNativePath: path.dirname(require.resolve('react-native/package.json')),
};
