const path = require('path');
const {getDefaultConfig, mergeConfig} = require('@react-native/metro-config');

// This app lives in an npm-workspaces monorepo: its dependencies (react-native,
// the shared @tenir/* packages, AsyncStorage, …) are hoisted to the repo root.
// Metro must therefore watch the workspace root and resolve modules from both the
// package-local and the hoisted node_modules.
const projectRoot = __dirname;
const workspaceRoot = path.resolve(projectRoot, '..');

const config = {
  watchFolders: [workspaceRoot],
  resolver: {
    nodeModulesPaths: [
      path.resolve(projectRoot, 'node_modules'),
      path.resolve(workspaceRoot, 'node_modules'),
    ],
  },
};

module.exports = mergeConfig(getDefaultConfig(projectRoot), config);
