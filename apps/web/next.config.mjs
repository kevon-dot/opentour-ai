/** @type {import('next').NextConfig} */
const config = {
  experimental: {
    // Keep explicit ESM imports compatible with the TypeScript sources in the Webpack build.
    extensionAlias: { '.js': ['.js', '.ts', '.tsx'] },
  },
};

export default config;
